"""Rebuild a messy deck on top of the approved master.

The master is opened as the base file, its own sample slides are dropped, and
every source slide is recreated on the layout it belongs to. Content moves
across; direct formatting does not.

That last point is the whole reason this exists. Re-pointing a slide at a
different layout in place changes nothing visible, because a layout supplies
only what a slide has not already set for itself, and a messy deck has set all
of it. Recreating the slide from the layout is what PowerPoint's Reset Slide
does, and it is the only way the master's geometry and type actually take
effect.

What crosses, and what does not:

    placeholder copy    moved into the matching placeholder on the new layout,
                        keeping paragraph level and bold/italic/underline
                        emphasis, dropping typeface, size and colour so that
                        the layout supplies them
    loose shapes        transplanted as they are, position included; they were
                        never governed by a layout and are not now
    pictures            transplanted with the image itself, so crops and
                        effects survive. A picture PLACEHOLDER is frozen
                        first: the frame and the shape it is cropped to are
                        the old layout's, so they are written onto the shape
                        before it moves and it stops being a placeholder.
                        See `_freeze_inherited` -- without it a circular
                        portrait arrives square and at the origin
    charts, SmartArt,   left behind and reported by name, because their content
    media, embedded     lives in parts this cannot rebuild, and a silently
    objects             broken chart is worse than a missing one

Nothing is dropped quietly. Every slide, every shape and every omission ends up
in the RebuildResult.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Optional, Sequence

from ..classify import SlideKind
from ..extract import read_deck
from ..models import (
    DeckProfile,
    LayoutChoice,
    LayoutProfile,
    RuleTuning,
    SlideProfile,
)
from . import master_apply
from .matcher import FAMILIES, LATENT, LayoutMatch, choose_layout

log = logging.getLogger(__name__)

_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class RebuildError(RuntimeError):
    """Raised when the rebuild cannot start or cannot be written."""


@dataclass
class DroppedShape:
    slide: int
    name: str
    reason: str

    def __str__(self) -> str:
        return f"slide {self.slide}: {self.name!r} ({self.reason})"


@dataclass
class SlideRecord:
    """What happened to one slide, in enough detail to check the work."""

    number: int
    source_layout: Optional[str]
    target_layout: str
    basis: str
    confident: bool
    kind: SlideKind = SlideKind.UNKNOWN     # what the slide is for
    layout_kind: SlideKind = SlideKind.UNKNOWN
    filled: list[str] = field(default_factory=list)
    transplanted: list[str] = field(default_factory=list)
    unfilled: list[str] = field(default_factory=list)
    dropped: list[DroppedShape] = field(default_factory=list)


@dataclass
class RebuildResult:
    master: str
    deck: str
    output: Path
    slides: list[SlideRecord] = field(default_factory=list)
    sample_slides_removed: int = 0
    size_note: Optional[str] = None
    # Which route did the work: "powerpoint" when PowerPoint applied the
    # layouts itself, "xml" when this module rebuilt the file. Reported because
    # the two differ in a way a designer sees -- the XML route drops what it
    # cannot recreate faithfully and lists it, the PowerPoint route drops
    # nothing -- and a reader comparing two runs needs to know which they have.
    applied_by: str = "xml"
    # PowerPoint route only: slides left on another design, and how many
    # masters the output carries as a result.
    stragglers: list[int] = field(default_factory=list)
    masters: int = 1

    @property
    def unmatched(self) -> list[SlideRecord]:
        """Slides placed on a layout that was only the least-bad option."""
        return [record for record in self.slides if not record.confident]

    @property
    def dropped(self) -> list[DroppedShape]:
        return [shape for record in self.slides for shape in record.dropped]

    @property
    def layouts_used(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.slides:
            counts[record.target_layout] = counts.get(record.target_layout, 0) + 1
        return counts


def rebuild(
    master: str | Path,
    deck: str | Path,
    out: str | Path,
    tuning: Optional[RuleTuning] = None,
    route: str = "auto",
    seen: Optional[Sequence[LayoutChoice]] = None,
    master_profile: Optional[DeckProfile] = None,
    deck_profile: Optional[DeckProfile] = None,
) -> RebuildResult:
    """Rebuild `deck` onto `master` and write the result to `out`.

    `master_profile` and `deck_profile` let a caller that has already read
    those files hand them over instead of paying for the parse twice.

    `seen` is what the AI layer read off the rendered slides: which layout
    each one belongs on. It outranks the structural fit and not a layout-name
    match; see `matcher.choose_layout`.

    `route` is "auto" to let PowerPoint do it where it can and fall back to
    rebuilding the file where it cannot, "powerpoint" to require PowerPoint, or
    "xml" to require the rebuild. Not a preference knob: the two routes produce
    genuinely different files -- see rebuild.master_apply -- so anything
    asserting about one has to say which.
    """
    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    master, deck, out = Path(master), Path(deck), Path(out)
    tuning = tuning or RuleTuning()

    # Re-read only what the caller has not already read. Reading a 24MB deck
    # costs about eight seconds, and the pipeline has both files parsed by the
    # time it gets here; doing it again was a quarter of the local work.
    master_profile = master_profile or read_deck(master)
    deck_profile = deck_profile or read_deck(deck)
    if not master_profile.layouts:
        raise RebuildError(
            f"{master.name} defines no slide layouts, so there is nothing to "
            "rebuild onto"
        )

    plans = _plans(deck_profile, master_profile, tuning, seen)

    # PowerPoint first where it can run. Assigning CustomLayout runs its own
    # placeholder matching, which is what actually moves a slide's content into
    # the new layout's placeholders, and nothing is re-serialised by us, so the
    # class of corruption the XML route kept producing cannot arise.
    if route in ("auto", "powerpoint"):
        if master_apply.available():
            applied = master_apply.apply_master(
                deck, master, out, {n: m.name for n, m in plans.items()}
            )
            if applied.fatal is None:
                return _result_from_powerpoint(master, deck, out, plans, applied)
            if route == "powerpoint":
                raise RebuildError(applied.fatal)
            log.warning(
                "PowerPoint could not apply the master (%s); rebuilding the "
                "file instead, which drops what it cannot recreate",
                applied.fatal,
            )
        elif route == "powerpoint":
            raise RebuildError(
                "this host cannot drive PowerPoint, so the master cannot be "
                "applied that way; use route='xml' or 'auto'"
            )

    base = Presentation(str(master))
    source = Presentation(str(deck))

    result = RebuildResult(master=master.name, deck=deck.name, output=out)
    result.sample_slides_removed = _drop_slides(base)
    result.size_note = _size_note(base, source)

    for profile, src_slide in zip(deck_profile.slides, source.slides):
        result.slides.append(
            _rebuild_slide(
                profile, src_slide, base, plans[profile.number],
            )
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        base.save(str(out))
    except Exception as exc:
        raise RebuildError(f"could not write {out}: {exc}") from exc

    log.info(
        "rebuilt %d slide(s) from %s onto %s; %d shape(s) left behind",
        len(result.slides),
        deck.name,
        master.name,
        len(result.dropped),
    )
    return result


def _plans(
    deck_profile: DeckProfile,
    master_profile: DeckProfile,
    tuning: RuleTuning,
    seen: Optional[Sequence[LayoutChoice]] = None,
) -> dict[int, LayoutMatch]:
    """The layout every slide should land on, decided once.

    Decided here rather than inside either route, so both apply the same picks
    and a report from one is comparable with a report from the other.
    """
    by_slide = {choice.slide: choice for choice in seen or ()}
    return {
        profile.number: choose_layout(
            profile, master_profile.layouts, tuning.layout_match_floor,
            deck=deck_profile, seen=by_slide.get(profile.number),
        )
        for profile in deck_profile.slides
    }


def _result_from_powerpoint(
    master: Path,
    deck: Path,
    out: Path,
    plans: dict[int, LayoutMatch],
    applied: master_apply.MasterApplyResult,
) -> RebuildResult:
    """The PowerPoint route's outcome, in the shape the report already reads.

    `filled` and `transplanted` stay empty on purpose. They are the XML route's
    account of which placeholder it copied what into, and PowerPoint does not
    report its matching decisions; claiming a shape landed somewhere specific
    would be inventing detail. What matters is stated: the layout applied, and
    whether it applied at all.
    """
    result = RebuildResult(
        master=master.name, deck=deck.name, output=out,
        applied_by="powerpoint",
        stragglers=applied.stragglers,
        masters=applied.masters,
    )
    for outcome in applied.outcomes:
        match = plans.get(outcome.number)
        result.slides.append(
            SlideRecord(
                number=outcome.number,
                source_layout=None,
                target_layout=outcome.target_layout or "(none)",
                basis=match.basis if match else "none",
                confident=bool(match and match.confident and outcome.applied),
                kind=match.kind if match else SlideKind.UNKNOWN,
                layout_kind=match.layout_kind if match else SlideKind.UNKNOWN,
            )
        )
    log.info(
        "PowerPoint applied %s to %d of %d slide(s) of %s; %d left on another "
        "design, %d master(s) in the output",
        master.name, applied.applied, len(applied.outcomes), deck.name,
        len(applied.stragglers), applied.masters,
    )
    for failure in applied.failed:
        log.warning("slide %d not restyled: %s", failure.number, failure.detail)
    return result


# --------------------------------------------------------------------------- #
# One slide
# --------------------------------------------------------------------------- #

def _rebuild_slide(
    profile: SlideProfile,
    src_slide: Any,
    base: Any,
    match: Optional[LayoutMatch],
) -> SlideRecord:
    # The match arrives decided. Both routes read the same plan, so a report
    # from one is comparable with a report from the other, and re-deciding
    # here would quietly ignore what the AI layer read off the render.
    new_slide = base.slides.add_slide(_layout_object(base, match))

    record = SlideRecord(
        number=profile.number,
        source_layout=profile.layout_name,
        target_layout=match.name,
        basis=match.basis,
        confident=match.confident,
        kind=match.kind,
        layout_kind=match.layout_kind,
    )

    # Before anything claims a placeholder: a picture placeholder's frame and
    # its cropped-to shape are the OLD layout's, and both are about to stop
    # existing. See `_freeze_inherited`.
    frozen = _freeze_inherited(src_slide)
    if frozen:
        log.debug(
            "slide %d: kept the original frame of %s",
            profile.number, ", ".join(frozen),
        )

    pool = _placeholder_pool(new_slide)
    for shape in src_slide.shapes:
        target = _claim(pool, shape) if _is_placeholder(shape) else None
        if target is not None:
            _copy_text(shape, target)
            record.filled.append(_name_of(target))
            continue
        _transplant(shape, src_slide, new_slide, record)

    # Placeholders nothing claimed are left in place rather than deleted. They
    # do not render in a show or in print, and they are the visible answer to
    # "where should this loose text box actually go", which is the one
    # judgement this cannot make for a designer.
    record.unfilled = [_name_of(placeholder) for placeholder in pool]

    _copy_notes(src_slide, new_slide)
    if profile.hidden:
        new_slide._element.set("show", "0")
    return record


def _layout_object(base: Any, match: LayoutMatch) -> Any:
    """The live layout object behind the matched LayoutProfile.

    Found by position rather than by name: a file can carry two slide masters
    that use the same layout name, and the profile's index is what actually
    identifies which one was chosen.
    """
    layouts = [
        layout for master in base.slide_masters for layout in master.slide_layouts
    ]
    return layouts[match.layout.index]


# --------------------------------------------------------------------------- #
# Placeholders
# --------------------------------------------------------------------------- #

def _is_placeholder(shape: Any) -> bool:
    try:
        return bool(shape.is_placeholder)
    except Exception:
        return False


def _placeholder_pool(slide: Any) -> list[Any]:
    """Placeholders on the new slide, in document order, none claimed yet."""
    try:
        return list(slide.placeholders)
    except Exception:
        return []


def _claim(pool: list[Any], shape: Any) -> Optional[Any]:
    """Take the first unclaimed placeholder of the same family.

    A source placeholder with no counterpart on the new layout returns None
    and is transplanted instead, so extra content is never silently lost.
    """
    family = _family_of(shape)
    if family is None:
        return None
    for index, candidate in enumerate(pool):
        if _family_of(candidate) == family:
            return pool.pop(index)
    return None


def _family_of(shape: Any) -> Optional[str]:
    try:
        raw = str(shape.placeholder_format.type or "")
    except Exception:
        return None
    token = raw.split("(")[0].strip().upper()
    if not token or token in LATENT:
        return None
    return FAMILIES.get(token)


def _copy_text(source: Any, target: Any) -> None:
    """Move the copy across, keeping emphasis and dropping brand formatting.

    Bold, italic and underline are part of what the writer meant, so they
    survive. Typeface, size and colour are the layout's business, and carrying
    them over would reproduce exactly the drift this is meant to remove.
    """
    if not _has_text(source) or not _has_text(target):
        return

    frame = target.text_frame
    frame.clear()

    for index, src_para in enumerate(source.text_frame.paragraphs):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.level = src_para.level
        for src_run in src_para.runs:
            run = para.add_run()
            run.text = src_run.text
            for attr in ("bold", "italic", "underline"):
                value = getattr(src_run.font, attr, None)
                if value is not None:
                    setattr(run.font, attr, value)
            _copy_hyperlink(src_run, run)


def _copy_hyperlink(src_run: Any, run: Any) -> None:
    try:
        address = src_run.hyperlink.address
    except Exception:
        return
    if not address:
        return
    try:
        run.hyperlink.address = address
    except Exception:
        log.debug("could not carry the hyperlink %r across", address)


def _has_text(shape: Any) -> bool:
    try:
        return bool(shape.has_text_frame)
    except Exception:
        return False


def _copy_notes(src_slide: Any, new_slide: Any) -> None:
    """Carry the speaker notes across as plain text.

    Notes are written for the presenter, not the audience, so their formatting
    is not a brand concern and only the words are worth moving.
    """
    try:
        if not src_slide.has_notes_slide:
            return
        text = src_slide.notes_slide.notes_text_frame.text
    except Exception:
        return
    if not text.strip():
        return
    try:
        new_slide.notes_slide.notes_text_frame.text = text
    except Exception:
        log.debug("could not carry notes across for slide")


# --------------------------------------------------------------------------- #
# Inherited geometry
# --------------------------------------------------------------------------- #

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _freeze_inherited(src_slide: Any) -> list[str]:
    """Make a picture placeholder's inherited look explicit, then un-placeholder it.

    The bug, from a real deck: a portrait cropped to a circle came out square
    and hard against the left margin, while the decorative arcs drawn around
    it stayed exactly where they were. Nothing had gone wrong with the copy.
    The circle and the position were never on that slide. They were on the OLD
    layout's picture placeholder, and the slide's `p:pic` carried only the
    image, the crop and `p:ph idx="N"`. Point that at another layout and both
    are simply gone: geometry falls back to the implicit `rect`, and the frame
    to whatever the new layout puts at that idx -- the origin, when it puts
    nothing there. The arcs came through untouched because they are ordinary
    autoshapes carrying their own `a:xfrm`, which is the whole difference.

    So the inherited values are written onto the shape while the old layout is
    still reachable, and the `p:ph` is then removed. Removing it matters as
    much as baking the values in: left in place it is a standing invitation to
    inherit again, from a layout that knows nothing about this shape.

    Only placeholders holding an image are touched, never one holding words. A
    text placeholder taking the new layout's type and position is the entire
    point of the rebuild, and freezing one would reproduce exactly the drift
    this exists to remove.

    Returns the names of the shapes it changed. Mutates `src_slide`, which is
    a throwaway in-memory read of the source file and is never written back.
    """
    frozen: list[str] = []
    for shape in list(src_slide.shapes):
        if not _is_placeholder(shape) or not _carries_picture(shape):
            continue
        if _freeze(shape, _layout_placeholder(shape, src_slide)):
            frozen.append(_name_of(shape))
    return frozen


def _carries_picture(shape: Any) -> bool:
    """A shape whose content is an image rather than words.

    Both ways of authoring one count: `p:pic`, and the autoshape with a
    picture fill that PowerPoint writes when a photo is dropped onto a shape.

    Any text disqualifies it. A titled panel that happens to have a photo fill
    is a text placeholder, and its words moving into the new layout's type is
    what has to happen to it.
    """
    element = getattr(shape, "_element", None)
    if element is None:
        return False
    if _text_of(shape).strip():
        return False
    return element.find(f".//{_A_NS}blip") is not None


def _text_of(shape: Any) -> str:
    try:
        return str(shape.text_frame.text) if shape.has_text_frame else ""
    except Exception:
        return ""


def _freeze(shape: Any, layout_ph: Optional[Any]) -> bool:
    """Bake the frame, then the look, then drop the `p:ph`."""
    spPr = getattr(shape._element, "spPr", None)
    if spPr is None:
        return False
    _bake_frame(spPr, _frame_of(shape))
    if layout_ph is not None:
        _bake_look(spPr, getattr(layout_ph._element, "spPr", None))
    return _strip_ph(shape._element)


def _frame_of(shape: Any) -> Optional[tuple[int, int, int, int]]:
    """Where the shape actually sits, resolving what it inherits.

    python-pptx reports the effective value for a placeholder -- its own where
    it has one, otherwise the layout's, otherwise the master's -- and that
    chain is the whole reason this works, because a picture placeholder in a
    designed template carries none of the four itself.
    """
    try:
        frame = (shape.left, shape.top, shape.width, shape.height)
    except Exception:
        # `LayoutPlaceholder._base_placeholder` looks its type up in a table
        # and raises KeyError on one the table does not list, which a
        # hand-built template can carry.
        return None
    if any(value is None for value in frame):
        return None
    return (int(frame[0]), int(frame[1]), int(frame[2]), int(frame[3]))


def _bake_frame(spPr: Any, frame: Optional[tuple[int, int, int, int]]) -> None:
    """Write an explicit `a:xfrm`, unless the shape already had one."""
    if frame is None or spPr.xfrm is not None:
        return
    xfrm = spPr.get_or_add_xfrm()
    xfrm.x, xfrm.y, xfrm.cx, xfrm.cy = frame


def _bake_look(spPr: Any, source: Optional[Any]) -> None:
    """Take the geometry, outline and shadow the layout was supplying.

    Geometry is one or the other: `a:custGeom` and `a:prstGeom` are a schema
    choice, so copying both writes a file PowerPoint refuses. Custom wins
    where a layout has one, because a shape that needed a drawn outline was
    never going to be describable as a preset.

    Anything the slide states for itself is left alone throughout. It was
    overriding the layout before the move and it still should after.
    """
    if source is None:
        return
    if spPr.prstGeom is None and spPr.custGeom is None:
        for name in ("custGeom", "prstGeom"):
            geometry = source.find(f"{_A_NS}{name}")
            if geometry is not None:
                getattr(spPr, f"_insert_{name}")(copy.deepcopy(geometry))
                break
    for name in ("ln", "effectLst"):
        if spPr.find(f"{_A_NS}{name}") is not None:
            continue
        inherited = source.find(f"{_A_NS}{name}")
        if inherited is not None:
            getattr(spPr, f"_insert_{name}")(copy.deepcopy(inherited))


def _layout_placeholder(shape: Any, src_slide: Any) -> Optional[Any]:
    """The source layout's placeholder this shape has been inheriting from."""
    try:
        return src_slide.slide_layout.placeholders.get(idx=shape._element.ph_idx)
    except Exception:
        return None


