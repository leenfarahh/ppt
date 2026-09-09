"""Presentation space rules: safe margins, bleed, overlap, alignment.

"Presentation space" in the workflow means the usable area of the slide and how
the content sits inside it -- content outside the safe margins, running off the
canvas, colliding with other shapes, or misaligned against a grid the rest of
the deck follows.
"""

from __future__ import annotations

from collections import Counter
from math import ceil
from typing import Iterable, Optional

from ..linemetrics import LineMetricsProvider, NullLineMetrics, ShapeKey
from ..models import Category, Geometry, Issue, Margins, Severity, SlideProfile, TextRole

# How a grid finding says its lines came from the master rather than from
# the deck under audit. A constant because the fixer keys on it: only a
# declared grid is safe to snap to, and a phrase drifting on one side of
# that would silently disable or silently enable the fix.
DECLARED_GRID = "the master declares"
from .base import Rule, RuleContext
from .repeats import _series as _series_of


class OffCanvasRule(Rule):
    """Content that runs off the slide."""

    id = "space.off_canvas"
    category = Category.SPACE
    description = "Shape extends beyond the slide edge."
    default_severity = Severity.BLOCKER

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        width, height = ctx.deck.width_in, ctx.deck.height_in
        # Full-bleed images are meant to reach the edge; a shape covering most
        # of the canvas is intentional bleed, not a defect.
        coverage = ctx.tuning.bleed_coverage

        for slide, shape in ctx.shapes():
            box = shape.geometry
            if self._is_bleed(box, width, height, coverage):
                continue
            overhang = max(
                -box.left_in,
                -box.top_in,
                box.right_in - width,
                box.bottom_in - height,
            )
            if overhang > ctx.spec.tolerances.position_in:
                yield self.issue(
                    f"Shape extends {overhang:.2f}in past the slide edge.",
                    slide=slide,
                    shape=shape,
                    expected=f"inside 0-{width:.2f} x 0-{height:.2f}in",
                    found=(
                        f"{box.left_in:.2f}, {box.top_in:.2f} to "
                        f"{box.right_in:.2f}, {box.bottom_in:.2f}in"
                    ),
                )

    @staticmethod
    def _is_bleed(
        box: Geometry, width: float, height: float, coverage: float
    ) -> bool:
        area = max(box.width_in * box.height_in, 0.0)
        return area >= coverage * width * height


class SafeMarginRule(Rule):
    """Content inside the safe margin, where it crowds the frame."""

    id = "space.safe_margin"
    category = Category.SPACE
    description = "Content crosses the safe margin."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        # The spec's margins, not the brand file's: authored where stated, read
        # off the master's layouts where not. Requiring a brand file meant this
        # never ran on a master-only run, which is most of them.
        deck_wide = ctx.spec.safe_margins
        width, height = ctx.deck.width_in, ctx.deck.height_in
        tolerance = ctx.spec.tolerances.position_in

        # Resolved once per layout, not once per shape: the lookup walks every
        # shape in the layout, and a 200-slide deck asks thousands of times.
        frames: dict[Optional[str], Optional[Margins]] = {}

        for slide, shape in ctx.shapes():
            # Only text is judged against the safe margin; a background panel
            # or bleed image is supposed to cross it.
            if not shape.paragraphs or not shape.text.strip():
                continue
            # Footers, page numbers and dates live in the margin by design.
            # The frame is derived from the content placeholders precisely so
            # that it describes where body copy belongs, and holding the
            # furniture to it would report every slide in every deck.
            if shape.role is TextRole.FOOTER:
                continue
            # Per slide, against the layout it is on. A two-column layout
            # offers a different area than a full-width one, and one frame for
            # the whole master cannot say so: it has to take the roomiest edge
            # any layout offers, which is the loosest of them everywhere.
            #
            # Only where the layout marks its presentation space, though. A
            # layout that marks none falls back to the deck-wide frame rather
            # than to its own placeholders, which would be a much stricter
            # reading than the one this rule has always applied.
            if slide.layout_name not in frames:
                frames[slide.layout_name] = self._frame_for(ctx, slide)
            margins = frames[slide.layout_name] or deck_wide
            box = shape.geometry
            # The same exemption by geometry, for furniture that carries no
            # footer role: a date line or a navigation tab sitting *entirely*
            # in the margin strip was put there on purpose. Overflow looks
            # different -- it starts inside the content band and spills out --
            # so a shape that straddles the edge is still reported.
            #
            # Without this the rule flags the footer row on every slide, and
            # the fixer then shoves those shapes up into the real footer,
            # trading a margin finding for a collision.
            if _outside_the_band(box, margins, width, height):
                continue
            breaches = []
            # An edge the brand does not specify is not tested. Guessing one
            # would report every deck against a frame nobody set.
            if margins.left_in is not None and box.left_in < margins.left_in - tolerance:
                breaches.append(f"left by {margins.left_in - box.left_in:.2f}in")
            if margins.top_in is not None and box.top_in < margins.top_in - tolerance:
                breaches.append(f"top by {margins.top_in - box.top_in:.2f}in")
            if (
                margins.right_in is not None
                and box.right_in > width - margins.right_in + tolerance
            ):
                breaches.append(
                    f"right by {box.right_in - (width - margins.right_in):.2f}in"
                )
            if (
                margins.bottom_in is not None
                and box.bottom_in > height - margins.bottom_in + tolerance
            ):
                breaches.append(
                    f"bottom by {box.bottom_in - (height - margins.bottom_in):.2f}in"
                )
            if breaches:
                yield self.issue(
                    f"Text crosses the safe margin ({'; '.join(breaches)}).",
                    slide=slide,
                    shape=shape,
                    expected=_margin_summary(margins),
                    found="; ".join(breaches),
                )

    def _frame_for(self, ctx: RuleContext, slide: SlideProfile) -> Optional[Margins]:
        """The presentation space of this slide's layout, as margins.

        None when the layout is unknown to the master or marks no presentation
        space, which is the caller's signal to use the deck-wide frame.

        An authored edge still wins. The brand file is a statement of intent
        and a PS rectangle is a drawing; where they disagree the guidelines are
        the ones somebody signed off on.
        """
        layout = ctx.spec.layout_named(slide.layout_name)
        if layout is None:
            return None
        frame = layout.content_frame()
        if frame is None:
            return None
        stated = ctx.guidelines.safe_margins
        width, height = ctx.deck.width_in, ctx.deck.height_in
        return Margins(
            left_in=stated.left_in if stated.left_in is not None
            else round(frame.left_in, 2),
            top_in=stated.top_in if stated.top_in is not None
            else round(frame.top_in, 2),
            right_in=stated.right_in if stated.right_in is not None
            else round(width - frame.right_in, 2),
            bottom_in=stated.bottom_in if stated.bottom_in is not None
            else round(height - frame.bottom_in, 2),
        )


