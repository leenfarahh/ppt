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
                        See `rebuild.pictures` -- without it a circular
                        portrait arrives square and at the origin
    charts, SmartArt,   left behind and reported by name, because their content
    media, embedded     lives in parts this cannot rebuild, and a silently
    objects             broken chart is worse than a missing one

Nothing is dropped quietly. Every slide, every shape and every omission ends up
in the RebuildResult.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path, PurePosixPath
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
from . import rtl
from .matcher import FAMILIES, LATENT, LayoutMatch, choose_layout
from .pictures import freeze_slide

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
    # Shapes turned round so a right-to-left deck reads that way. Zero on an
    # English deck, where turning it round would be the defect.
    mirrored: int = 0

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
    mirror: Optional[bool] = None,
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

    # Decided from the deck rather than asked for, unless the caller says
    # otherwise. A master is drawn for one reading direction and applying it
    # to a deck that reads the other way gets the type right and the layout
    # backwards, which is not a thing anybody wants by default.
    if mirror is None:
        mirror = deck_profile.rtl
    if mirror:
        log.info(
            "%s reads right to left; the rebuilt slides will be mirrored so "
            "the content does too", deck.name,
        )

    # PowerPoint first where it can run. Assigning CustomLayout runs its own
    # placeholder matching, which is what actually moves a slide's content into
    # the new layout's placeholders, and nothing is re-serialised by us, so the
    # class of corruption the XML route kept producing cannot arise.
    if route in ("auto", "powerpoint"):
        if master_apply.available():
            applied = master_apply.apply_master(
                deck, master, out, {n: m.name for n, m in plans.items()},
                mirror=mirror,
            )
            if applied.fatal is None:
                result = _result_from_powerpoint(master, deck, out, plans, applied)
                # After PowerPoint, not instead of it: its own matching has
                # already moved every placeholder it could, and this fills what
                # is left from the slide's loose runs. Never fatal -- a failure
                # here leaves the deck exactly as PowerPoint wrote it, which is
                # what this route produced before.
                try:
                    _record_filled(result, *fill_runs(out))
                except Exception:
                    log.warning(
                        "could not fill the layouts' repeated regions from the "
                        "deck's own runs; the slides keep their loose boxes",
                        exc_info=True,
                    )
                _reset_at_the_end(out, mirror)
                return result
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

    if mirror:
        result.mirrored = rtl.mirror_presentation(base)

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        base.save(str(out))
    except Exception as exc:
        raise RebuildError(f"could not write {out}: {exc}") from exc

    _reset_at_the_end(out, mirror)

    log.info(
        "rebuilt %d slide(s) from %s onto %s; %d shape(s) left behind",
        len(result.slides),
        deck.name,
        master.name,
        len(result.dropped),
    )
    return result