def _strip_ph(element: Any) -> bool:
    """Turn the shape into an ordinary floating one.

    Not cosmetic. While the `p:ph` is there the shape is a placeholder on a
    slide whose layout has changed, so PowerPoint resolves anything it does
    not state against the wrong layout, and `_claim` offers it a target that
    an image cannot be copied into -- which is its own silent loss, because
    `_copy_text` finds no text frame on either side and returns having done
    nothing, while the shape is reported as filled.
    """
    ph = element.ph
    if ph is None:
        return False
    parent = ph.getparent()
    if parent is None:
        return False
    parent.remove(ph)
    return True


# --------------------------------------------------------------------------- #
# Loose shapes
# --------------------------------------------------------------------------- #

def _transplant(
    shape: Any, src_slide: Any, new_slide: Any, record: SlideRecord
) -> None:
    """Copy a shape's XML across, rebuilding the parts it points at.

    A shape referencing a part this cannot rebuild is left behind rather than
    copied broken: a chart pointing at a chart part that is not in the new file
    opens as an error in PowerPoint, which is harder to notice and to fix than
    an absence the report names.
    """
    name = _name_of(shape)
    element = copy.deepcopy(shape._element)

    unsupported = _remap_relationships(element, src_slide.part, new_slide.part)
    if unsupported:
        record.dropped.append(
            DroppedShape(
                slide=record.number,
                name=name,
                reason=f"references {', '.join(sorted(unsupported))}",
            )
        )
        return

    new_slide.shapes._spTree.append(element)
    record.transplanted.append(name)