class TextCollisionRule(Rule):
    """Text that has left its own box and is drawn over another shape.

    The defect this was written for, off a real slide: four status columns,
    each a line of copy over a thin progress bar. Two of them ran to a third
    line, left their box, and were drawn across the bar. Nothing reported it.

    Neither existing rule could. `space.overlap` measures STORED boxes and
    only between two text-bearing shapes -- and on that slide the boxes did
    not touch at all, the copy box ending at 5.02in and the bar starting at
    5.06in. `space.text_overflow` knew the text was too tall for its box but
    not what was underneath it. The collision exists only once the text is
    drawn, so only a renderer can see it: `linemetrics` reports the rectangle
    PowerPoint drew, and 0.19in of it was on the bar.

    Reported on the shape that has to MOVE, which is the same call
    `space.overlap` makes and for the same reason: geometry cannot say which
    of two shapes is in the wrong place, but z-order says which one landed on
    the other, and `slide.shapes` is in document order.

    The finding carries the position to move to rather than a description of
    the problem, because the arithmetic belongs where the measurement is. What
    it does not carry is permission: the applier checks every geometric move
    against the shapes around it and reverts one that lands on a neighbour or
    breaks an alignment, so a shape with nowhere to go stays where it is and
    the report says so.
    """

    id = "space.text_collision"
    category = Category.SPACE
    description = "Text spilling out of its box is drawn over another shape."
    default_severity = Severity.ERROR

    def __init__(self, metrics: Optional[LineMetricsProvider] = None) -> None:
        self.metrics = metrics or NullLineMetrics()

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        if not self.metrics.available:
            return
        for slide, shape in ctx.text_shapes():
            if shape.is_group or not shape.text.strip():
                continue
            box = shape.geometry
            if box.width_in <= 0 or box.height_in <= 0:
                continue
            bounds = self.metrics.bounds(ShapeKey(slide.number, shape.shape_id))
            if bounds is None:
                continue
            if _overhang(bounds, box)[0] <= _MEASURED_SLACK_IN:
                continue
            hit = _first_shape_under(slide, shape, bounds)
            if hit is None:
                continue

            plan = _separation(ctx.deck, slide, shape, bounds, hit)
            if plan is None:
                continue
            mover, still, dx, dy = plan
            rect = still.geometry
            yield self.issue(
                f"{mover.name!r} and text spilling out of "
                f"{(still if mover is shape else shape).name!r} are drawn over "
                f"each other; {mover.name!r} is in front, so it is the one "
                f"that moves.",
                slide=slide,
                shape=mover,
                expected=(
                    f"clear of {still.name!r} at {rect.left_in:.2f}, "
                    f"{rect.top_in:.2f}, {rect.width_in:.2f} x "
                    f"{rect.height_in:.2f}in, moved to "
                    f"{mover.geometry.left_in + dx:.2f}, "
                    f"{mover.geometry.top_in + dy:.2f}in"
                ),
                found=(
                    f"text drawn to {bounds.bottom_in:.2f}in over a shape "
                    f"starting at {rect.top_in:.2f}in"
                ),
                suggestion=(
                    f"Move {mover.name!r} by {dx:+.2f}, {dy:+.2f}in, which is "
                    "the shortest way clear. If there is no room for that, "
                    "shorten the copy or give the text box the height it needs."
                ),
            )


