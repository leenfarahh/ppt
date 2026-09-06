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
                        effects survive
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
from typing import Any, Optional

from ..classify import SlideKind
from ..extract import read_deck
from ..models import DeckProfile, LayoutProfile, RuleTuning, SlideProfile
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
) -> RebuildResult:
    """Rebuild `deck` onto `master` and write the result to `out`."""
    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    master, deck, out = Path(master), Path(deck), Path(out)
    tuning = tuning or RuleTuning()

    master_profile = read_deck(master)
    deck_profile = read_deck(deck)
    if not master_profile.layouts:
        raise RebuildError(
            f"{master.name} defines no slide layouts, so there is nothing to "
            "rebuild onto"
        )

    base = Presentation(str(master))
    source = Presentation(str(deck))

    result = RebuildResult(master=master.name, deck=deck.name, output=out)
    result.sample_slides_removed = _drop_slides(base)
    result.size_note = _size_note(base, source)

    for profile, src_slide in zip(deck_profile.slides, source.slides):
        result.slides.append(
            _rebuild_slide(
                profile, src_slide, base, master_profile.layouts, tuning,
                deck=deck_profile,
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


# --------------------------------------------------------------------------- #
# One slide
# --------------------------------------------------------------------------- #

def _rebuild_slide(
    profile: SlideProfile,
    src_slide: Any,
    base: Any,
    layouts: list[LayoutProfile],
    tuning: RuleTuning,
    deck: Optional[DeckProfile] = None,
) -> SlideRecord:
    match = choose_layout(profile, layouts, tuning.layout_match_floor, deck=deck)
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
    parent = node.getparent()
    if parent is not None:
        parent.remove(node)


def _reimport_image(rel: Any, tgt_part: Any) -> str:
    """Copy the image bytes into the target package and return the new rId.

    Relating the source part directly looks like it works and quietly corrupts
    the file: the foreign part keeps its own partname, and the master almost
    always already has a `ppt/media/image1.png` of its own. Both then write to
    the same name in the zip and one image silently replaces the other.

    Going through the bytes lets the target package allocate a free partname,
    and it deduplicates by content, so a logo transplanted onto twenty slides
    is stored once.
    """
    blob = rel.target_part.blob
    _image_part, rid = tgt_part.get_or_add_image_part(BytesIO(blob))
    return rid


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