def _remap_relationships(element: Any, src_part: Any, tgt_part: Any) -> set[str]:
    """Repoint every r: reference in the copied XML at the new package.

    Returns the relationship kinds that could not be rebuilt. Images are
    re-related to the same image part, which the target package pulls in when
    it is saved. External references (hyperlinks, linked images) are recreated
    from their URL. Anything else points at a part with its own internal
    structure -- a chart, a diagram, an embedded workbook -- and is reported
    rather than copied.
    """
    unsupported: set[str] = set()

    for node in list(element.iter()):
        for attr, rid in list(node.attrib.items()):
            if not attr.startswith(_R_NS):
                continue

            # An empty r:id is the idiom for "no hyperlink here", left behind
            # when one is removed. It is not a reference and there is nothing
            # to rebuild; treating it as a broken one cost fifteen shapes on a
            # real deck, including a cover title.
            if not rid:
                continue

            if _is_tag(node):
                # Customer-data tags carry no content. Dropping the shape over
                # metadata loses the shape; dropping the metadata loses
                # nothing anybody can see.
                _detach(node)
                break

            rel = _relationship(src_part, rid)
            if rel is None:
                unsupported.add("a missing relationship")
                continue
            try:
                if rel.is_external:
                    new_rid = tgt_part.relate_to(
                        rel.target_ref, rel.reltype, is_external=True
                    )
                elif _kind(rel.reltype) in _IMAGE_KINDS:
                    new_rid = _reimport_image(rel, tgt_part)
                else:
                    unsupported.add(_kind(rel.reltype))
                    continue
            except Exception:
                if _is_alternate_image(node):
                    # A high-definition or vector copy of an image the shape
                    # already carries in `a:blip`. Losing the enhancement is
                    # invisible; losing the shape is two missing portraits.
                    log.debug("dropping an alternate image reference on %s", node.tag)
                    _detach(node)
                    break
                unsupported.add(_kind(rel.reltype))
                continue
            node.set(attr, new_rid)

    return unsupported