def _separation(deck, slide, shape, bounds, hit):
    """Who moves and by how much, or None if neither can be nudged clear.

    The rectangle that matters for the text is the INK, not the box: the box
    is 0.22in tall and the text is drawn 0.40in tall, and it is the drawn
    height that is on the bar. The ink travels with the box, so a move
    computed against the ink is applied to the box unchanged.

    Front first, per z-order, which is the shape that arrived last and landed
    on the other. If its shortest way out leaves the slide, the other one is
    tried instead -- one of the two is usually against an edge, and refusing
    both because the front one is would leave the defect in the deck.
    """
    ink = Geometry(
        left_in=bounds.left_in, top_in=bounds.top_in,
        width_in=bounds.width_in, height_in=bounds.height_in,
    )
    order = list(slide.shapes)
    try:
        text_at = next(i for i, s in enumerate(order) if s.shape_id == shape.shape_id)
        hit_at = next(i for i, s in enumerate(order) if s.shape_id == hit.shape_id)
    except StopIteration:
        return None

    # (the one that moves, the one it clears, the rectangle to clear it of,
    #  the rectangle that moves)
    front_first = (
        [(hit, shape, ink, hit.geometry), (shape, hit, hit.geometry, ink)]
        if hit_at > text_at
        else [(shape, hit, hit.geometry, ink), (hit, shape, ink, hit.geometry)]
    )
    for mover, still, obstacle, moving in front_first:
        delta = _shortest_way_out(moving, obstacle)
        if delta is None:
            continue
        dx, dy = delta
        # A nudge, or nothing. Text with wrapping off can overhang its box by
        # inches, and then the shortest way out is to relocate something
        # across the slide -- a source line shunted 2in to clear a panel is
        # not the fix anyone wanted, and the real answer there is to turn
        # wrapping on or shorten the line. Past this the finding stands and
        # says so, with no target on it.
        if abs(dx) > _MAX_NUDGE_IN or abs(dy) > _MAX_NUDGE_IN:
            continue
        box = mover.geometry
        if (
            box.left_in + dx >= 0
            and box.top_in + dy >= 0
            and box.left_in + dx + box.width_in <= deck.width_in
            and box.top_in + dy + box.height_in <= deck.height_in
        ):
            return mover, still, dx, dy
    return None


def _shortest_way_out(moving: Geometry, obstacle: Geometry):
    """The smallest translation that separates two rectangles, or None.

    One axis, never a diagonal: a diagonal clears the same collision by moving
    further and lands the shape somewhere neither rectangle suggested. An axis
    the two fully share is not a collision on that axis but an alignment --
    a bar the width of the text above it -- so it is not a way out.
    """
    clear = _CLEAR_IN
    across = (
        (obstacle.left_in + obstacle.width_in - moving.left_in + clear, 0.0),
        (obstacle.left_in - (moving.left_in + moving.width_in) - clear, 0.0),
    )
    down = (
        (0.0, obstacle.top_in + obstacle.height_in - moving.top_in + clear),
        (0.0, obstacle.top_in - (moving.top_in + moving.height_in) - clear),
    )
    ways = []
    if not _shares_axis(moving.left_in, moving.width_in,
                        obstacle.left_in, obstacle.width_in):
        ways.extend(across)
    if not _shares_axis(moving.top_in, moving.height_in,
                        obstacle.top_in, obstacle.height_in):
        ways.extend(down)
    if not ways:
        ways = list(across) + list(down)
    return min(ways, key=lambda way: abs(way[0]) + abs(way[1]))


def _shares_axis(a: float, a_size: float, b: float, b_size: float) -> bool:
    """Whether one span is entirely inside the other on this axis."""
    return (a >= b and a + a_size <= b + b_size) or (
        b >= a and b + b_size <= a + a_size
    )


# A hair of daylight, so a shape pushed clear does not come back next run as
# touching to the nearest EMU.
_CLEAR_IN = 0.02

# The most a shape may be nudged to clear a collision. Beyond this the move
# is not tidying a slide, it is redesigning one: three quarters of an inch is
# already a visible relocation, and an overhang that needs more than that is
# a copy or wrapping problem wearing a geometry problem's clothes.
_MAX_NUDGE_IN = 0.75


class OverlapRule(Rule):
    """Text boxes that collide.

    Only reported between two text-bearing shapes: text over an image or a
    coloured panel is normal layout, text over text is not.

    Reported on the shape IN FRONT, and that is the whole difference between a
    finding a machine can act on and one it cannot. Two boxes overlap and the
    geometry alone cannot say which of them is in the wrong place -- but it
    can say which one landed on the other, because that is what z-order
    records. `slide.shapes` is in document order, so the later of the pair is
    the one drawn on top, and it is the one that moves.

    The partner's box travels on the finding rather than its name alone. A fix
    needs to know what it is clearing, names inside one slide are not unique,
    and a finding that carries its own evidence does not need to go looking.
    """

    id = "space.overlap"
    category = Category.SPACE
    description = "Two text boxes overlap."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        # Below this, two boxes are touching from rounding, not colliding.
        floor = ctx.tuning.min_overlap_in2
        for slide in ctx.deck.slides:
            # A collapsed row is `space.series_crowded`'s finding, and it is
            # one finding about the set rather than four about its pairs.
            # Saying the same thing five times helps nobody, and none of the
            # five can be fixed on its own: push the second tab clear of the
            # first and it lands on the third.
            crowded = _crowded_members(ctx, slide, floor)
            texts = [
                s for s in slide.shapes
                if s.text.strip() and not s.is_picture and s.geometry.width_in
            ]
            for i, behind in enumerate(texts):
                for front in texts[i + 1:]:
                    if behind.shape_id in crowded and front.shape_id in crowded:
                        continue
                    area = _overlap_area(behind.geometry, front.geometry)
                    if area > floor:
                        box = behind.geometry
                        yield self.issue(
                            f"Text boxes {front.name!r} and {behind.name!r} "
                            f"overlap by {area:.2f} sq in.",
                            slide=slide,
                            shape=front,
                            expected=(
                                f"clear of {behind.name!r} at "
                                f"{box.left_in:.2f}, {box.top_in:.2f}, "
                                f"{box.width_in:.2f} x {box.height_in:.2f}in"
                            ),
                            found=f"{area:.2f} sq in overlap with {behind.name!r}",
                            suggestion=(
                                f"{front.name!r} is the one on top, so it is the "
                                "one that moved. Nudge it clear, or shorten "
                                "whichever of the two grew."
                            ),
                        )