def _reset_at_the_end(out: Path, mirror: bool) -> None:
    """The last thing either route does: hand every placeholder back to its
    layout.

    NOT ON A MIRRORED DECK. Turning a right-to-left deck round works by writing
    an explicit position onto the placeholders that carry one -- see
    `rebuild.rtl` -- and this exists to take explicit positions off. Run after
    a mirror it would undo it, silently and completely. An RTL deck keeps the
    geometry the mirror gave it, which is the behaviour it had before this
    existed.

    Never fatal. A reset that fails leaves the deck as the route wrote it.
    """
    if mirror:
        log.info(
            "not resetting the placeholders: this deck was mirrored to read "
            "right to left, and the mirror is written as explicit positions"
        )
        return
    try:
        reset_layouts(out)
    except Exception:
        log.warning(
            "could not reset the slides onto their layouts' geometry; they "
            "keep the boxes they were rebuilt with", exc_info=True,
        )


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
        mirrored=applied.mirrored,
    )
    for outcome in applied.outcomes:
        match = plans.get(outcome.number)
        result.slides.append(
            SlideRecord(
                number=outcome.number,
                source_layout=None,
                target_layout=outcome.target_layout or "(none)",
                basis=(
                    outcome.detail if outcome.forced
                    else match.basis if match else "none"
                ),
                # A forced slide is on the master but not on the layout
                # anybody chose for it, so it is never reported as confident
                # however sure the matcher was of the pick that was missing.
                confident=bool(
                    match and match.confident and outcome.applied
                    and not outcome.forced
                ),
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
    for forced in applied.forced:
        log.warning(
            "slide %d placed on %r: %s",
            forced.number, forced.target_layout, forced.detail,
        )
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
    # existing. See `pictures.freeze_slide`.
    frozen = freeze_slide(src_slide)
    if frozen:
        log.debug(
            "slide %d: kept the original frame of %s",
            profile.number, ", ".join(frozen),
        )

    pool = _placeholder_pool(new_slide)
    canvas = _canvas_of(base)

    # The placeholder pass first, and on its own, because it is the stronger
    # claim: a shape that was already a placeholder is saying which region it
    # belongs in, and the runs below are an inference from how a slide is
    # arranged. Deciding it all up front also means the transplant loop can be
    # told what has already found a home, rather than working it out twice.
    # READ ONCE AND KEEP THE OBJECTS. python-pptx builds a fresh proxy on
    # every `shapes` access, so the same shape is a different Python object
    # each time it is walked and `id()` is only stable within one list. Keyed
    # across two walks the claims match nothing, silently: the file is written
    # unchanged and the log still says what was decided.
    shapes = list(src_slide.shapes)

    claims: dict[int, Any] = {}
    for shape in shapes:
        if not _is_placeholder(shape):
            continue
        target = _claim(pool, shape, canvas)
        if target is not None:
            claims[id(shape)] = target

    loose = [
        shape for shape in shapes
        if not _is_placeholder(shape) and _has_copy(shape)
    ]
    run_claims, furniture, moves = claim_runs(loose, pool, shapes)
    claims.update(run_claims)
    claims.update(claim_drawn_over(
        [s for s in loose if id(s) not in claims], pool
    ))
    spare = {id(shape) for shape in furniture}
    written = layout_writes(new_slide.slide_layout)

    for shape in shapes:
        target = claims.get(id(shape))
        if target is not None:
            _copy_text(shape, target)
            record.filled.append(_name_of(target))
            continue
        if id(shape) in spare:
            record.dropped.append(
                DroppedShape(
                    slide=profile.number,
                    name=_name_of(shape),
                    reason=RUN_DRAWING,
                )
            )
            continue
        if echoes_layout(shape, written):
            record.dropped.append(
                DroppedShape(
                    slide=profile.number,
                    name=_name_of(shape),
                    reason=LAYOUT_ECHO,
                )
            )
            continue
        _transplant(shape, src_slide, new_slide, record)
        _reposition(new_slide, moves.get(id(shape)))

    # Placeholders nothing claimed are left in place rather than deleted. They
    # do not render in a show or in print, and they are the visible answer to
    # "where should this loose text box actually go", which is the one
    # judgement this cannot make for a designer.
    record.unfilled = [_name_of(placeholder) for placeholder in pool]

    lift_imagery(new_slide, _canvas_in(base))

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


def _claim(
    pool: list[Any], shape: Any, canvas: tuple[int, int] = (0, 0)
) -> Optional[Any]:
    """Take the unclaimed placeholder of the same family nearest this shape.

    NEAREST, not first, and the difference is a slide arriving scrambled. The
    pool is in the layout's document order, which is the order the designer's
    XML happens to carry and has nothing to do with the order a reader sees.
    On an agenda of eleven numbered items -- twenty-two placeholders, a number
    and a label each -- taking the first free one of the family put item 01 in
    the last slot on the slide, shifted every other item up by one, and dropped
    a two-digit number into a slot sized for something else, where it wrapped
    to "0 / 3".

    Matched on the box instead: the item that sits top-left on the old slide
    goes in the placeholder that sits top-left on the new one. Overlap decides
    it, with the distance between centres breaking ties, so a slide whose
    layout has moved a little still lands in reading order and a number slot
    takes a number.

    Greedy, in the order the source slide lists its shapes. A shape can take a
    placeholder a later one wanted, which a global assignment would avoid; on
    every deck measured this has not come up, because a source item overlaps
    its own slot far more than it overlaps its neighbour's, and the simpler
    thing that can be read in one sitting is worth more here than the last
    fraction of a percent.

    A source placeholder with no counterpart on the new layout returns None
    and is transplanted instead, so extra content is never silently lost.
    """
    family = _family_of(shape)
    if family is None:
        return None
    candidates = [
        index for index, candidate in enumerate(pool)
        if _family_of(candidate) == family
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda index: _affinity(shape, pool[index], canvas))
    return pool.pop(best)


def claim_runs(
    loose: Sequence[Any], pool: list[Any], everything: Sequence[Any] = ()
) -> tuple[dict[int, Any], list[Any], dict[int, tuple]]:
    """Fill the layout's repeated regions from the slide's repeated content.

    THE CASE THIS IS FOR. A messy deck keeps its content in loose boxes, and
    `_claim` above is only ever offered shapes that are already placeholders,
    so on a real deck the master's `Agenda` layout arrived with every one of
    its twenty-four slots empty and the slide's five agenda items transplanted
    at their old inches -- across the bottom of a layout that puts a photograph
    there. The right layout, chosen correctly, and it may as well not have
    been.

    ORDER DECIDES IT, NOT POSITION, and `rebuild.series` explains at length why
    position cannot: the slide's agenda is five cards across the bottom and the
    layout's is a twelve-item list down the right half, so of the five items
    one overlaps no slot at all and the other four collide in pairs. What maps
    them is that both sides are runs, and a designer fills run to run in the
    order they are read.

    Placeholders it claims come out of `pool`, so they are not reported as
    unfilled and cannot be claimed twice. Nothing is filled by halves: a run
    with no layout run long enough to take all of it is left exactly as it was.
    """
    if not loose or not pool:
        return {}, [], {}
    # Empty ones only. In the rebuild the whole pool is empty by construction,
    # but the same work runs over a file PowerPoint has already written, where
    # a region may have been filled by its own placeholder matching -- and
    # writing a run over that would lose the copy that was already home.
    free = [
        p for p in pool
        if _family_of(p) == "content" and not _has_copy(p)
    ]
    if not free:
        return {}, [], {}

    from .series import (  # noqa: PLC0415 - cycle
        belongs_to,
        companion_of,
        find_series,
        fit_into,
        furniture_of,
        pair_up,
    )

    # Loose shapes only, and that restriction is load-bearing. Offered every
    # shape on the slide, `furniture_of` swept up the layout's own empty
    # placeholders -- small, wordless, and sitting squarely in the run's block
    # -- and deleted the regions the run had just been filled into.
    candidates = [shape for shape in everything if not _is_placeholder(shape)]

    regions = find_series(free, shortest=2)
    pairs = pair_up(find_series(loose), regions)
    claims: dict[int, Any] = {}
    spare: list[Any] = []
    moves: dict[int, tuple[int, int, int, int]] = {}
    for run, slots in pairs:
        for shape, target in zip(run.shapes, slots.shapes):
            claims[id(shape)] = target
            pool.remove(target)

        furniture = furniture_of(run, candidates)
        moved = _move_imagery(
            run, slots, [s for s in furniture if _is_imagery(s)],
            regions, pool, companion_of, belongs_to, fit_into,
        )
        moves.update(moved)
        spare.extend(s for s in furniture if id(s) not in moved)
        log.info(
            "filled %d of %d repeated region(s) from a run of %d loose shape(s)",
            len(run), len(slots), len(run),
        )
    return claims, spare, moves


def claim_drawn_over(loose: Sequence[Any], pool: list[Any]) -> dict[int, Any]:
    """Fill a region from the single box drawn on top of it.

    THE GAP THIS CLOSES. `claim_runs` above fills from RUNS, and a run is three
    or more like-sized boxes, because that is where a run stops being a
    coincidence. A slide's subtitle is one box. So on a real deck the layout's
    subtitle region sat empty, showing its own prompt text, with the deck's
    subtitle drawn across the top of it in a loose box -- the words on the
    slide twice over, once as a prompt and once as content.

    One box is not an arrangement, so there is no order to read. What there is
    instead is geometry, and here it is unambiguous in a way it never was for
    the agenda: the layout's region is 12.28in wide at 0.69in from the edge,
    and the loose box is 12.28in wide at 0.67in. The same rectangle, drawn
    twice. See `series.drawn_over` for why width decides that and height is
    deliberately ignored.

    Runs get first refusal, because a run is the stronger statement: it says
    what a group of boxes IS, where this says only where one of them sits.
    """
    if not loose or not pool:
        return {}
    free = [
        shape for shape in pool
        if _family_of(shape) in ("content", "subtitle", "title")
        and not _has_copy(shape)
    ]
    if not free:
        return {}

    from .series import pair_over  # noqa: PLC0415 - cycle

    claims: dict[int, Any] = {}
    for shape, region in pair_over(loose, free):
        claims[id(shape)] = region
        pool.remove(region)
        log.info(
            "filled %r from the box drawn over it", _name_of(region),
        )
    return claims


def _is_imagery(shape: Any) -> bool:
    """A picture or a group of them, as against a rule or a blank panel.

    The distinction decides what survives a run moving. An icon beside an
    agenda item is content a designer drew and meant; the rule under it is the
    old layout's way of separating two rows, and the new layout draws its own.
    So one follows the item to its new home and the other goes.
    """
    try:
        name = str(shape.shape_type or "").upper()
    except Exception:
        return False
    return "PICTURE" in name or "GROUP" in name


def _move_imagery(
    run, slots, pictures, regions, pool, companion_of, belongs_to, fit_into
) -> dict[int, tuple[int, int, int, int]]:
    """Put each item's icon in the region the layout pairs with its new slot.

    A designer who draws a list of twelve labels usually draws twelve
    somethings beside them -- an ordinal, a rule, a swatch -- and on the master
    this was built against each agenda label has a 0.48in box at the same row,
    which is where its number goes. It is also exactly where the item's icon
    belongs, now that the item has moved.

    ONE ICON PER ITEM OR NONE AT ALL. If the count does not come out exactly --
    two icons over one card, or icons over only some of them -- the mapping is
    a guess, and a guess scatters a deck's iconography across a list. The whole
    lot then falls back to being treated as the old layout's drawing.
    """
    if not pictures or len(pictures) != len(run):
        return {}
    companion = companion_of(slots, regions, taken=slots.shapes)
    if companion is None:
        return {}

    owner = belongs_to(run, pictures)
    if sorted(owner.values()) != list(range(len(run))):
        return {}          # not one each; see above

    moved: dict[int, tuple[int, int, int, int]] = {}
    for shape in pictures:
        target = companion.shapes[owner[id(shape)]]
        moved[id(shape)] = fit_into(shape, target)
        if target in pool:
            pool.remove(target)
    return moved


def _record_filled(
    result: "RebuildResult",
    filled: dict[int, list[str]],
    shed: dict[int, list[tuple[str, str]]],
) -> None:
    """Say on each slide's record which regions its own runs filled.

    The PowerPoint route reports what PowerPoint did, and this happens after
    it, so without this the report would describe a file that is no longer the
    one on disk -- naming as unfilled the regions a designer can now see copy
    sitting in.
    """
    for record in result.slides:
        names = filled.get(record.number)
        if names:
            record.filled.extend(names)
            record.unfilled = [
                name for name in record.unfilled if name not in names
            ]
        for name, reason in shed.get(record.number, []):
            record.dropped.append(
                DroppedShape(slide=record.number, name=name, reason=reason)
            )


def layout_writes(layout: Any) -> set[str]:
    """The words a layout puts on the page itself, normalised for comparison.

    Its own shapes, not its placeholders: a placeholder is a space for someone
    else's copy, and what is wanted here is the copy the layout ALREADY has.
    """
    words = set()
    try:
        shapes = list(layout.shapes)
    except Exception:
        return words
    for shape in shapes:
        if _is_placeholder(shape):
            continue
        text = _normalised(shape)
        if text:
            words.add(text)
    return words


def _normalised(shape: Any) -> str:
    try:
        if not shape.has_text_frame:
            return ""
        text = shape.text_frame.text
    except Exception:
        return ""
    kept = [c.lower() for c in text if c.isalnum() or c.isspace()]
    return " ".join("".join(kept).split())


def echoes_layout(shape: Any, written: set[str]) -> bool:
    """Whether this shape says only what the layout already says.

    THE SLIDE IT IS FOR. A deck's agenda carries its title in a TITLE
    placeholder, and the master's agenda layout has no title region at all --
    it draws the word AGENDA itself, as artwork. So the title finds no home,
    is transplanted at its old inches, and the rebuilt slide says "Agenda"
    twice: once in the layout's own display type and once in the deck's, over
    the layout's photograph.

    Matched on the whole of the text and not on a part of it, with case,
    punctuation and spacing taken out. A heading that repeats the layout's
    word and then says more is not an echo, it is a heading.
    """
    if not written:
        return False
    return _normalised(shape) in written


# How much of the canvas a layout shape must cover before content sitting on it
# is sitting on the LAYOUT rather than on empty page. A hairline rule and a
# 0.67in logo come to a fraction of a percent; the arcs and panels that swallow
# an icon run from a quarter of the page upward.
_ARTWORK_SHARE = 0.06

# And how large a graphic may be and still be something that labels content
# rather than being content. An icon is under a percent of the page; a
# photograph is a third of it, and moving a photograph is never this function's
# business.
_LABEL_SHARE = 0.02

# Clear air between a lifted graphic and the copy it labels.
_GAP_IN = 0.15


def lift_imagery(slide: Any, canvas: tuple[float, float]) -> list[tuple[str, float]]:
    """Move small graphics off the layout's own artwork, onto what they label.

    THE SLIDE THIS IS FOR. A deck's two-track page carried an icon above each
    column, sitting on the edge of the arc its OLD layout drew. The master's arc
    is bigger, so on the rebuilt slide both icons sat well inside it: dark line
    art on a dark disk, still there and impossible to see. Nothing was wrong
    with the geometry and nothing was wrong with the layout choice. The slide
    was simply drawn against a different arc.

    IT IS NOT ENOUGH TO ASK WHETHER A SHAPE IS ON THE ARTWORK. That layout puts
    its own title and body regions inside the same arc, in reversed type, on
    purpose -- so "sits on the artwork" does not separate wrong from intended.
    What separates them is that these graphics belong to something: each one is
    centred over a column of copy, and the place it belongs is directly above
    that copy. So a graphic is lifted only when it is small enough to be a
    label, is sitting on artwork big enough to swallow it, and has copy beneath
    it that it is centred over. Anything with nothing underneath it is left
    alone, because there is nothing to say where it should go instead.

    Returns what moved and how far, in inches, for the record.
    """
    width, height = canvas
    if width <= 0 or height <= 0:
        return []
    page = width * height

    try:
        shapes = list(slide.shapes)
        artwork = [
            _rect(shape, width, height) for shape in slide.slide_layout.shapes
            if not _is_placeholder(shape)
        ]
    except Exception:
        log.debug("could not read the layout's artwork", exc_info=True)
        return []
    artwork = [box for box in artwork if box and _area(box) >= _ARTWORK_SHARE * page]
    if not artwork:
        return []

    loose = [shape for shape in shapes if not _is_placeholder(shape)]
    labels = [
        shape for shape in loose
        if _is_imagery(shape) and _area(_rect(shape, width, height) or (0, 0, 0, 0))
        <= _LABEL_SHARE * page
    ]
    copy = [shape for shape in loose if _has_copy(shape)]

    moved: list[tuple[str, float]] = []
    for cluster in _clusters(labels, width, height):
        box = _union([_rect(shape, width, height) for shape in cluster])
        if box is None or not any(_overlaps(box, art) for art in artwork):
            continue
        target = _copy_below(box, copy, width, height)
        if target is None:
            continue
        lift = (target[1] - _GAP_IN) - box[3]
        if lift <= 0:
            continue                  # already clear of the copy it labels
        for shape in cluster:
            shape.top = int(shape.top + lift * 914400)
            moved.append((_name_of(shape), lift))
    return moved


def _canvas_in(base: Any) -> tuple[float, float]:
    try:
        return ((base.slide_width or 0) / 914400,
                (base.slide_height or 0) / 914400)
    except Exception:
        return (0.0, 0.0)


def _rect(shape: Any, width: float, height: float):
    """A shape's box in inches, clipped to the canvas, or None if it has none.

    CLIPPED, because a layout's artwork is routinely drawn far off the page: the
    photograph inside one master's arc is a 13.33in-tall picture starting five
    inches above the top edge, and taken at face value its bounding box covers
    everything, so every shape on the slide would read as sitting on it.
    """
    try:
        left = (shape.left or 0) / 914400
        top = (shape.top or 0) / 914400
        right = left + (shape.width or 0) / 914400
        bottom = top + (shape.height or 0) / 914400
    except Exception:
        return None
    left, top = max(0.0, left), max(0.0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _area(box) -> float:
    if not box:
        return 0.0
    return (box[2] - box[0]) * (box[3] - box[1])


def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _union(boxes):
    boxes = [box for box in boxes if box]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _clusters(shapes, width: float, height: float) -> list[list[Any]]:
    """Graphics that overlap each other, grouped -- one icon is often several.

    The two icons on the slide this was written for are each a ring and a glyph
    sitting inside it, two top-level shapes apiece. Lifting one and not the
    other would take an icon apart.
    """
    groups: list[list[Any]] = []
    for shape in shapes:
        box = _rect(shape, width, height)
        if box is None:
            continue
        for group in groups:
            if any(_overlaps(box, _rect(other, width, height) or box)
                   for other in group):
                group.append(shape)
                break
        else:
            groups.append([shape])
    return groups


def _copy_below(box, copy: Sequence[Any], width: float, height: float):
    """The nearest copy under this graphic that it is centred over, or None."""
    middle = (box[0] + box[2]) / 2
    best = None
    for shape in copy:
        other = _rect(shape, width, height)
        if other is None or other[1] < box[3]:
            continue                          # not below it
        if not (other[0] <= middle <= other[2]):
            continue                          # not what it sits over
        if best is None or other[1] < best[1]:
            best = other
    return best


RUN_DRAWING = "drew a run of content that the layout now draws itself"
LAYOUT_ECHO = "says only what the layout already writes on the page"


def fill_runs(
    path: Path,
) -> tuple[dict[int, list[str]], dict[int, list[tuple[str, str]]]]:
    """Fill a written deck's repeated regions from its own loose runs.

    THE POWERPOINT ROUTE NEEDS THIS AND THE REBUILD DOES NOT. Assigning a
    CustomLayout runs PowerPoint's own placeholder matching, which is why that
    route is preferred -- but that matching only ever moves a PLACEHOLDER into
    a placeholder. A messy deck's content is in loose boxes, so on a real deck
    PowerPoint put all twenty-four of the agenda layout's slots on the slide,
    empty, and left the five agenda items sitting at their old inches on top of
    the layout's photograph. The XML route does this inline in `_rebuild_slide`;
    here it has to be a second pass over the file PowerPoint wrote.

    The file IS re-serialised by this, which the PowerPoint route otherwise
    avoids on purpose. It is a narrow re-serialisation -- text set into
    placeholders that are already on the slide, and the consumed boxes removed
    -- and nothing is copied between parts, which is where that route's
    corruption came from. It also only happens at all on a slide where a run
    was actually matched, so a deck this cannot help is written by PowerPoint
    and not touched again.
    """
    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    presentation = Presentation(str(path))
    canvas = (
        (presentation.slide_width or 0) / 914400,
        (presentation.slide_height or 0) / 914400,
    )
    filled: dict[int, list[str]] = {}
    shed: dict[int, list[tuple[str, str]]] = {}
    touched: set[int] = set()
    for number, slide in enumerate(presentation.slides, start=1):
        try:
            # One list, for the reason `_rebuild_slide` gives: a fresh proxy
            # per access makes `id()` meaningless across two walks.
            shapes = list(slide.shapes)
            pool = list(slide.placeholders)
            loose = [
                shape for shape in shapes
                if not _is_placeholder(shape) and _has_copy(shape)
            ]
            claims, furniture, moves = claim_runs(loose, pool, shapes)
            claims.update(claim_drawn_over(
                [s for s in loose if id(s) not in claims], pool
            ))
            written = layout_writes(slide.slide_layout)
        except Exception:
            log.debug("could not settle slide %d", number, exc_info=True)
            continue

        drawing = {id(shape) for shape in furniture} - set(moves)
        names: list[str] = []
        gone: list[tuple[str, str]] = []

        for shape in shapes:
            target = claims.get(id(shape))
            if target is not None:
                # The box the copy came out of goes with the copy, or the slide
                # says it twice: once where the layout puts it and once where
                # it used to be, which is worse than either on its own.
                _copy_text(shape, target)
                names.append(_name_of(target))
                _remove(shape)
            elif id(shape) in drawing:
                # The run's own drawing goes for the same reason: the new
                # layout draws its own. See `series.furniture_of`.
                _remove(shape)
                gone.append((_name_of(shape), RUN_DRAWING))
            elif echoes_layout(shape, written):
                # Separate defect, same slide: a title the layout already
                # writes as artwork, carried over because the layout offers no
                # title region for it to land in. Reported as itself, not as
                # the run's drawing -- a reader checking the work needs to know
                # which of the two happened.
                _remove(shape)
                gone.append((_name_of(shape), LAYOUT_ECHO))
            elif id(shape) in moves:
                shape.left, shape.top, shape.width, shape.height = moves[id(shape)]

        lifted = lift_imagery(slide, canvas)
        if lifted:
            log.info(
                "slide %d: lifted %d graphic(s) off the layout's artwork onto "
                "the copy they label", number, len(lifted),
            )

        if names:
            filled[number] = names
        if gone:
            shed[number] = gone
        if names or gone or moves or lifted:
            touched.add(number)

    if touched:
        presentation.save(str(path))
        log.info(
            "settled %d slide(s): %d region(s) filled from the deck's own runs, "
            "%d shape(s) removed as the layout's own work",
            len(touched),
            sum(len(names) for names in filled.values()),
            sum(len(names) for names in shed.values()),
        )
    return filled, shed


def _reposition(new_slide: Any, where) -> None:
    """Move the shape just transplanted into the box a run's companion gives it.

    Addressed as the last shape on the new slide, because that is what
    `_transplant` appends and the source shape itself is in the other file.
    """
    if where is None:
        return
    try:
        shape = list(new_slide.shapes)[-1]
        shape.left, shape.top, shape.width, shape.height = where
    except Exception:
        log.debug("could not reposition a run's imagery", exc_info=True)


def _remove(shape: Any) -> None:
    parent = shape._element.getparent()
    if parent is not None:
        parent.remove(shape._element)


def _has_copy(shape: Any) -> bool:
    """Whether this shape carries copy, rather than being furniture.

    A run is matched on the boxes, and an empty box is the same size as a full
    one. The separator rules and blank panels a designer draws behind a row of
    items are exactly that shape, and a layout region filled from one of them
    is a region filled with nothing -- while the item that should have gone
    there keeps its old inches.
    """
    try:
        return bool(shape.has_text_frame and shape.text_frame.text.strip())
    except Exception:
        return False


def _affinity(source: Any, target: Any, canvas: tuple[int, int]) -> float:
    """How much these two boxes look like the same place on the slide.

    Overlap first, because two boxes that share ground are the same slot
    however differently they are sized. Where nothing overlaps -- a messy
    deck's item sitting where the new layout puts nothing -- the nearer of the
    free slots is the better guess, so the distance between centres decides,
    negated so that closer scores higher and always below any real overlap.
    """
    a, b = _box_of(source), _box_of(target)
    if a is None or b is None:
        return -1e9
    width, height = canvas
    if width > 0 and height > 0:
        a = (a[0] / width, a[1] / height, a[2] / width, a[3] / height)
        b = (b[0] / width, b[1] / height, b[2] / width, b[3] / height)

    overlap = (
        max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    )
    if overlap > 0:
        union = (
            (a[2] - a[0]) * (a[3] - a[1])
            + (b[2] - b[0]) * (b[3] - b[1])
            - overlap
        )
        return overlap / union if union > 0 else 0.0

    centre_a = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
    centre_b = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    distance = (
        (centre_a[0] - centre_b[0]) ** 2 + (centre_a[1] - centre_b[1]) ** 2
    ) ** 0.5
    return -distance


def _box_of(shape: Any) -> Optional[tuple[float, float, float, float]]:
    """(left, top, right, bottom), or None where the shape will not say.

    A placeholder that states no position of its own answers with the value it
    inherits from its layout, which is the position it will be drawn at and so
    the one to match on.
    """
    try:
        left, top = shape.left, shape.top
        width, height = shape.width, shape.height
    except Exception:
        return None
    if None in (left, top, width, height):
        return None
    return (float(left), float(top), float(left + width), float(top + height))


def _canvas_of(presentation: Any) -> tuple[int, int]:
    """The output canvas, for measuring both decks in the same units.

    A deck built at 4:3 and a master at 16:9 state the same place with
    different numbers, so boxes are compared as fractions of their own canvas
    rather than in EMU.
    """
    try:
        return int(presentation.slide_width or 0), int(presentation.slide_height or 0)
    except Exception:
        return (0, 0)


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
    if target_type is None:
        # Nothing recognised the bytes, which is not the same as bad bytes.
        # An icon out of PowerPoint's own library is an SVG with an EMF
        # fallback, and Pillow identifies neither, so `get_or_add_image_part`
        # raised on both halves of every one of them: the SVG was dropped as
        # an "invisible enhancement" and the EMF was left holding the source's
        # relationship id, which means nothing on the new slide. The icon
        # arrived as a broken-image box -- "The picture can't be displayed" --
        # and only for library icons, which is why some survived and some did
        # not.
        #
        # These do not need identifying. They are valid parts already; what
        # they need is a partname in the target package and the content type
        # the source declared. See `_carry_media`.
        return _carry_media(rel, tgt_part, source_type)
    if source_type and source_type != target_type:
        raise MediaTypeMismatch(
            f"the source calls this {source_type} and re-importing it would "
            f"write {target_type}"
        )
    _image_part, rid = tgt_part.get_or_add_image_part(BytesIO(blob))
    return rid


def _carry_media(rel: Any, tgt_part: Any, source_type: Optional[str]) -> str:
    """Copy media bytes across verbatim, keeping the type the source declared.

    Used for the parts python-pptx will not sniff. It does the two things
    `get_or_add_image_part` was doing that actually matter -- a free partname
    in the target package, and dedup so an icon on twenty slides is stored
    once -- and skips the part that was in the way, asking Pillow what the
    bytes are.

    The type is taken from the source rather than guessed, which is the whole
    reason this is safe: the source package declared it, PowerPoint wrote it,
    and copying a declaration is not a decision. Where the source declares
    nothing there is nothing to copy faithfully, so it raises and the caller
    reports the shape as it would any other media it cannot rebuild.
    """
    from pptx.opc.package import Part  # noqa: PLC0415 - lazy, opc internals

    if not source_type:
        raise MediaTypeMismatch(
            "these bytes are of no type anything here can name, so copying "
            "them would be a guess"
        )

    blob = rel.target_part.blob
    package = tgt_part.package
    # Dedup by content, like the image path it stands in for. Held on the
    # package because that is what the parts belong to, and a rebuild makes
    # one of those per run.
    carried = getattr(package, "_carried_media", None)
    if carried is None:
        carried = {}
        package._carried_media = carried

    key = (source_type, hashlib.sha1(blob).hexdigest())
    part = carried.get(key)
    if part is None:
        extension = PurePosixPath(str(rel.target_part.partname)).suffix or ".bin"
        partname = package.next_partname(f"/ppt/media/image%d{extension}")
        part = Part(partname, source_type, package, blob)
        carried[key] = part
    return tgt_part.relate_to(part, rel.reltype)


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


# --------------------------------------------------------------------------- #
# Reset
# --------------------------------------------------------------------------- #

def reset_layouts(path: Path) -> dict[int, int]:
    """Put every placeholder back on the geometry its layout gives it.

    THIS IS "RESET SLIDE", DONE TO THE FILE. A layout supplies a placeholder's
    position and size only while the placeholder does not state its own, and a
    messy deck states its own everywhere -- an explicit `a:xfrm` on every
    shape, written against whatever layout it used to sit on. Re-pointing the
    slide at a new layout does not clear those, so the copy lands in the new
    design still wearing the old one's boxes: a title that was 4.4in wide on
    its old layout stays 4.4in wide on a layout that gives titles the full
    width, and a title that was narrow stays narrow and reflows to one word a
    line down the side of the page.

    Only where the new layout actually has something to say. A placeholder with
    no counterpart on the layout it now points at inherits nothing, so
    stripping its `a:xfrm` would not reset it, it would send it to the origin
    at a default size. Those keep what they have and are left for the rest of
    the rebuild to deal with.

    Returns the count of placeholders reset per slide.
    """
    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    presentation = Presentation(str(path))
    reset: dict[int, int] = {}
    for number, slide in enumerate(presentation.slides, start=1):
        try:
            offered = _layout_slots(slide.slide_layout)
            count = sum(
                1 for shape in list(slide.shapes)
                if _is_placeholder(shape)
                and _slot_of(shape) in offered
                and _drop_xfrm(shape)
            )
        except Exception:
            log.debug("could not reset slide %d", number, exc_info=True)
            continue
        if count:
            reset[number] = count

    if reset:
        presentation.save(str(path))
        log.info(
            "reset %d placeholder(s) across %d slide(s) onto the geometry "
            "their layout gives them",
            sum(reset.values()), len(reset),
        )
    return reset


def _layout_slots(layout: Any) -> set:
    """Which placeholder slots this layout defines, by idx and by type."""
    slots = set()
    try:
        shapes = list(layout.placeholders)
    except Exception:
        return slots
    for shape in shapes:
        slot = _slot_of(shape)
        if slot is not None:
            slots.add(slot)
    return slots


def _slot_of(shape: Any):
    """A placeholder's identity on its layout: its idx, or its type.

    Both, because PowerPoint matches on both. A title has no meaningful idx
    and is found by type; a body region is one of several and is found by idx.
    """
    try:
        fmt = shape.placeholder_format
        return (str(fmt.type).split()[0].upper(), fmt.idx)
    except Exception:
        return None


def _drop_xfrm(shape: Any) -> bool:
    """Remove a shape's own position and size, so it inherits again."""
    try:
        spPr = shape._element.spPr
    except Exception:
        return False
    if spPr is None:
        return False
    removed = False
    for xfrm in spPr.findall(f"{_A_NS}xfrm"):
        spPr.remove(xfrm)
        removed = True
    return removed


_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