# Parts that are a picture and nothing else, so copying the bytes copies
# everything. `hdphoto` is the Windows Media Photo alternate PowerPoint writes
# alongside a JPEG; `svg` is the vector original beside its raster fallback.
_IMAGE_KINDS = frozenset({"image", "hdphoto"})

_ALTERNATE_TAGS = ("imgLayer", "svgBlip")


def _is_tag(node: Any) -> bool:
    return node.tag.rsplit("}", 1)[-1] == "tags"


def _is_alternate_image(node: Any) -> bool:
    return node.tag.rsplit("}", 1)[-1] in _ALTERNATE_TAGS


def _detach(node: Any) -> None:
    """Remove a node, and any container left behind with nothing in it.

    Dropping the node alone is not enough. A reference like the saturation
    layer on a picture lives at the bottom of a chain that exists only to hold
    it:

        a:extLst / a:ext / a14:imgProps / a14:imgLayer r:embed="rId10"

    Take out the imgLayer and `a14:imgProps` is left empty, which its schema
    does not allow, and PowerPoint rejects the whole file for it -- "could not
    open the file", no part named. One of these on one slide cost all five
    slides of a real deck.

    So a reference inside an extension takes the whole `a:ext` with it. That
    is the right unit and the only safe one: an extension is a self-contained
    opaque payload, and both `a:ext` and `a:extLst` are permitted to be absent
    entirely, so removing one can never leave a hole the schema minds.

    Outside an extension the node alone goes. Walking up removing emptied
    parents there does more harm than good -- on the same deck it emptied a
    fill on slide 1 and moved the corruption rather than fixing it -- because
    those parents are ordinary required content, not opaque payload.
    """
    parent = node.getparent()
    if parent is None:
        return
    extension = _extension_of(node)
    if extension is not None:
        holder = extension.getparent()
        if holder is not None:
            holder.remove(extension)
            return
    parent.remove(node)