class CrowdedSeriesRule(Rule):
    """A row of repeated shapes that has collapsed into itself.

    A navigation ribbon of five tabs, each overlapping the next. Every pair is
    an overlap and none of them can be fixed one pair at a time: push the
    second clear of the first and it lands on the third, which is exactly what
    the move guard refuses. The set has to be spread across the space it
    occupies, all at once, which is what a designer does with Distribute
    Horizontally.

    So it is one finding about the set rather than four about its pairs, and
    `space.overlap` stands down for the shapes it names -- saying the same
    thing five times helps nobody.
    """

    id = "space.series_crowded"
    category = Category.SPACE
    description = "A row of repeated shapes overlaps itself."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        floor = ctx.tuning.min_overlap_in2
        for slide in ctx.deck.slides:
            if slide.hidden:
                continue
            for series in _series_of(slide, ctx.tuning.repeat_min_members):
                finding = self._crowded(ctx, slide, series, floor)
                if finding is not None:
                    yield finding

    def _crowded(self, ctx, slide, series, floor) -> Optional[Issue]:
        ordered = sorted(series, key=lambda s: s.geometry.left_in)
        if not _is_a_row(ordered, ctx.spec.tolerances.position_in):
            return None

        collisions = [
            (a, b) for a, b in zip(ordered, ordered[1:])
            if _overlap_area(a.geometry, b.geometry) > floor
        ]
        if not collisions:
            return None

        width = sum(s.geometry.width_in for s in ordered)
        start = ordered[0].geometry.left_in
        span = ordered[-1].geometry.left_in + ordered[-1].geometry.width_in

        # Their own span first: spreading inside it moves nothing outside the
        # row and cannot land on anything. When the shapes are wider than that
        # -- a row does not usually collapse without having outgrown itself --
        # fall back to the width the master leaves for content, which is what
        # a designer would widen the row to.
        if width > span - start:
            usable = _usable_width(ctx)
            if usable is not None and width <= usable[1] - usable[0]:
                start, span = usable
        gap = (span - start - width) / max(1, len(ordered) - 1)
        return self.issue(
            f"{len(ordered)} repeated shapes in a row overlap each other; "
            f"{len(collisions)} of the gaps between them are negative.",
            slide=slide,
            shape=ordered[0],
            expected=(
                f"evenly spaced across {start:.2f}-{span:.2f}in, "
                f"gap {gap:.2f}in"
            ),
            found=f"{len(collisions)} overlapping neighbour(s)",
            suggestion=(
                f"Select the {len(ordered)} shapes and distribute them "
                "horizontally across the space they already occupy."
            ),
        )


def _usable_width(ctx: RuleContext) -> Optional[tuple[float, float]]:
    """The left and right edges of the master's content area, in inches."""
    margins = ctx.spec.safe_margins
    left = getattr(margins, "left_in", None)
    right = getattr(margins, "right_in", None)
    if left is None or right is None or not ctx.deck.width_in:
        return None
    return left, ctx.deck.width_in - right


def _crowded_members(ctx: RuleContext, slide, floor: float) -> set:
    """Shape ids belonging to a row that `space.series_crowded` will report."""
    found: set = set()
    for series in _series_of(slide, ctx.tuning.repeat_min_members):
        ordered = sorted(series, key=lambda s: s.geometry.left_in)
        if not _is_a_row(ordered, ctx.spec.tolerances.position_in):
            continue
        if any(
            _overlap_area(a.geometry, b.geometry) > floor
            for a, b in zip(ordered, ordered[1:])
        ):
            found.update(s.shape_id for s in ordered)
    return found


def _is_a_row(ordered: list, tolerance: float) -> bool:
    """Every member sharing a centre y, which is what makes a row a row."""
    centres = [s.geometry.top_in + s.geometry.height_in / 2 for s in ordered]
    return max(centres) - min(centres) <= tolerance * 4


