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

Shapes laid out on a curve or a ring have no row, no column and no even
spacing, so `RepeatedElementRule` has nothing to measure them against and says
nothing. That used to be the end of it: eleven badges around a hexagon were
left to the render pass, on the grounds that the odd one out looks wrong to an
eye rather than to a ruler.

That was half right. There is no arrangement *on the slide* to measure, but
each badge sits on a hexagon, and the offset from one to the other is rigidly
constant however the pair is placed. `SatelliteOffsetRule` measures in that
frame instead. On a real deck the ring gave ten badges agreeing on their offset
to within 0.02in and one sitting 0.198in high, which is a ruler's answer to a
question that looked like an eye's.

The render pass was tried on the same slide and is not a substitute: across
twenty-seven runs, with prompts up to and including one that named the defect
type and told the model to ignore the mirroring, it identified the right badge
about a fifth of the time and named an innocent one more often than the guilty
one. Sub-tenth-of-an-inch placement has a numeric answer and belongs here.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Iterable

from ..models import (
    Category,
    Issue,
    Severity,
    ShapeProfile,
    SlideProfile,
    walk_shapes,
)
from .base import Rule, RuleContext
from .series import series_of as _series


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

        majority = ctx.tuning.majority_fraction
        row = _shared_edge([s.geometry.top_in for s in series], tolerance, majority)
        column = _shared_edge(
            [s.geometry.left_in for s in series], tolerance, majority
        )

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
# Rows and columns inside a set
# --------------------------------------------------------------------------- #
#
# The blind spot these close. Every other space rule models a set as shapes
# sharing ONE edge: `space.repeat_out_of_line` finds a series and asks which
# left or top edge the majority agrees on. Two columns of five have two lefts
# and five tops, so no edge is shared, nothing is out of line with anything,
# and a shape 0.08in low is invisible. Measured on a deck built for it, both
# that layout and an eleven-node ring with one node low produced not a single
# space finding.
#
# So a set is read as rows and columns instead of as one edge, and the two
# rules below split on how much the geometry can actually settle:
#
#   3 or more in a row   a majority exists, the odd one out is the defect, and
#                        the fix is to put it back on the row.
#   exactly 2            no majority. They disagree and the geometry cannot say
#                        which of them moved, so both are named and a designer
#                        decides. This is the ring case, where every pair is a
#                        pair.
#
# The clustering window is wider than the reporting tolerance on purpose.
# Shapes within `near_miss_factor` of each other are one row -- that is what
# that tuning value means -- and inside a row anything past `position_in` is
# drift. Between 0.05in and 0.20in is a defect; past that it is another row.


class SeriesRowRule(Rule):
    """A member of a repeated set sitting off the row or column it belongs to."""

    id = "space.row_out_of_line"
    category = Category.SPACE
    description = "One of a set of repeated shapes is off the row or column it sits in."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            if slide.hidden:
                continue
            for series in _series(slide, ctx.tuning.repeat_min_members):
                yield from self._lines(ctx, slide, series, "y")
                yield from self._lines(ctx, slide, series, "x")

    def _lines(
        self,
        ctx: RuleContext,
        slide: SlideProfile,
        series: list[ShapeProfile],
        axis: str,
    ) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.position_in
        window = tolerance * ctx.tuning.near_miss_factor
        centres = [_centre(s)[0 if axis == "x" else 1] for s in series]
        groups = _cluster_1d(centres, window)

        # A row of two has no majority: both members are equally far from the
        # midpoint between them, so this rule cannot say which moved.
        # `MirroredPairRule` reports those, naming both.
        settled = [g for g in groups if len(g) >= _MAJORITY_NEEDS]
        if not settled:
            return
        # A group holding the whole series is the case
        # `space.repeat_out_of_line` already reports, and reporting it twice
        # helps nobody.
        for group in settled:
            if len(group) == len(series):
                continue
            yield from self._off_line(ctx, slide, series, centres, group, axis)

    def _off_line(
        self,
        ctx: RuleContext,
        slide: SlideProfile,
        series: list[ShapeProfile],
        centres: list[float],
        group: list[int],
        axis: str,
    ) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.position_in
        line = median([centres[i] for i in group])
        word = "column" if axis == "x" else "row"
        edge = "centre x" if axis == "x" else "centre y"

        for index in group:
            drift = abs(centres[index] - line)
            if drift <= tolerance:
                continue
            shape = series[index]
            yield self.issue(
                f"One of {len(group)} shapes in this {word} is {drift:.2f}in "
                f"off the {edge} the other {len(group) - 1} share.",
                slide=slide,
                shape=shape,
                expected=f"{edge} {line:.2f}in, as the rest of the {word}",
                found=f"{edge} {centres[index]:.2f}in",
                suggestion=(
                    f"Select the {len(group)} shapes in this {word} and align "
                    f"them{' vertically' if axis == 'x' else ' horizontally'}."
                ),
            )


