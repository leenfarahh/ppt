"""Apply the fixes a designer ticked, and optionally the master's layouts.

Two operations that belong in one command because they are one decision: the
deck goes out corrected, or it does not.

    original deck
        |
        v
    selected fixes      only the findings that were ticked, each one a
        |               mechanical correction to a named shape
        v
    rebuild (optional)  recreate every slide on the master's layout
        |
        v
    new file

Order matters. Fixes run first, on the deck the findings were measured
against, so a finding that names "TextBox 28 on slide 2" still means that
shape. Rebuilding first would move the copy into placeholders and rename
things underneath the findings.

The input deck is never modified. Everything happens on a copy.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..models import Issue, RuleTuning, Tolerances
from .fixers import (
    COHORT,
    GEOMETRIC,
    is_geometric,
    RELATIVE,
    LeaveAlone,
    fix_order,
    fixer_for,
    why_not_fixable,
)

log = logging.getLogger(__name__)


class ApplyError(RuntimeError):
    """Raised when the fixes cannot be applied or the result cannot be written."""


@dataclass
class FixBrand:
    """The master's own values, for checking a proposed fix against.

    Only the AI dispatcher uses these, and only to refuse. A rule finding
    carries a target that was measured off the master in the first place; a
    proposal from the model has to be shown to be one of the master's values
    before it is written into a client deck.
    """

    palette: dict[str, str] = field(default_factory=dict)
    allowed_fonts: list[str] = field(default_factory=list)
    roles: dict[str, tuple[Optional[float], Optional[float]]] = field(
        default_factory=dict
    )

    def size_range(self, role: str) -> tuple[Optional[float], Optional[float]]:
        """The pt range for a role, or (None, None) when none is declared.

        An undeclared role does not veto a size. The brand system is allowed
        to be silent about body copy, and refusing every proposal for a role
        nobody wrote a rule about would be inventing a rule.
        """
        return self.roles.get(role, (None, None))

    @classmethod
    def from_spec(cls, spec: Any) -> "FixBrand":
        roles: dict[str, tuple[Optional[float], Optional[float]]] = {}
        for name, role in (getattr(spec, "roles", None) or {}).items():
            roles[str(name)] = (
                getattr(role, "min_size_pt", None),
                getattr(role, "max_size_pt", None),
            )
        return cls(
            palette=dict(getattr(spec, "palette", None) or {}),
            allowed_fonts=list(getattr(spec, "allowed_fonts", None) or []),
            roles=roles,
        )


@dataclass
class FixContext:
    """What a fixer needs to know about the deck it is working on."""

    width_emu: int
    height_emu: int
    # How close two edges have to be to read as deliberately aligned. The same
    # number the rules measure with, so a fix cannot break a relationship the
    # rules would have called aligned, and defaulted off the model rather than
    # restated as a literal here.
    align_tolerance_in: float = Tolerances().position_in
    # How far a shape can be from the position a finding names and still be
    # read as having drifted there. Past it the placement was a decision, so
    # the fix that would undo it stands down. The same ceiling the repeat
    # rules measure with, and defaulted off the model for the same reason.
    max_drift_in: float = RuleTuning().repeat_max_drift_in
    # The master's palette, typefaces and role sizes. None when the caller had
    # no master to hand, and then no AI-proposed fix runs at all: an unchecked
    # proposal is a guess with a brand label on it.
    brand: Optional[FixBrand] = None
    # The master's safe margins, in inches, keyed left/top/right/bottom. What a
    # proposed position is checked against: the model is given these in its
    # payload, so a target outside them is not a reading it could defend.
    margins: Optional[dict[str, float]] = None
    # The other shapes on the finding's slide, filled in per finding. Scratch
    # rather than configuration: the two fixes that spread a row need to find
    # the row, and going back to the file for it would re-read a deck the
    # applier already has open.
    neighbours: list = field(default_factory=list)


@dataclass
class FixOutcome:
    issue: Issue
    applied: bool
    detail: str

    @property
    def slide(self) -> Optional[int]:
        return self.issue.slide

    def __str__(self) -> str:
        where = f"slide {self.issue.slide}" if self.issue.slide else "deck"
        shape = f" {self.issue.shape!r}" if self.issue.shape else ""
        return f"{self.issue.id} {where}{shape}: {self.detail}"


@dataclass
class ApplyResult:
    deck: str
    output: Path
    applied: list[FixOutcome] = field(default_factory=list)
    skipped: list[FixOutcome] = field(default_factory=list)
    rebuilt: Optional[Any] = None      # RebuildResult, when --rebuild was asked for
    # What the rules find on the deck that was just written, as opposed to the
    # one that was measured. See `_recheck`.
    recheck: list[Issue] = field(default_factory=list)
    rechecked: bool = False
    # What the second round applied to the written deck, and what it measured
    # afterwards. See `_second_round`.
    second_round: list[FixOutcome] = field(default_factory=list)
    settled: list[Issue] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.applied)

    @property
    def removed(self) -> list[FixOutcome]:
        """Shapes taken off the deck, which is the one change with no evidence.

        Everything else this does leaves something on the slide to look at. A
        removal leaves a gap, so it is listed on its own rather than being one
        line among forty, and each outcome quotes what the shape said.
        """
        return [
            outcome for outcome in self.applied
            if outcome.issue.fix is not None
            and outcome.issue.fix.op == "remove_note"
        ]

    @property
    def introduced(self) -> list[Issue]:
        """Findings on the output that the input did not have.

        Matched on rule and place rather than on id: an id carries the message,
        and a message carrying a measurement changes when the measurement does,
        so an overlap that grew from 2.79 to 3.10 sq in would read as a new
        finding rather than the same one made worse.
        """
        before = {(i.rule_id, i.slide, i.shape) for i in self.before}
        return [
            issue for issue in self.recheck
            if (issue.rule_id, issue.slide, issue.shape) not in before
        ]

    before: list[Issue] = field(default_factory=list)


def fixable(issues: Iterable[Issue]) -> list[Issue]:
    """The findings that have a fixer, in report order."""
    return [issue for issue in issues if fixer_for(issue) is not None]


def apply_fixes(
    deck: str | Path,
    issues: Sequence[Issue],
    out: str | Path,
    selected: Optional[Iterable[str]] = None,
    master: Optional[str | Path] = None,
    tuning: Optional[RuleTuning] = None,
    tolerances: Optional[Tolerances] = None,
    spec: Optional[Any] = None,
) -> ApplyResult:
    """Apply the selected findings to `deck` and write the result to `out`.

    `selected` is the set of finding ids the designer ticked. None means every
    finding that has a fixer, which is the "fix everything mechanical" case;
    an empty set means none, which is a legitimate request when the only thing
    wanted is the rebuild.

    With `master`, the corrected deck is then rebuilt onto that master's
    layouts.
    """
    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    deck, out = Path(deck), Path(out)
    if not deck.exists():
        raise ApplyError(f"deck not found: {deck}")

    wanted = _wanted(issues, selected)
    out.parent.mkdir(parents=True, exist_ok=True)

    presentation = Presentation(str(deck))
    context = FixContext(
        width_emu=presentation.slide_width,
        height_emu=presentation.slide_height,
        align_tolerance_in=(tolerances or Tolerances()).position_in,
        max_drift_in=(tuning or RuleTuning()).repeat_max_drift_in,
        brand=FixBrand.from_spec(spec) if spec is not None else None,
        margins=_margins_of(spec),
    )
    result = ApplyResult(deck=deck.name, output=out)

    # Shapes the report already faults geometrically. An alignment with one of
    # these is not a relationship worth protecting: it is two shapes wrong the
    # same way, and often the neighbour is being moved in this very run.
    in_breach = {
        (issue.slide, issue.shape_id)
        for issue in issues
        if (issue.rule_id or "") in GEOMETRIC and issue.shape_id is not None
    }

    # Shapes a fix has already moved in this run. A fix measured as a delta
    # cannot be trusted against one of these; see RELATIVE.
    moved: set[tuple[Optional[int], Optional[int]]] = set()

    # Stable sort, so findings of equal priority stay in report order and a
    # run is reproducible.
    for issue in sorted(wanted, key=fix_order):
        result_line = _apply_one(issue, presentation, context, in_breach, moved)
        (result.applied if result_line.applied else result.skipped).append(result_line)
        if result_line.applied and (issue.rule_id or "") in GEOMETRIC:
            moved.add((issue.slide, issue.shape_id))

    try:
        presentation.save(str(out))
    except Exception as exc:
        raise ApplyError(f"could not write {out}: {exc}") from exc

    log.info(
        "%s: applied %d fix(es), %d skipped -> %s",
        deck.name,
        len(result.applied),
        len(result.skipped),
        out,
    )

    if master is not None:
        result.rebuilt = _rebuild_in_place(master, out, tuning)

    result.before = list(issues)
    _recheck(result, spec)
    _second_round(result, context, spec)
    return result


def _second_round(
    result: ApplyResult, context: FixContext, spec: Optional[Any]
) -> None:
    """Correct what this run caused, on the deck it wrote.

    The first round applies what a designer ticked, measured against the deck
    as it arrived. Applying it makes a different file -- type at the master's
    size, a box that no longer shrinks its text, a shape that no longer clears
    its neighbour -- and some of what that file measures was not true when the
    list was ticked, so it could not have been on it.

    ONLY what this run introduced, which is the line that keeps this from
    exceeding its mandate. A finding the deck already had and the designer did
    not tick is one they chose to leave; picking it up here would apply
    something nobody asked for. A finding that appeared because of the fixes
    is this run's mess, and clearing it up is finishing the job rather than
    widening it.

    One round, never a loop. A fix that provokes a finding that provokes
    another fix is a fight between two rules, and the honest end of that is a
    report saying so rather than a deck that keeps moving.
    """
    if not result.rechecked:
        return
    todo = fixable(result.introduced)
    if not todo:
        result.settled = list(result.recheck)
        return

    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    try:
        presentation = Presentation(str(result.output))
        moved: set[tuple[Optional[int], Optional[int]]] = set()
        for issue in sorted(todo, key=fix_order):
            outcome = _apply_one(issue, presentation, context, None, moved)
            result.second_round.append(outcome)
            if outcome.applied and (issue.rule_id or "") in GEOMETRIC:
                moved.add((issue.slide, issue.shape_id))
        presentation.save(str(result.output))
    except Exception:
        log.warning(
            "could not apply the second round to %s; the first round is "
            "applied and the file is written",
            result.output.name, exc_info=True,
        )
        return

    # Measure once more, so what is reported is the file as it now stands.
    _recheck(result, spec)
    result.settled = list(result.recheck)


def _recheck(result: ApplyResult, spec: Optional[Any]) -> None:
    """Measure the deck that was written, not the one that was measured.

    The report a designer ticks describes the ORIGINAL. By the time it has
    been applied and rebuilt onto the master, the deck is a different file:
    type is the master's size rather than the deck's, a box that shrank its
    text to fit no longer does, and a shape that cleared its neighbour by a
    hair no longer clears it. None of that is in the report, because none of
    it was true when the report was written.

    So the rules are run again on the output. Only the deterministic layer --
    the AI layer costs money and a second opinion on a file nobody has looked
    at yet is not worth it. What comes back is the honest state of the deck
    being sent, and `introduced` is the part that matters: what this run
    caused rather than what it inherited.

    Never fatal. The fixes are applied and the file is written by the time
    this runs; a rule that cannot read the output costs the recheck, not the
    work.
    """
    if spec is None:
        return
    from ..extract import read_deck            # noqa: PLC0415 - lazy, heavy
    from ..rules import RuleContext, build_default_rules, run_rules

    try:
        deck = read_deck(result.output)
        result.recheck = run_rules(
            RuleContext(deck=deck, spec=spec), build_default_rules()
        )
        # Findings arrive from the rules without one, and the second round
        # reports them by id the way every other outcome does.
        for issue in result.recheck:
            issue.id = issue.fingerprint()
        result.rechecked = True
    except Exception:
        log.warning(
            "could not re-check %s after applying; the fixes are applied and "
            "the file is written, but nothing has measured the result",
            result.output.name, exc_info=True,
        )


# --------------------------------------------------------------------------- #
# One finding
# --------------------------------------------------------------------------- #

def _apply_one(
    issue: Issue,
    presentation: Any,
    context: FixContext,
    in_breach: Optional[set] = None,
    moved: Optional[set] = None,
) -> FixOutcome:
    fixer = fixer_for(issue)
    if fixer is None:
        return FixOutcome(issue, False, f"needs a designer: {why_not_fixable(issue)}")

    if not issue.slide or not issue.shape:
        # A deck-level finding names no single shape because it is not about
        # one: "thirty-six shapes sit on a column of their own" is a fact
        # about the deck. Saying so beats the missing-shape message below,
        # which would blame the deck for having changed.
        return FixOutcome(
            issue,
            False,
            "needs a designer: this describes the deck as a whole, not one shape",
        )

    shape = _find_shape(presentation, issue)
    if shape is None:
        return FixOutcome(
            issue,
            False,
            f"could not find {issue.shape!r} on slide {issue.slide}; the deck "
            "has changed since the report was made",
        )

    if (issue.rule_id or "") in RELATIVE and (issue.slide, issue.shape_id) in (
        moved or set()
    ):
        # Not caution: arithmetic. This fix subtracts the drift the report
        # measured, and the shape is no longer where it was measured, so the
        # subtraction would land it somewhere neither the report nor the rule
        # ever named.
        return FixOutcome(
            issue,
            False,
            "left alone: another fix has already moved this shape in this run, "
            "and this one is measured from where it was. Re-run the check to "
            "see whether anything is still out of line",
        )

    geometric = is_geometric(issue)
    before = (shape.left, shape.top) if geometric else None
    neighbours = (
        _neighbours(presentation, issue, shape)
        if geometric or (issue.rule_id or "") in _NEEDS_NEIGHBOURS
        else []
    )
    covered_before = _covered(shape, neighbours) if geometric else {}
    partners = (
        [
            other
            for other in neighbours
            if (issue.slide, getattr(other, "shape_id", None)) not in (in_breach or set())
        ]
        if geometric
        else []
    )
    aligned_before = (
        _aligned(shape, partners, context.align_tolerance_in)
        if geometric
        else set()
    )

    context.neighbours = neighbours
    try:
        detail = fixer(shape, issue, context)
    except LeaveAlone as reason:
        return FixOutcome(issue, False, f"left alone: {reason}")
    except Exception as exc:
        # One fixer failing must not cost the others. The deck is still saved
        # with whatever did apply, and this finding is reported as skipped.
        log.exception("fixer for %s failed", issue.rule_id)
        return FixOutcome(issue, False, f"the fix failed: {exc}")

    if not detail:
        return FixOutcome(issue, False, "already correct, nothing to change")

    if geometric:
        # Alignment the shape already had, and would lose by moving.
        #
        # This is the guard that let `space.alignment_grid` be registered at
        # all. Its recorded failure was a section label snapped 0.20in onto a
        # grid line from the other half of the slide, away from the table it
        # captioned. Nothing about that move increases overlap, so the check
        # below could not see it; what it breaks is an alignment the label
        # already had with the table, and that is readable.
        #
        # It is also the hazard `space.satellite_offset` reports, so without
        # this an accepted fix could manufacture a finding for the next run.
        broken = aligned_before - _aligned(
            shape, partners, context.align_tolerance_in
        )
        if broken:
            # The set is what is out of place, so the set is what moves. A
            # column of shapes all sitting 0.44in outside the margin is not
            # eight findings about eight shapes; it is one column in the wrong
            # place, and moving any one of them in breaks the column.
            delta = (shape.left - before[0], shape.top - before[1])
            shape.left, shape.top = before
            move = (
                _cohort_move(shape, partners, neighbours, delta, context)
                if (issue.rule_id or "") in COHORT
                else None
            )
            if move is None:
                names = ", ".join(sorted(broken)[:3])
                return FixOutcome(
                    issue,
                    False,
                    f"left alone: moving it would break its alignment with {names}",
                )
            worse = _cohort_worsened(move, neighbours)
            if worse:
                move.revert()
                names = ", ".join(sorted(worse)[:3])
                return FixOutcome(
                    issue,
                    False,
                    "left alone: moving the set it belongs to would put it "
                    f"further over {names}",
                )
            aligned = (
                f", {move.aligned} of them onto the edge first" if move.aligned
                else ""
            )
            return FixOutcome(
                issue, True,
                f"{detail}, and the {move.carried} shape(s) aligned with it"
                f"{aligned}, so the set moves as one",
            )

        worse = _worsened(covered_before, _covered(shape, neighbours))
        if worse:
            # A slide is a composition. Satisfying a rule by pushing a shape
            # into the thing beside it trades a measurable finding for a
            # visible defect, which is the worse of the two and the one a
            # client sees. On a real deck this put a name over a portrait and
            # a footer line through another.
            #
            # Area, not just which neighbours are touched: a caption already
            # overlapping the photo above it gains no new neighbour by sliding
            # further under it, and that was exactly the move that buried the
            # name on the portrait.
            shape.left, shape.top = before
            names = ", ".join(sorted(worse)[:3])
            return FixOutcome(
                issue,
                False,
                f"left alone: moving it would put it further over {names}",
            )

    return FixOutcome(issue, True, detail)


# Fixes that read the shapes around the one they were given without moving it.
_NEEDS_NEIGHBOURS = frozenset({"space.series_crowded"})


def _margins_of(spec: Optional[Any]) -> Optional[dict[str, float]]:
    """The master's safe margins as plain inches, or None when it declares none."""
    margins = getattr(spec, "safe_margins", None)
    if margins is None:
        return None
    kept = {}
    for side in ("left", "top", "right", "bottom"):
        # `Margins` names its fields for the unit, and an edge it does not
        # declare is None rather than zero -- there is no sensible default
        # frame, so an unset edge simply does not constrain anything.
        value = getattr(margins, f"{side}_in", None)
        if value is not None:
            kept[side] = float(value)
    return kept or None