def _extension_of(node: Any) -> Optional[Any]:
    """The `a:ext` this node sits inside, if any."""
    current = node.getparent()
    while current is not None:
        if current.tag.rsplit("}", 1)[-1] == "ext":
            return current
        current = current.getparent()
    return None


def _reimport_image(rel: Any, tgt_part: Any) -> str:
    """Copy the image bytes into the target package and return the new rId.

    Relating the source part directly looks like it works and quietly corrupts
    the file: the foreign part keeps its own partname, and the master almost
    always already has a `ppt/media/image1.png` of its own. Both then write to
    the same name in the zip and one image silently replaces the other.

    Going through the bytes lets the target package allocate a free partname,
    and it deduplicates by content, so a logo transplanted onto twenty slides
    is stored once.

    It also re-sniffs the bytes, which is where this got dangerous. python-pptx
    asks Pillow what the blob is, and Pillow's WMF reader accepts EMF as well
    and answers "WMF" for it. An EMF picture therefore came back typed
    image/x-wmf and was written as image8.wmf. PowerPoint reads the extension,
    tries to parse EMF bytes as WMF, and refuses the entire file:
    "PowerPoint could not open the file", naming no part. One such picture on
    one slide of a real deck cost all five slides.

    So the type is checked before anything is added. A blob the target would
    label differently than the source did is refused, and the caller drops the
    shape and reports it, which is this module's answer to everything it cannot
    rebuild faithfully. Checked first because get_or_add_image_part creates the
    relationship as well as the part: raising afterwards would leave the
    mislabelled part in the package, and content types are global.
    """
    source_type = _content_type(rel.target_part)
    blob = rel.target_part.blob
    target_type = _sniffed_type(blob)
    if source_type and target_type and source_type != target_type:
        raise MediaTypeMismatch(
            f"the source calls this {source_type} and re-importing it would "
            f"write {target_type}"
        )
    _image_part, rid = tgt_part.get_or_add_image_part(BytesIO(blob))
    return rid