# Three, because two is a disagreement and three is a majority with an
# exception. Everything this rule says depends on there being a right answer
# among the members, and two members never produce one.
_MAJORITY_NEEDS = 3


class MirroredPairRule(Rule):
    """Two shapes placed as a mirrored pair that do not sit level.

    The arrangement in a radial diagram: eleven nodes around a centre, pairing
    across the vertical axis, each pair meant to sit at one height. Also the
    plainer case of two columns, where every row is a pair.

    Reported as a pair rather than as a shape, and left for a designer, which
    is the honest answer rather than a cautious one: the two disagree about a
    height and nothing in the geometry says which of them moved. Moving both
    to the midpoint would level the pair and put both of them 0.04in off the
    arrangement they belong to, which is a worse deck than the one that came
    in.
    """

    id = "space.mirror_pair_offset"
    category = Category.SPACE
    description = "Two shapes mirrored across the slide do not sit level."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            if slide.hidden:
                continue
            for series in _series(slide, ctx.tuning.repeat_min_members):
                if len(series) < _MIRROR_NEEDS:
                    continue
                yield from self._pairs(ctx, slide, series)

    def _pairs(
        self, ctx: RuleContext, slide: SlideProfile, series: list[ShapeProfile]
    ) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.position_in
        window = tolerance * ctx.tuning.near_miss_factor
        centres = [_centre(s) for s in series]
        axis = median([c[0] for c in centres])

        pairs = _mirrored_pairs(centres, axis, window)
        # Most of the set has to pair before the arrangement is a mirror at
        # all. Two shapes either side of the middle of a scattered slide are a
        # coincidence, and reporting one would put a finding on every busy
        # slide in the deck.
        paired = {i for pair in pairs for i in pair}
        if len(paired) < ctx.tuning.majority_fraction * len(series):
            return

        ceiling = ctx.tuning.repeat_max_drift_in
        for left, right in pairs:
            gap = abs(centres[left][1] - centres[right][1])
            if gap <= tolerance or gap > ceiling:
                continue
            first, second = series[left], series[right]
            yield self.issue(
                f"{first.name!r} and {second.name!r} are a mirrored pair across "
                f"the arrangement's axis at {axis:.2f}in, but sit {gap:.2f}in "
                "apart vertically.",
                slide=slide,
                shape=first,
                expected=f"both at one centre y, level with {second.name!r}",
                found=(
                    f"{centres[left][1]:.2f}in and {centres[right][1]:.2f}in"
                ),
                suggestion=(
                    f"Select {first.name!r} and {second.name!r} and align them "
                    "middle. Which of the two moved is not in the geometry, so "
                    "check against the rest of the arrangement before nudging."
                ),
            )


# A mirror needs enough members to be a pattern rather than a coincidence:
# two pairs, so that one pair can be wrong while the arrangement still reads
# as mirrored.
_MIRROR_NEEDS = 4