def _worsened(before: dict[str, int], after: dict[str, int]) -> set[str]:
    """Neighbours this shape now covers more of than it did.

    A hair of tolerance, because a move that satisfies a rule to the nearest
    EMU should not be rejected over a rounding artefact.
    """
    slack = int(0.001 * 914400 ** 2)
    return {
        name
        for name, area in after.items()
        if area > before.get(name, 0) + slack
    }


def _neighbours(presentation: Any, issue: Issue, shape: Any) -> list[Any]:
    """The other top-level shapes on the same slide.

    Top-level only, and never the moved shape's own descendants: a group
    carries its parts with it, so its own children can never be collided with.
    """
    slides = list(presentation.slides)
    if not issue.slide or not 1 <= issue.slide <= len(slides):
        return []
    own = _identities([shape])
    return [
        s for s in slides[issue.slide - 1].shapes
        if not (_identities([s]) & own)
    ]


def _identities(shapes: Any) -> set:
    """A shape and its descendants, identified by OOXML id.

    NOT by `id()`. python-pptx hands out a fresh proxy object every time a
    shape collection is walked, so the shape found for a finding and the same
    shape met again while listing its neighbours are two Python objects with
    two identities. Excluding by those excluded nothing, and the shape came
    out as its own neighbour -- invisible for a move, because covering
    yourself does not change when you move, and immediate for a resize, which
    grew its own self-overlap and was refused for landing on itself.
    """
    found = set()
    for shape in _walk(shapes):
        try:
            found.add(int(shape.shape_id))
        except Exception:
            found.add(id(shape))        # unidentifiable: at least be consistent
    return found