class AlignmentGridRule(Rule):
    """Shapes that miss the alignment the rest of the deck follows.

    Measured on the LEADING edge, which is the left one in an English deck and
    the right one in an Arabic deck. That is not a nicety: right-to-left copy
    is set flush right, so a column of Arabic shapes of different widths
    shares a right edge and nothing else. Read on left edges it is not a
    column at all, and every shape in it looks correctly placed -- the rule
    had nothing to say about an Arabic deck, which is worse than saying the
    wrong thing because it reads as a clean bill of health.
    """

    id = "space.alignment_grid"
    category = Category.SPACE
    description = "Shape edge misses the deck's dominant alignment."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.position_in
        trailing = ctx.deck.rtl

        grid, source = self._grid(ctx, trailing)
        if not grid:
            return

        misses: dict[tuple, list] = {}
        for slide, shape in ctx.shapes():
            if not shape.text.strip():
                continue
            left = _leading_edge(shape, trailing)
            nearest = min(grid, key=lambda edge: abs(edge - left))
            drift = abs(nearest - left)
            # Only near-misses: a shape deliberately placed elsewhere is not a
            # defect, a shape 0.06in off a grid line is.
            if not tolerance < drift <= tolerance * ctx.tuning.near_miss_factor:
                continue
            # Keyed on the line missed and the position missed from, bucketed
            # by tolerance so 0.48 and 0.49 are one position rather than two.
            misses.setdefault((nearest, round(left / tolerance)), []).append(
                (slide, shape, left, drift)
            )

        for (nearest, _bucket), members in sorted(misses.items()):
            # A near-miss the whole deck makes is not a near-miss. It is the
            # deck using a column of its own, which is one fact about the deck
            # rather than a defect per shape: measuring a real deck against a
            # marked-up master turned a single 0.11in difference into
            # thirty-two identical findings, and a report nobody reads to the
            # end is the same as no report.
            #
            # Reported all the same, because it is worth knowing and the old
            # inferred grid hid it completely: taking the deck's own 0.48in as
            # the grid line made every shape on it correct by definition.
            if len(members) >= ctx.tuning.grid_support:
                slides = sorted({slide.number for slide, _s, _l, _d in members})
                left = members[0][2]
                drift = members[0][3]
                where = (
                    f"slide {slides[0]}" if len(slides) == 1
                    else f"{len(slides)} slides"
                )
                yield self.issue(
                    f"{len(members)} shapes on {where} sit at "
                    f"{left:.2f}in, {drift:.2f}in off the {nearest:.2f}in grid "
                    f"line {source}. The deck is using a column of its own, "
                    f"not missing this one by accident.",
                    expected=f"{nearest:.2f}in",
                    found=f"{left:.2f}in on slides {_runs(slides)}",
                )
                continue
            side = "Right" if trailing else "Left"
            for slide, shape, left, drift in members:
                yield self.issue(
                    f"{side} edge is {drift:.2f}in off the {nearest:.2f}in "
                    f"grid line {source}.",
                    slide=slide,
                    shape=shape,
                    # The edge is named as well as the number, because which
                    # edge a deck aligns on depends on which way it reads and
                    # the fixer has to move the right one.
                    expected=f"{side.lower()} {nearest:.2f}in",
                    found=f"{left:.2f}in",
                )

    @staticmethod
    def _grid(ctx: RuleContext, trailing: bool) -> tuple[list[float], str]:
        """The edges to measure against, and where they came from.

        The master's own declarations when it has any. Inference is what this
        rule did before and still does for an unmarked master, but it takes
        the audited deck's word for what the grid is: on one real deck it
        derived eight grid lines, not one of which the master declares, and
        blessed 0.48in and 0.49in as two separate intentions. A deck that
        consistently sits off the master's column has that position certified
        as its own grid and reports nothing.

        Which source is in play changes what a finding means, so it is said in
        the message rather than left for the reader to assume.
        """
        declared = (
            ctx.spec.grid_right_edges_in if trailing else ctx.spec.grid_edges_in
        )
        if declared:
            return list(declared), DECLARED_GRID

        # A leading edge shared by this many shapes is an intended grid line.
        support = ctx.tuning.grid_support
        lefts = Counter(
            round(_leading_edge(shape, trailing), 2)
            for _slide, shape in ctx.shapes()
            if shape.text.strip()
        )
        return (
            [edge for edge, count in lefts.items() if count >= support],
            "the rest of the deck follows",
        )


