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

from ..models import Issue, RuleTuning
from .fixers import GEOMETRIC, LeaveAlone, fix_order, fixer_for, why_not_fixable

log = logging.getLogger(__name__)


class ApplyError(RuntimeError):
    """Raised when the fixes cannot be applied or the result cannot be written."""


@dataclass
class FixContext:
    """What a fixer needs to know about the deck it is working on."""

    width_emu: int
    height_emu: int


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

    @property
    def changed(self) -> int:
        return len(self.applied)


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
    )
    result = ApplyResult(deck=deck.name, output=out)

    # Stable sort, so findings of equal priority stay in report order and a
    # run is reproducible.
    for issue in sorted(wanted, key=fix_order):
        result_line = _apply_one(issue, presentation, context)
        (result.applied if result_line.applied else result.skipped).append(result_line)

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
    return result


# --------------------------------------------------------------------------- #
# One finding
# --------------------------------------------------------------------------- #

def _apply_one(issue: Issue, presentation: Any, context: FixContext) -> FixOutcome:
    fixer = fixer_for(issue)
    if fixer is None:
        return FixOutcome(issue, False, f"needs a designer: {why_not_fixable(issue)}")

    shape = _find_shape(presentation, issue)
    if shape is None:
        return FixOutcome(
            issue,
            False,
            f"could not find {issue.shape!r} on slide {issue.slide}; the deck "
            "has changed since the report was made",
        )

    geometric = (issue.rule_id or "") in GEOMETRIC
    before = (shape.left, shape.top) if geometric else None
    neighbours = _neighbours(presentation, issue, shape) if geometric else []
    covered_before = _covered(shape, neighbours) if geometric else {}

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
    own = {id(s) for s in _walk([shape])}
    return [s for s in slides[issue.slide - 1].shapes if id(s) not in own]


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