EMU_PER_INCH = 914400


def _aligned(shape: Any, neighbours: list[Any], tolerance_in: float) -> set[str]:
    """Neighbours this shape currently shares an edge with, by name and edge.

    Left, right and centre horizontally, top and bottom vertically. A caption
    under a table shares the table's left edge; a label beside a chart shares
    its top. Either is a relationship a designer made, and a move that ends it
    is a move that broke the slide even when nothing overlaps.
    """
    box = _box(shape)
    if box is None:
        return set()
    left, top, right, bottom = box
    mine = {
        "left": left, "right": right, "top": top, "bottom": bottom,
        "centre-x": (left + right) // 2, "centre-y": (top + bottom) // 2,
    }
    slack = int(tolerance_in * EMU_PER_INCH)
    out: set[str] = set()
    for other in neighbours:
        theirs = _box(other)
        if theirs is None:
            continue
        o_left, o_top, o_right, o_bottom = theirs
        edges = {
            "left": o_left, "right": o_right, "top": o_top, "bottom": o_bottom,
            "centre-x": (o_left + o_right) // 2,
            "centre-y": (o_top + o_bottom) // 2,
        }
        for edge, value in edges.items():
            if abs(mine[edge] - value) <= slack:
                out.add(f"{_name_of(other)} ({edge})")
    return out