class TextOverflowRule(Rule):
    """Text that does not fit the box it is in.

    The defect a rebuild creates more than any other. A messy deck hides
    overlong copy behind shrink-to-fit, which is PowerPoint quietly reducing
    the type until it fits; put that slide on the master and the copy arrives
    at the master's size with nothing shrinking it, and it runs out of its box
    and over whatever is under it. On a real deck a body paragraph came out
    across the heading below it and the subtitle above.

    Measured when a renderer is wired in, estimated when one is not.

    `formatting_tool.linemetrics` now reports the rectangle PowerPoint drew
    the text into, which settles what the estimate could only guess at, and
    settles a case the estimate cannot see at all: a box 0.22in tall holding
    three lines of 8pt renders 0.40in of text, and the 0.18in that does not
    fit lands on the shape below. The stored boxes never touch, so
    `space.overlap` -- which measures stored boxes, and only between two
    text-bearing shapes -- is silent, and a status bar under a status line is
    exactly that geometry. Measured, it is arithmetic.

    A measured overflow also says WHAT it runs over, which is the difference
    between "this box is a little tight" and "this line is drawn across a red
    bar on a slide going to a client".

    Without a renderer, the two proxies below, which need none:

    - Wrapping off, and the longest line wider than the box. One line, no
      wrap, and a width the box does not have.
    - Wrapping on, and the wrapped lines taller than the box.

    The wrap is estimated from an average glyph advance, which is the part a
    renderer would do properly, so it is reported only past a whole line of
    slack: a borderline box stays silent rather than filling a report with
    maybes.

    The three autofit modes are three different claims and are not treated
    alike. Shrink-to-fit is not an overflow -- the text is made to fit, and
    that it had to be is `size.autofit_shrink`'s finding. Grow-to-fit is not
    an overflow of the text either, but the box is stored smaller than it will
    render, so it grows into its neighbour and the overlap rule, measuring the
    stored box, sees nothing. That is worth saying and is said here.
    """

    id = "space.text_overflow"
    category = Category.SPACE
    description = "Text overflows its shape."
    default_severity = Severity.ERROR

    def __init__(self, metrics: Optional[LineMetricsProvider] = None) -> None:
        self.metrics = metrics or NullLineMetrics()

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        measurable = self.metrics.available
        for slide, shape in ctx.text_shapes():
            if shape.is_group or not shape.text.strip():
                continue
            box = shape.geometry
            if box.width_in <= 0 or box.height_in <= 0:
                continue

            mode = (shape.autofit or "").upper()

            # Measured first, and where a measurement exists it is the whole
            # answer: the estimate has nothing to add to it, including for
            # shrink-to-fit, which the estimate has to skip because it cannot
            # know what the text was shrunk to.
            if measurable:
                bounds = self.metrics.bounds(
                    ShapeKey(slide.number, shape.shape_id)
                )
                if bounds is not None:
                    finding = self._measured(slide, shape, box, bounds)
                    if finding is not None:
                        yield finding
                    continue

            if mode.startswith("TEXT_TO_FIT"):
                # The text is made to fit. That it had to be is a finding, and
                # `size.autofit_shrink` is the one that makes it.
                continue

            if shape.word_wrap is False:
                finding = self._unwrapped(slide, shape, box)
            else:
                finding = self._wrapped(slide, shape, box, mode)
            if finding is not None:
                yield finding

    def _measured(self, slide, shape, box, bounds) -> Optional[Issue]:
        """What the renderer drew, against the box it was meant to stay in.

        The bottom edge only. Text is drawn into a box whose width the wrap
        already respects, so the overflow that happens in practice is
        vertical; a width overhang on a wrapped box means the measurement is
        of something this rule does not understand.
        """
        over, edge = _overhang(bounds, box)
        if over <= _MEASURED_SLACK_IN:
            return None

        # What it runs OVER is `space.text_collision`'s finding. That one is
        # reported on the shape that has to move and carries a target, so it
        # can be applied; this one is about a box too small for its copy.
        hit = _first_shape_under(slide, shape, bounds)
        needed = _box_for(bounds, box, edge)
        return self.issue(
            f"Text is drawn {bounds.width_in:.2f} x {bounds.height_in:.2f}in "
            f"in a {box.width_in:.2f} x {box.height_in:.2f}in box, "
            f"overhanging the {edge} by {over:.2f}in"
            + (f" and running over {hit.name!r}." if hit is not None
               else " and running outside it."),
            slide=slide,
            shape=shape,
            severity=Severity.WARNING,
            # The box the copy needs, in the shape a fixer can read -- but
            # ONLY when the overflow runs into free space. Growing the box is
            # arithmetic there: the copy and the type are untouched and the
            # stored box stops lying about what is drawn.
            #
            # Where the text already runs over something, growing the box
            # reaches straight into the shape it is colliding with, so no
            # target is offered and `space.text_collision` -- which moves the
            # other shape -- is left to answer it. Withheld here rather than
            # refused in the fixer because the fixer's view of the slide is
            # the top-level shapes, and this rule has looked at all of them.
            expected=(
                (
                    f"a box its copy fits, at {needed.left_in:.2f}, "
                    f"{needed.top_in:.2f}, {needed.width_in:.2f} x "
                    f"{needed.height_in:.2f}in"
                )
                if hit is None
                else f"text within {box.width_in:.2f} x {box.height_in:.2f}in"
            ),
            found=(
                f"{bounds.width_in:.2f} x {bounds.height_in:.2f}in of text, "
                f"{over:.2f}in past the {edge}"
                + (f", over {hit.name!r}" if hit is not None else "")
            ),
            suggestion=(
                f"Give the box the {edge} room its copy needs, or shorten the "
                "copy. Resizing type changes how much fits, so it is not done "
                "here."
            ),
        )

    def _unwrapped(self, slide, shape, box) -> Optional[Issue]:
        """Wrapping is off, so every paragraph is one line however long it is."""
        widest = _widest_line_in(shape)
        if widest is None or widest <= box.width_in + _SLACK_IN:
            return None
        return self.issue(
            f"Wrapping is off and the longest line needs about "
            f"{widest:.2f}in in a {box.width_in:.2f}in box, so the text runs "
            "outside it.",
            slide=slide,
            shape=shape,
            expected=f"text within {box.width_in:.2f}in",
            found=f"about {widest:.2f}in of text on one line",
            suggestion=(
                "Turn wrapping on, widen the box, or shorten the copy. "
                "Resizing type changes how much fits, so it is not done here."
            ),
        )

    def _wrapped(self, slide, shape, box, mode: str) -> Optional[Issue]:
        needed = _wrapped_height_in(shape, box.width_in)
        if needed is None or needed <= box.height_in + _SLACK_IN:
            return None
        if mode.startswith("SHAPE_TO_FIT"):
            return self.issue(
                f"The box grows to fit its text: it is stored at "
                f"{box.height_in:.2f}in and needs about {needed:.2f}in, so it "
                "renders taller than it measures and can cover what is below.",
                slide=slide,
                shape=shape,
                severity=Severity.WARNING,
                expected=f"a box the height of its text, about {needed:.2f}in",
                found=f"{box.height_in:.2f}in stored, grow-to-fit on",
                suggestion=(
                    "Set the box to the height its copy needs, so what is "
                    "measured is what is drawn."
                ),
            )
        return self.issue(
            f"Text needs about {needed:.2f}in of height in a "
            f"{box.height_in:.2f}in box, so it runs outside it.",
            slide=slide,
            shape=shape,
            expected=f"text within {box.height_in:.2f}in",
            found=f"about {needed:.2f}in of text",
            suggestion=(
                "Shorten the copy, or give the box the height the copy needs. "
                "Resizing type changes how much fits, so it is not done here."
            ),
        )