class MediaTypeMismatch(Exception):
    """Re-importing a media part would relabel its bytes as another type."""


def _content_type(part: Any) -> Optional[str]:
    try:
        declared = part.content_type
    except Exception:
        return None
    # Not str(): a part with no declared type would come back as "None", which
    # is truthy and matches nothing, so every such blob read as a mismatch.
    return str(declared) if declared else None


def _sniffed_type(blob: bytes) -> Optional[str]:
    """The content type the target package would give these bytes.

    None when it cannot tell, which is not a mismatch: the re-import will
    raise on its own and the caller already handles that.
    """
    from pptx.parts.image import Image  # noqa: PLC0415 - lazy heavy dependency

    try:
        return str(Image.from_blob(blob).content_type)
    except Exception:
        return None


def _kind(reltype: str) -> str:
    return reltype.rsplit("/", 1)[-1] or reltype


def _relationship(part: Any, rid: str) -> Optional[Any]:
    try:
        return part.rels[rid]
    except Exception:
        return None


def _name_of(shape: Any) -> str:
    try:
        return str(shape.name)
    except Exception:
        return "unnamed shape"


# --------------------------------------------------------------------------- #
# Deck level
# --------------------------------------------------------------------------- #

def _drop_slides(base: Any) -> int:
    """Remove the master file's own sample slides.

    The master is being used as a template here, not as content. Its example
    slides are what a designer built to show the layouts off, and shipping them
    inside the rebuilt deck is never wanted.
    """
    slide_ids = base.slides._sldIdLst
    removed = 0
    for slide_id in list(slide_ids):
        base.part.drop_rel(slide_id.rId)
        slide_ids.remove(slide_id)
        removed += 1
    return removed


def _size_note(base: Any, source: Any) -> Optional[str]:
    """Flag a canvas size change, which moves every transplanted shape.

    The master's size wins, because it is the approved one. Loose shapes keep
    their position in inches, so on a different canvas they land somewhere else
    relative to the edges and a person has to look at them.
    """
    if (
        base.slide_width == source.slide_width
        and base.slide_height == source.slide_height
    ):
        return None
    return (
        f"canvas changes from {source.slide_width / 914400:.2f} x "
        f"{source.slide_height / 914400:.2f}in to {base.slide_width / 914400:.2f} x "
        f"{base.slide_height / 914400:.2f}in; transplanted shapes keep their "
        f"position in inches and will need repositioning"
    )