def _covered(shape: Any, neighbours: list[Any]) -> dict[str, int]:
    """How much of each neighbour this shape's box currently covers."""
    box = _box(shape)
    if box is None:
        return {}
    covered: dict[str, int] = {}
    for other in neighbours:
        theirs = _box(other)
        if theirs is None:
            continue
        area = _intersection(box, theirs)
        if area:
            name = _name_of(other)
            covered[name] = covered.get(name, 0) + area
    return covered


def _box(shape: Any) -> Optional[tuple[int, int, int, int]]:
    left, top = _attr(shape, "left"), _attr(shape, "top")
    width, height = _attr(shape, "width"), _attr(shape, "height")
    if None in (left, top, width, height):
        return None
    return (left, top, left + width, top + height)


def _intersection(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    """Overlapping area of two boxes in EMU squared, 0 when they only touch.

    A shared edge is not a collision: shapes are routinely laid out flush.
    """
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return dx * dy if dx > 0 and dy > 0 else 0


def _find_shape(presentation: Any, issue: Issue) -> Optional[Any]:
    """The live shape a finding names, searched inside groups too.

    By OOXML id first. Shape names are not unique and in a real deck are
    wildly not unique -- sixteen shapes called "Pentagon 7" on one slide is a
    thing that happens -- so matching on the name would apply the fix to
    whichever of them came first. The id is unique within its slide.

    The name is the fallback, for a report written before ids were recorded.
    It is only trusted when exactly one shape on the slide carries it; an
    ambiguous name returns nothing and the finding is reported as skipped,
    because editing the wrong shape in a client deck is worse than editing
    none.
    """
    if not issue.slide:
        return None
    slides = list(presentation.slides)
    if not 1 <= issue.slide <= len(slides):
        return None
    shapes = list(_walk(slides[issue.slide - 1].shapes))

    if issue.shape_id is not None:
        for shape in shapes:
            if _attr(shape, "shape_id") == issue.shape_id:
                return shape
        return None

    if not issue.shape:
        return None
    named = [s for s in shapes if _attr(s, "name") == issue.shape]
    return named[0] if len(named) == 1 else None


def _attr(shape: Any, name: str) -> Any:
    try:
        return getattr(shape, name)
    except Exception:
        return None


def _name_of(shape: Any) -> str:
    return str(_attr(shape, "name") or "an unnamed shape")


def _walk(shapes: Any) -> Iterable[Any]:
    for shape in shapes:
        yield shape
        try:
            children = shape.shapes
        except Exception:
            continue
        yield from _walk(children)


def _wanted(
    issues: Sequence[Issue], selected: Optional[Iterable[str]]
) -> list[Issue]:
    """The findings to act on, in report order.

    An id that matches nothing is an error rather than a silent no-op: it
    means the report and the selection have come apart, and quietly applying
    the rest would hide that.
    """
    if selected is None:
        return fixable(issues)

    wanted = list(dict.fromkeys(selected))
    by_id = {issue.id: issue for issue in issues if issue.id}
    unknown = [key for key in wanted if key not in by_id]
    if unknown:
        raise ApplyError(
            f"no finding with id {', '.join(unknown)} in this report. "
            "Ids come from the report the deck was checked against; re-run "
            "validate if the deck has changed."
        )
    return [by_id[key] for key in wanted]


def _rebuild_in_place(
    master: str | Path, target: Path, tuning: Optional[RuleTuning]
) -> Any:
    """Rebuild the corrected deck onto the master, over the same output path.

    Done through a temporary file because the rebuild reads its source while
    writing its target, and those cannot be the same path.
    """
    from ..rebuild import rebuild  # noqa: PLC0415 - avoids a circular import

    staged = target.with_suffix(".prefix.pptx")
    shutil.move(str(target), str(staged))
    try:
        return rebuild(master, staged, target, tuning=tuning)
    finally:
        staged.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Moving a set rather than a member
# --------------------------------------------------------------------------- #
#
# The refusal this exists to answer, from a real deck: 37 of 50 safe-margin
# fixes came back "moving it would break its alignment with ...". Not one of
# them was wrong. Slide 1 carried a column of shapes all sitting at left
# 0.48in against a 0.92in margin, so every one of them was 0.44in outside it,
# and moving any single one in would have broken the column.
#
# The set is what is out of place, so the set is what moves. Every shape
# sharing the edge travels the same distance -- the move a designer makes by
# selecting the column and nudging it, and the one that keeps the column a
# column.
#
# Members slightly off the shared edge are put ON it first. Alignment within
# tolerance is not alignment, it is drift a tolerance forgave, and carrying it
# along would preserve it at the new position forever. So the set is aligned,
# then moved: two steps, in that order, because moving a ragged set only
# relocates the raggedness.
#
# Shapes no finding named do move here, which is a departure and a deliberate
# one. A column half corrected is worse than a column uncorrected. Every shape
# that moves is checked against its own neighbours before the move is allowed
# to stand, which is the same check a single move has always had.


@dataclass
class CohortMove:
    """A set of shapes moved together, and what it takes to put them back."""

    shapes: list[Any]
    origin: dict[int, tuple[int, int]]
    covered: dict[int, dict[str, int]]      # what each covered before it moved
    aligned: int = 0                        # how many were snapped onto the edge

    def revert(self) -> None:
        for shape in self.shapes:
            position = self.origin.get(id(shape))
            if position is not None:
                shape.left, shape.top = position

    @property
    def carried(self) -> int:
        """Shapes that came along, not counting the one the finding named."""
        return max(0, len(self.shapes) - 1)


def _cohort_move(
    shape: Any,
    partners: list[Any],
    neighbours: list[Any],
    delta: tuple[int, int],
    context: FixContext,
) -> Optional[CohortMove]:
    """Align the set this shape belongs to, then move all of it by `delta`.

    None when there is no set to move, which leaves the caller to refuse the
    finding exactly as it did before this existed.
    """
    dx, dy = delta
    if not dx and not dy:
        return None

    members = _sharing_an_edge(shape, partners, context.align_tolerance_in, dx, dy)
    if not members:
        return None

    shapes = [shape] + [other for other, _edge in members]
    carried = _identities(shapes)
    move = CohortMove(
        shapes=shapes,
        origin={id(s): (s.left, s.top) for s in shapes},
        covered={
            s.shape_id: _covered(
                s, [n for n in neighbours if not (_identities([n]) & carried)]
            )
            for s in shapes
        },
    )

    try:
        for other, edge in members:
            if _align_to(other, shape, edge):
                move.aligned += 1
        for other in shapes:
            other.left = (other.left or 0) + dx
            other.top = (other.top or 0) + dy
    except Exception:
        move.revert()
        log.debug("could not move a set of shapes together", exc_info=True)
        return None
    return move


def _cohort_worsened(
    move: CohortMove, neighbours: list[Any]
) -> set[str]:
    """Neighbours any of the moved shapes now covers more of.

    Every shape that moved, not only the one the finding named: a shape
    dragged along by its column can land on something just as easily as the
    one that was asked to move.
    """
    carried = _identities(move.shapes)
    outside = [n for n in neighbours if not (_identities([n]) & carried)]
    worse: set[str] = set()
    for shape in move.shapes:
        worse |= _worsened(
            move.covered.get(shape.shape_id, {}), _covered(shape, outside)
        )
    return worse


def _sharing_an_edge(
    shape: Any, partners: list[Any], tolerance_in: float, dx: int, dy: int
) -> list[tuple[Any, str]]:
    """The partners this shape is aligned with, on the axis it is moving along.

    Only the moving axis: a horizontal nudge cannot break a shared top edge,
    so a shape sharing only that has no business being dragged sideways.
    """
    box = _box(shape)
    if box is None:
        return []
    left, top, right, bottom = box
    wanted: dict[str, int] = {}
    if dx:
        wanted.update(
            {"left": left, "right": right, "centre-x": (left + right) // 2}
        )
    if dy:
        wanted.update(
            {"top": top, "bottom": bottom, "centre-y": (top + bottom) // 2}
        )

    slack = int(tolerance_in * EMU_PER_INCH)
    found: list[tuple[Any, str]] = []
    for other in partners:
        theirs = _box(other)
        if theirs is None:
            continue
        o_left, o_top, o_right, o_bottom = theirs
        edges = {
            "left": o_left, "right": o_right, "centre-x": (o_left + o_right) // 2,
            "top": o_top, "bottom": o_bottom, "centre-y": (o_top + o_bottom) // 2,
        }
        for edge, value in wanted.items():
            if abs(edges[edge] - value) <= slack:
                found.append((other, edge))
                break
    return found


def _align_to(other: Any, shape: Any, edge: str) -> bool:
    """Put `other` exactly on the edge it already nearly shares with `shape`.

    True when that actually moved it, which is what makes the difference
    between a set that was aligned and one that was merely within tolerance
    of being aligned.
    """
    box, theirs = _box(shape), _box(other)
    if box is None or theirs is None:
        return False
    left, top, right, bottom = box
    o_left, o_top, o_right, o_bottom = theirs
    width, height = o_right - o_left, o_bottom - o_top
    was = (other.left, other.top)

    if edge == "left":
        other.left = left
    elif edge == "right":
        other.left = right - width
    elif edge == "centre-x":
        other.left = ((left + right) // 2) - width // 2
    elif edge == "top":
        other.top = top
    elif edge == "bottom":
        other.top = bottom - height
    elif edge == "centre-y":
        other.top = ((top + bottom) // 2) - height // 2
    return (other.left, other.top) != was