# How far past the box the estimate has to reach before it is reported. About
# a line: the wrap is estimated rather than rendered, and an estimate half a
# line out should not produce a finding.
_SLACK_IN = 0.25

# And how far a MEASURED overflow has to reach, which is far less, because
# nothing is being guessed at. Two points: enough to swallow the rounding in a
# value PowerPoint reports to a tenth of a point, not enough to hide a line of
# 8pt type.
_MEASURED_SLACK_IN = 0.03

# Slack on the collision test, so two shapes that merely abut are not called a
# collision.
_TOUCH_IN = 0.01

# What PowerPoint leaves between a text frame's edge and its text unless told
# otherwise, on the axis being grown. A box grown to exactly the ink would
# re-overflow by this much the moment it is redrawn.
_INSET_IN = 0.05


def _box_for(bounds, box, edge: str):
    """The box this copy would fit in, grown on the edge it overflows.

    Grown, never shrunk, and on one edge only. The left and top stay where
    the designer put them: a box that overflows its bottom is not evidence
    that its top is wrong, and moving two edges to fix one is how a fix starts
    making composition decisions.
    """
    if edge in ("bottom", "top"):
        height = max(
            box.height_in, bounds.bottom_in + _INSET_IN - box.top_in
        )
        return Geometry(
            left_in=box.left_in, top_in=box.top_in,
            width_in=box.width_in, height_in=height,
        )
    width = max(box.width_in, bounds.right_in + _INSET_IN - box.left_in)
    return Geometry(
        left_in=box.left_in, top_in=box.top_in,
        width_in=width, height_in=box.height_in,
    )


def _first_shape_under(slide, shape, bounds):
    """The shape the overhanging text is drawn across, if there is one.

    The test is on the part of the text that is OUTSIDE its own box, and that
    is the whole point rather than a detail. Text over a coloured panel is
    ordinary layout when the box is doing it deliberately -- a caption on a
    photo, a label on a band -- and `space.overlap` declines to report it for
    exactly that reason. Text that has left its box to land on that panel is
    not deliberate, and the overhang is what tells the two apart.

    Any edge. It began as "below the box", because a wrapped paragraph can
    only grow downwards, but wrapping off is common in a hand-built deck and
    then a long line runs off the right instead. Asking whether the collision
    lies outside the box covers both, and the top and left with them, without
    four special cases.

    Searched in z-order and the first hit returned: one name makes the finding
    readable, and a designer who looks will see the rest.
    """
    box = shape.geometry
    for other in slide.shapes:
        if getattr(other, "shape_id", None) == shape.shape_id:
            continue
        rect = getattr(other, "geometry", None)
        if rect is None or rect.width_in <= 0 or rect.height_in <= 0:
            continue
        hit = _intersection(bounds, rect)
        if hit is None:
            continue
        # Inside the text's own box, this is the layout doing what it meant
        # to. Outside it, the text has gone somewhere nobody put it.
        if _within(hit, box):
            continue
        return other
    return None


def _intersection(a, b):
    """The rectangle two rectangles share, or None if they barely touch.

    Takes anything with left/top/width/height in inches, which is both
    `Geometry` and `TextBounds`.
    """
    left = max(a.left_in, b.left_in)
    top = max(a.top_in, b.top_in)
    right = min(a.left_in + a.width_in, b.left_in + b.width_in)
    bottom = min(a.top_in + a.height_in, b.top_in + b.height_in)
    if right - left <= _TOUCH_IN or bottom - top <= _TOUCH_IN:
        return None
    return Geometry(
        left_in=left, top_in=top,
        width_in=right - left, height_in=bottom - top,
    )


