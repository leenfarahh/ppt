"""Sets of identical shapes that do not sit consistently.

A consulting deck is full of repeated furniture: five process steps, three
column headers, eleven numbered badges. They are drawn once and duplicated, so
they are pixel-identical in size and fill, and a designer places them by eye.
One that ends up a fraction out of line is the single most common visible
defect in a real deck and no other rule here catches it:

- `space.alignment_grid` needs four shapes sharing a rounded left edge before
  it believes in a grid line at all, and only reports a miss *within* four
  tolerances of one. A badge 0.2in adrift is "deliberate" to that rule.
- `space.overlap` and `space.off_canvas` only see hard failures.

The signal used here is the set itself. Shapes of identical size that carry the
same kind of content are a series, and a series has an arrangement: a row, a
column, or a grid. Whatever the rest of the series does is the intent, and the
member that departs from it is the finding.

What this deliberately does not do is invent an arrangement for shapes laid out
on a curve or a ring. Eleven badges around a hexagon are a series with no row,
no column and no even spacing to measure, and the member that looks wrong there
looks wrong to an eye rather than to a ruler. That case belongs to the render
pass, not here, and reporting a guess about it would be worse than silence.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Iterable

from ..models import Category, Issue, Severity, ShapeProfile, SlideProfile
from .base import Rule, RuleContext


class RepeatedElementRule(Rule):
    """One of a set of identical shapes is out of line with the rest."""

    id = "space.repeat_out_of_line"
    category = Category.SPACE
    description = "One of a set of repeated shapes is out of line with the rest."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tuning = ctx.tuning
        for slide in ctx.deck.slides:
            if slide.hidden:
                continue
            for series in _series(slide, tuning.repeat_min_members):
                yield from self._check_series(ctx, slide, series)

    def _check_series(
        self, ctx: RuleContext, slide: SlideProfile, series: list[ShapeProfile]
    ) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.position_in

        row = _shared_edge([s.geometry.top_in for s in series], tolerance)
        column = _shared_edge([s.geometry.left_in for s in series], tolerance)

        # A row and a column at once is a grid, where neither edge is the
        # series' arrangement and both are legitimate. Nothing to say.
        if row is not None and column is not None:
            return
        if row is not None:
            yield from self._off_edge(ctx, slide, series, row, "top")
        elif column is not None:
            yield from self._off_edge(ctx, slide, series, column, "left")

    def _off_edge(
        self,
        ctx: RuleContext,
        slide: SlideProfile,
        series: list[ShapeProfile],
        edge: float,
        side: str,
    ) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.position_in
        size = f"{series[0].geometry.width_in:.2f} x {series[0].geometry.height_in:.2f}in"

        ceiling = ctx.tuning.repeat_max_drift_in
        for shape in series:
            value = getattr(shape.geometry, f"{side}_in")
            drift = abs(value - edge)
            if drift <= tolerance:
                continue
            if drift > ceiling:
                # Not out of line, somewhere else. A shape that far from the
                # series was put there, and calling that a misalignment would
                # report every deliberate layout in the deck.
                continue
            yield self.issue(
                f"One of {len(series)} identical shapes is {drift:.2f}in off the "
                f"{side} edge the other {len(series) - 1} share.",
                slide=slide,
                shape=shape,
                expected=f"{side} {edge:.2f}in, as the rest of the set",
                found=f"{side} {value:.2f}in",
                suggestion=(
                    f"Select all {len(series)} shapes of {size} and align them "
                    f"on the {side} edge."
                ),
            )


# --------------------------------------------------------------------------- #
# Finding the series
# --------------------------------------------------------------------------- #

def _series(slide: SlideProfile, minimum: int) -> Iterable[list[ShapeProfile]]:
    """Groups of shapes that are the same thing repeated.

    Grouped on rounded size and shape type together. Size alone would put a
    row of icons and a row of same-sized text boxes in one series; type alone
    would group every rectangle on a busy slide, whatever its dimensions.

    Only top-level shapes. A grouped diagram is placed as one object, and its
    parts are positioned relative to each other by whoever drew it, so holding
    them to a shared edge would report the drawing rather than a defect.
    """
    buckets: dict[tuple, list[ShapeProfile]] = defaultdict(list)
    for shape in slide.shapes:
        if shape.is_group or not shape.geometry.width_in:
            continue
        key = (
            round(shape.geometry.width_in, 2),
            round(shape.geometry.height_in, 2),
            shape.shape_type,
        )
        buckets[key].append(shape)

    for members in buckets.values():
        if len(members) >= minimum:
            yield members


def _shared_edge(values: list[float], tolerance: float) -> float | None:
    """The edge most of the series agrees on, or None if there is no agreement.

    A majority has to share it, and at least one member has to depart from it:
    a series that already lines up perfectly has no finding, and a series
    scattered with no majority has no intent to measure against.
    """
    rounded = [round(v / tolerance) for v in values]
    counts: dict[int, int] = defaultdict(int)
    for value in rounded:
        counts[value] += 1

    best, agreeing = max(counts.items(), key=lambda item: item[1])
    # A clear majority, not a bare one. Two of three agreeing is not a series
    # with one member out of line, it is three shapes in two places.
    if agreeing < max(3, 0.6 * len(values)) or agreeing == len(values):
        return None
    return median([v for v, r in zip(values, rounded) if r == best])