def _cluster_1d(values: list[float], window: float) -> list[list[int]]:
    """Group values that sit within `window` of each other, as indices.

    Single-link on a sorted list, which is what "these are the same row" means
    and cannot produce the order-dependent groupings that comparing against a
    running median would.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    groups: list[list[int]] = []
    for index in order:
        if groups and values[index] - values[groups[-1][-1]] <= window:
            groups[-1].append(index)
        else:
            groups.append([index])
    return groups


def _mirrored_pairs(
    centres: list[tuple[float, float]], axis: float, window: float
) -> list[tuple[int, int]]:
    """Members that reflect onto each other across `axis`, each used once.

    Nearest reflection first, so a shape sitting almost on the axis does not
    claim a partner that belongs to somebody else.

    A shape ON the axis has no partner, and saying so is what stops the whole
    degenerate class this rule would otherwise be full of. A column of six
    identical boxes all share a centre x, that centre x is the axis, and every
    one of them reflects onto every other one for nothing: on a real deck that
    produced eight findings about a vertical stack that is not mirrored at
    all. A pair has to straddle the axis, one member each side of it.
    """
    candidates = []
    for i, (x, _y) in enumerate(centres):
        if abs(x - axis) <= window:
            continue
        for j in range(i + 1, len(centres)):
            other = centres[j][0]
            if abs(other - axis) <= window:
                continue
            if (x - axis) * (other - axis) >= 0:
                continue        # same side of the axis: not a reflection
            miss = abs((axis - x) + (axis - other))
            if miss <= window:
                candidates.append((miss, i, j))

    taken: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _miss, i, j in sorted(candidates):
        if i in taken or j in taken:
            continue
        taken.update((i, j))
        pairs.append((i, j))
    return pairs


# --------------------------------------------------------------------------- #
# Finding the series
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Satellites: a repeated shape measured against the partner it travels with
# --------------------------------------------------------------------------- #

class SatelliteOffsetRule(Rule):
    """A repeated shape sits differently on its partner than its copies do."""

    id = "space.satellite_offset"
    category = Category.SPACE
    description = (
        "One copy of a repeated pairing sits differently than the other copies."
    )
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tuning = ctx.tuning
        for slide in ctx.deck.slides:
            if slide.hidden:
                continue
            # One finding per shape. A badge inside a hexagon inside an icon
            # belongs to two valid pairings and would otherwise be reported
            # once per partner series, which reads as two defects.
            best: dict[int, tuple[float, Issue]] = {}
            for issue, shape_id, drift in self._findings(ctx, slide, tuning):
                if shape_id not in best or drift > best[shape_id][0]:
                    best[shape_id] = (drift, issue)
            for _drift, issue in best.values():
                yield issue

    def _findings(self, ctx: RuleContext, slide: SlideProfile, tuning):
        # A cohort, plus at least one member departing from it: the smallest
        # set that can show a majority and a minority at all. Dropping the
        # series below it is pruning rather than a check -- the cohort floor
        # downstream is what actually enforces this, and would reach the same
        # verdict on its own -- but it is worth doing, because every surviving
        # series is compared against every other. On one real deck it took the
        # cross-comparisons from 445 to 56.
        smallest = tuning.satellite_cohort_min + 1
        groups = _size_groups(
            list(walk_shapes(slide.shapes)), smallest, tuning.satellite_colocated_in
        )
        keys = sorted(groups)
        for i, key_a in enumerate(keys):
            for key_b in keys[i + 1:]:
                yield from self._compare(
                    ctx, slide, groups[key_a], groups[key_b], tuning
                )

    def _compare(self, ctx, slide, satellites, partners, tuning):
        pairs = _pair_up(satellites, partners, tuning.satellite_reach)

        offsets = [
            (
                _centre(sat)[0] - _centre(partner)[0],
                _centre(sat)[1] - _centre(partner)[1],
            )
            for sat, partner in pairs
        ]
        cohorts = [
            group
            for group in _cluster(offsets, tuning.satellite_cluster_in)
            if len(group) >= tuning.satellite_cohort_min
        ]
        # Without a majority sitting in agreement there is no intended offset,
        # only a scatter, and a scatter has no odd one out.
        settled = sum(len(g) for g in cohorts)
        if not cohorts or settled < tuning.majority_fraction * len(offsets):
            return

        in_cohort = {index for group in cohorts for index in group}
        for index, (satellite, partner) in enumerate(pairs):
            if index in in_cohort:
                continue
            expected = min(
                (_median_of(offsets, g) for g in cohorts),
                key=lambda e: (e[0] - offsets[index][0]) ** 2
                + (e[1] - offsets[index][1]) ** 2,
            )
            dx = offsets[index][0] - expected[0]
            dy = offsets[index][1] - expected[1]
            drift = (dx * dx + dy * dy) ** 0.5
            if drift <= ctx.spec.tolerances.position_in:
                continue
            if drift > ctx.tuning.repeat_max_drift_in:
                # The same ceiling `_off_edge` uses, for the same reason. A
                # satellite four inches from where its cohort sits was not
                # nudged, it is part of a different arrangement that happens to
                # reuse the shape: one slide paired a hero image with the row
                # of thumbnails below it and called the hero a 3.59in drift.
                continue
            agreeing = max(len(g) for g in cohorts)
            # The partner is named by size, not by shape name. Real decks reuse
            # one name across a whole slide ("Text Placeholder 30" twice over,
            # "Google Shape;2547;..." eleven times), so a name identifies
            # nothing and can even repeat the subject back at the reader.
            size = (
                f"{partner.geometry.width_in:.2f} x "
                f"{partner.geometry.height_in:.2f}in"
            )
            yield (
                self.issue(
                    f"This shape sits {_describe(dx, dy)} from where the other "
                    f"{agreeing} copies of it sit on their {size} partner.",
                    slide=slide,
                    shape=satellite,
                    expected=(
                        f"offset {expected[0]:+.2f}, {expected[1]:+.2f}in from the "
                        f"{size} shape it pairs with, as the other {agreeing} have"
                    ),
                    found=(
                        f"offset {offsets[index][0]:+.2f}, "
                        f"{offsets[index][1]:+.2f}in"
                    ),
                    suggestion=(
                        "Copy a correct pair, or nudge this one back by "
                        f"{_describe(-dx, -dy)}."
                    ),
                ),
                satellite.shape_id,
                drift,
            )


def _size_groups(shapes: list[ShapeProfile], minimum: int, colocated_in: float):
    """Repeated shapes, bucketed the way `_series` buckets them.

    Co-located duplicates collapse to one member first. Decks routinely stack a
    filled shape and its glyph in the same box, and counting both makes a set of
    five icons look like ten, which starves the pairing below of partners and
    scrambles the assignment.
    """
    buckets: dict[tuple, list[ShapeProfile]] = defaultdict(list)
    for shape in shapes:
        if shape.is_group or not shape.geometry.width_in or not shape.geometry.height_in:
            continue
        buckets[
            (
                round(shape.geometry.width_in, 2),
                round(shape.geometry.height_in, 2),
                shape.shape_type,
            )
        ].append(shape)

    groups = {}
    for key, members in buckets.items():
        deduped: list[ShapeProfile] = []
        for shape in members:
            here = _centre(shape)
            if any(
                abs(here[0] - _centre(kept)[0]) < colocated_in
                and abs(here[1] - _centre(kept)[1]) < colocated_in
                for kept in deduped
            ):
                continue
            deduped.append(shape)
        if len(deduped) >= minimum:
            groups[key] = deduped
    return groups


def _pair_up(satellites, partners, reach_factor: float):
    """Match each satellite to its own nearest partner, one for one.

    Nearest-neighbour rather than group membership, because the grouping in a
    real deck is not reliable: on the deck this rule was written against, one
    badge sat at the top level, another two levels down inside nested groups,
    and a third was grouped with its hexagon. Proximity is what the pairing
    actually means, and it is stated the same way everywhere.
    """
    if not satellites or not partners:
        return []

    # Non-zero: _size_groups drops shapes with no width or height.
    box = partners[0].geometry
    reach = reach_factor * (box.width_in ** 2 + box.height_in ** 2) ** 0.5

    candidates = []
    for s_index, satellite in enumerate(satellites):
        for p_index, partner in enumerate(partners):
            gap = (
                (_centre(satellite)[0] - _centre(partner)[0]) ** 2
                + (_centre(satellite)[1] - _centre(partner)[1]) ** 2
            ) ** 0.5
            if gap <= reach:
                candidates.append((gap, s_index, p_index))

    # Shortest gaps claimed first, so an unambiguous pair is never stolen by a
    # distant one that happened to be considered earlier.
    candidates.sort()
    taken_s: set[int] = set()
    taken_p: set[int] = set()
    pairs = []
    for _gap, s_index, p_index in candidates:
        if s_index in taken_s or p_index in taken_p:
            continue
        taken_s.add(s_index)
        taken_p.add(p_index)
        pairs.append((satellites[s_index], partners[p_index]))
    return pairs


def _cluster(offsets: list[tuple[float, float]], tolerance: float) -> list[list[int]]:
    """Group offsets that agree, as indices into `offsets`.

    A mirrored layout produces two groups and both are intended, so this looks
    for agreement rather than for a single majority.
    """
    groups: list[list[int]] = []
    for index, offset in enumerate(offsets):
        for group in groups:
            centre = _median_of(offsets, group)
            if (
                abs(offset[0] - centre[0]) <= tolerance
                and abs(offset[1] - centre[1]) <= tolerance
            ):
                group.append(index)
                break
        else:
            groups.append([index])
    return groups


def _median_of(offsets, indices) -> tuple[float, float]:
    return (
        median([offsets[i][0] for i in indices]),
        median([offsets[i][1] for i in indices]),
    )


def _centre(shape: ShapeProfile) -> tuple[float, float]:
    box = shape.geometry
    return (box.left_in + box.width_in / 2, box.top_in + box.height_in / 2)


# Inches are reported to this many decimals throughout, so anything below half
# of the last place would print as 0.00in and is left out of the wording.
_DECIMALS = 2
_SHOWN_IN = 0.5 * 10 ** -_DECIMALS


def _describe(dx: float, dy: float) -> str:
    """The drift in words, because "0.20in up" is actionable and a vector is not."""
    parts = []
    if abs(dx) >= _SHOWN_IN:
        parts.append(f"{abs(dx):.{_DECIMALS}f}in {'right' if dx > 0 else 'left'}")
    if abs(dy) >= _SHOWN_IN:
        parts.append(f"{abs(dy):.{_DECIMALS}f}in {'down' if dy > 0 else 'up'}")
    return " and ".join(parts) or f"less than {10 ** -_DECIMALS:.{_DECIMALS}f}in"


def _shared_edge(
    values: list[float], tolerance: float, majority: float
) -> float | None:
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
    if agreeing < max(3, majority * len(values)) or agreeing == len(values):
        return None
    return median([v for v, r in zip(values, rounded) if r == best])