def _within(inner, outer) -> bool:
    """Whether one rectangle sits inside another, give or take a hair."""
    return (
        inner.left_in >= outer.left_in - _TOUCH_IN
        and inner.top_in >= outer.top_in - _TOUCH_IN
        and inner.left_in + inner.width_in
        <= outer.left_in + outer.width_in + _TOUCH_IN
        and inner.top_in + inner.height_in
        <= outer.top_in + outer.height_in + _TOUCH_IN
    )


def _overhang(bounds, box) -> tuple[float, str]:
    """How far the drawn text reaches past its box, and over which edge.

    The largest of the four, because that is the one a reader sees and the one
    a fix has to answer. Wrapped text overhangs the bottom; text with wrapping
    off overhangs the right.
    """
    edges = (
        (bounds.bottom_in - (box.top_in + box.height_in), "bottom"),
        (bounds.right_in - (box.left_in + box.width_in), "right"),
        (box.left_in - bounds.left_in, "left"),
        (box.top_in - bounds.top_in, "top"),
    )
    return max(edges, key=lambda edge: edge[0])

# Average glyph advance as a fraction of point size, for the proportional
# faces a deck is set in. Deliberately low, which under-counts the width of a
# line and so reports less rather than more.
_AVG_CHAR_WIDTH = 0.50

# Line box as a multiple of point size, and the size to assume for a run that
# states none of its own.
_LINE_HEIGHT = 1.2
_DEFAULT_PT = 18.0

# The insets PowerPoint gives a text box unless told otherwise: 0.1in left and
# right, 0.05in top and bottom.
_INSET_W_IN = 0.20
_INSET_H_IN = 0.10


def _widest_line_in(shape) -> Optional[float]:
    """The width of the longest paragraph, if nothing wraps it."""
    widths = [
        len(paragraph.text or "") * _char_width_in(_paragraph_size_pt(paragraph))
        for paragraph in shape.paragraphs
        if (paragraph.text or "").strip()
    ]
    return max(widths) + _INSET_W_IN if widths else None


def _wrapped_height_in(shape, width_in: float) -> Optional[float]:
    """Roughly how tall this shape's text needs to be once wrapped."""
    usable = width_in - _INSET_W_IN
    if usable <= 0:
        return None

    height = 0.0
    seen = False
    for paragraph in shape.paragraphs:
        size = _paragraph_size_pt(paragraph)
        line_in = (size * _LINE_HEIGHT) / 72.0
        text = paragraph.text or ""
        if not text.strip():
            height += line_in
            continue
        seen = True
        per_line = max(1, int(usable / _char_width_in(size)))
        height += max(1, ceil(len(text) / per_line)) * line_in
    return height + _INSET_H_IN if seen else None


def _char_width_in(size_pt: float) -> float:
    return (size_pt * _AVG_CHAR_WIDTH) / 72.0


def _paragraph_size_pt(paragraph) -> float:
    """The largest size stated in a paragraph, which sets its line box."""
    sizes = [run.size_pt for run in paragraph.runs if run.size_pt]
    return max(sizes) if sizes else _DEFAULT_PT


def _leading_edge(shape, trailing: bool) -> float:
    """The edge a deck aligns its content on.

    The right edge for right-to-left copy, the left edge otherwise. Named for
    what it is rather than for which side it happens to be, because which side
    it is depends on the deck.
    """
    box = shape.geometry
    return box.left_in + box.width_in if trailing else box.left_in


def _outside_the_band(box: Geometry, margins, width: float, height: float) -> bool:
    """True when the shape sits wholly in a margin strip rather than crossing into it.

    The distinction the rule turns on. Content that overflows starts inside
    the usable area and runs past its edge; furniture is drawn in the strip
    and never enters the content band at all.
    """
    if margins.bottom_in is not None and box.top_in >= height - margins.bottom_in:
        return True
    if margins.top_in is not None and box.bottom_in <= margins.top_in:
        return True
    if margins.left_in is not None and box.right_in <= margins.left_in:
        return True
    if margins.right_in is not None and box.left_in >= width - margins.right_in:
        return True
    return False


def _runs(numbers: list[int]) -> str:
    """[1,2,3,7,9,10] -> "1-3, 7, 9-10".

    A deck-wide finding names every slide it covers, and thirty slide numbers
    in a row is not a list a designer reads.
    """
    if not numbers:
        return ""
    parts: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:] + [None]:
        if number is not None and number == previous + 1:
            previous = number
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        if number is not None:
            start = previous = number
    return ", ".join(parts)


def _margin_summary(margins) -> str:
    """Only the edges that are actually specified."""
    parts = [
        f"{side} {getattr(margins, f'{side}_in')}in"
        for side in ("top", "right", "bottom", "left")
        if getattr(margins, f"{side}_in") is not None
    ]
    return ", ".join(parts) + " safe margin"


def _overlap_area(a: Geometry, b: Geometry) -> float:
    """Intersection area of two boxes, in square inches.

    Ignores rotation: a rotated shape is compared by its unrotated box, which
    over-reports. TODO once ShapeProfile.geometry.rotation is non-zero often
    enough to matter.
    """
    dx = min(a.right_in, b.right_in) - max(a.left_in, b.left_in)
    dy = min(a.bottom_in, b.bottom_in) - max(a.top_in, b.top_in)
    return dx * dy if dx > 0 and dy > 0 else 0.0
