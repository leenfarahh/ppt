"""Presentation space rules: safe margins, bleed, overlap, alignment.

"Presentation space" in the workflow means the usable area of the slide and how
the content sits inside it -- content outside the safe margins, running off the
canvas, colliding with other shapes, or misaligned against a grid the rest of
the deck follows.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
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


class BandWidthRule(Rule):
    """A band across a column that does not reach the column's edges.

    The defect off a real slide: a headline band reading "A trillion-dollar
    industry is passing the inflection point" sitting over three rows of
    content, 0.06in narrower than the rows beneath it. Small enough to survive
    a review and large enough to see once seen, because the eye reads a band
    and the block under it as one object and a short edge as a mistake.

    Nothing reported it, and the three rules that look like they should each
    had a reason:

    - `space.alignment_grid` reads the LEADING edge only, against a deck-wide
      grid. The band's left edge is right; it is the trailing edge, and
      therefore the width, that is wrong.
    - `space.repeat_out_of_line` and `space.row_out_of_line` group through
      `repeats._series`, which buckets on identical size and type. A band is
      not the size of the boxes it heads, so it is never in the same group as
      the column it belongs to.
    - `space.overlap` and the crowding rules are about shapes touching, and
      these do not touch.

    What makes it detectable is comparing the band against the BOUNDING BOX of
    the cluster it caps rather than against any shape in it. The band spans a
    dark label column and a light body column and matches neither on its own;
    it matches the pair of them.

    ONE EDGE HAS TO BE RIGHT. That is the discriminator between drift and
    design: a band dragged into place usually snaps one edge and misses the
    other, while a band deliberately inset from its column is inset at both
    ends. Both edges wrong is left alone, which costs the occasional real
    defect and buys never straightening a band somebody centred on purpose.
    """

    id = "space.band_width"
    category = Category.SPACE
    description = "A band across a column misses the column's edges."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            for band, cluster, span in _bands(slide, ctx.tuning.repeat_min_members):
                box = band.geometry
                left_off = box.left_in - span.left_in
                right_off = (box.left_in + box.width_in) - (
                    span.left_in + span.width_in
                )

                anchored = min(abs(left_off), abs(right_off))
                adrift = max(abs(left_off), abs(right_off))
                if anchored > _BAND_SLACK_IN or adrift <= _BAND_SLACK_IN:
                    continue        # both edges adrift, or both already right
                if adrift > _band_ceiling(box.width_in):
                    continue        # far enough out to be deliberate

                edge = "right" if abs(right_off) > abs(left_off) else "left"
                short = "short of" if (
                    right_off < 0 if edge == "right" else left_off > 0
                ) else "past"
                yield self.issue(
                    f"This band stops {adrift:.2f}in {short} the {edge} edge "
                    f"of the {len(cluster)} shapes it spans, so it reads as a "
                    "short edge on an object the eye takes as one.",
                    slide=slide,
                    shape=band,
                    expected=(
                        f"the column's edges, at {span.left_in:.2f}, "
                        f"{box.top_in:.2f}, {span.width_in:.2f} x "
                        f"{box.height_in:.2f}in"
                    ),
                    found=(
                        f"{box.left_in:.2f} to "
                        f"{box.left_in + box.width_in:.2f}in against a column "
                        f"from {span.left_in:.2f} to "
                        f"{span.left_in + span.width_in:.2f}in"
                    ),
                    suggestion=(
                        f"Take the band to the column's {edge} edge. The "
                        "column is several shapes wide, so it is the band that "
                        "moves rather than any of them."
                    ),
                )


# How far a band edge may sit from its column's before it is a finding.
#
# Tighter than the deck-wide `position_in`, deliberately. That tolerance is
# for shapes that should be about aligned; a band and the column it caps
# should share an edge EXACTLY, so anything past EMU rounding is a defect
# rather than a near miss. The slide this was found on was out by 0.06in and a
# 0.05in tolerance would have called it acceptable.
_BAND_SLACK_IN = 0.02

# And how far out stops being drift and starts being a decision. A band
# dragged carelessly is out by a hair; one inset on purpose is inset visibly,
# and straightening that would be the tool overruling a designer.
#
# Set from real decks rather than picked. At 0.25in and 5% this reported a
# grey band on a real slide as 0.23in short on its left -- and it was not: the
# circular badge icon beside it deliberately overhangs the component, and one
# overhanging element is enough to move the cluster's bounding edge. The
# genuine defect was 0.06in on a 6.04in band, so an eighth of an inch and 2%
# separate the two cleanly and with room either side.
_BAND_DRIFT_CEILING_IN = 0.12
_BAND_DRIFT_FRACTION = 0.02

# A band is wide for its height. Without this a label sitting above a column
# is read as capping it.
_BAND_ASPECT = 3.0

# And it spans the column rather than sitting over part of it.
_BAND_COVERAGE = 0.8

# How far below a band its column may start. A band caps what is directly
# under it; a gap larger than this is two things on one slide.
_BAND_REACH_IN = 0.6


def _is_band(shape) -> bool:
    """Whether a shape is a drawn band rather than something wide and short.

    Two conditions, and both were learned from the noise. Run over three real
    decks the first version reported four titles, a subtitle and a footer
    placeholder: all wide, all short, and all compared against most of the
    slide because the cluster under a title IS most of the slide.

    A band is FILLED. That is what a band is -- a coloured bar drawn behind
    copy -- and a title is a text frame with nothing behind it.

    A band is NOT A PLACEHOLDER. A placeholder's width comes from the layout,
    so a title narrower than the body beneath it is the layout's business or
    `title.position_inconsistent`'s, and squaring it against the body would
    fight whichever of those is right.

    Wide for its height, still: a filled square panel is not a band either.
    """
    if shape.placeholder_type is not None:
        return False
    if not (shape.fill_hex or shape.fill_theme):
        return False
    box = shape.geometry
    return box.width_in >= _BAND_ASPECT * box.height_in


def _band_ceiling(width_in: float) -> float:
    """The most a band may be out and still be read as drift."""
    return min(_BAND_DRIFT_CEILING_IN, _BAND_DRIFT_FRACTION * width_in)


def _bands(slide, minimum: int):
    """Every (band, cluster) pair on a slide, in document order.

    A band is a wide, short shape with a cluster of shapes directly beneath it
    or directly above it -- a header or a footer. Both directions, because a
    footer band across the bottom of a table is the same object and the same
    defect.
    """
    shapes = [
        s for s in slide.shapes
        if not s.is_group and s.geometry.width_in > 0 and s.geometry.height_in > 0
    ]
    for band in shapes:
        if not _is_band(band):
            continue
        box = band.geometry
        for below in (True, False):
            cluster = _capped_cluster(band, shapes, below)
            if len(cluster) < minimum:
                continue
            span = _shared_span(cluster)
            if span is None or span.width_in <= 0:
                continue
            overlap = min(
                box.left_in + box.width_in, span.left_in + span.width_in
            ) - max(box.left_in, span.left_in)
            if overlap < _BAND_COVERAGE * span.width_in:
                continue
            # The span travels with the pair. Computed twice, `_bands` and
            # `check` disagreed about it: this filtered on the shared span and
            # that compared against the raw bounding box, which on a real
            # slide included the full-width footer band and read the headline
            # band as 0.54in off on a left edge that was exact. The rule went
            # silent on the defect it was written for.
            yield band, cluster, span
            break       # a band caps one cluster; the nearer one wins


def _capped_cluster(band, shapes, below: bool) -> list:
    """The run of shapes a band sits directly on top of, or under.

    Grown row by row from the band outwards: the first row is whatever starts
    within `_BAND_REACH_IN` of the band's edge, and each row after it is
    whatever starts within the same reach of the last one. That is what stops
    the cluster running to the bottom of the slide and swallowing a footer, a
    source line and the page number with it.
    """
    box = band.geometry
    frontier = box.top_in + box.height_in if below else box.top_in
    cluster: list = []
    seen = {band.shape_id}

    while True:
        row = []
        for shape in shapes:
            if shape.shape_id in seen:
                continue
            rect = shape.geometry
            # Inside the band's span, allowing for the drift being looked
            # for. Overlap alone was not enough: the full-width footer band
            # across the bottom of a real slide overlaps every band above it,
            # and letting it join the cluster took the cluster's edges out to
            # the slide's. A shape spilling far past the band is a different
            # object, not part of the column it caps.
            spill = _BAND_DRIFT_CEILING_IN
            if (
                rect.left_in < box.left_in - spill
                or rect.left_in + rect.width_in
                > box.left_in + box.width_in + spill
            ):
                continue
            edge = rect.top_in if below else rect.top_in + rect.height_in
            gap = (edge - frontier) if below else (frontier - edge)
            if -_BAND_SLACK_IN <= gap <= _BAND_REACH_IN:
                row.append(shape)
        if not row:
            return cluster
        cluster.extend(row)
        seen.update(shape.shape_id for shape in row)
        frontier = (
            max(s.geometry.top_in + s.geometry.height_in for s in row)
            if below else min(s.geometry.top_in for s in row)
        )


def _shared_span(shapes) -> Optional[Geometry]:
    """The cluster's extent, ignoring an edge only one shape reaches.

    A component's outer edge is a shared edge: three labels start at the same
    left, three bodies end at the same right. A single shape sticking out past
    everything else is not the edge of the component, it is an element placed
    over it -- the circular badge that overhangs a band on a real slide -- and
    letting it define the span made every band on that slide look short.

    None when neither edge is shared, which is not a component this rule can
    speak about.
    """
    if len(shapes) < 2:
        return None
    lefts = sorted(s.geometry.left_in for s in shapes)
    rights = sorted(
        (s.geometry.left_in + s.geometry.width_in for s in shapes), reverse=True
    )
    left = _shared_edge_value(lefts)
    right = _shared_edge_value(rights)
    if left is None or right is None or right <= left:
        return None
    bounds = _bounds_of(shapes)
    return Geometry(
        left_in=left, top_in=bounds.top_in,
        width_in=right - left, height_in=bounds.height_in,
    )


def _shared_edge_value(edges: list[float]) -> Optional[float]:
    """The first edge in this order that at least two shapes sit on."""
    for index, edge in enumerate(edges):
        for other in edges[index + 1:]:
            if abs(other - edge) <= _BAND_SLACK_IN:
                return edge
    return None


def _bounds_of(shapes) -> Geometry:
    """The bounding box of a set of shapes."""
    left = min(s.geometry.left_in for s in shapes)
    top = min(s.geometry.top_in for s in shapes)
    right = max(s.geometry.left_in + s.geometry.width_in for s in shapes)
    bottom = max(s.geometry.top_in + s.geometry.height_in for s in shapes)
    return Geometry(
        left_in=left, top_in=top, width_in=right - left, height_in=bottom - top
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


@dataclass(frozen=True)
class Cell:
    """One box of a matrix, as either side of the tool can describe it."""

    key: Any            # a shape id for the rules, a live shape for the fixer
    left_in: float
    top_in: float
    width_in: float
    height_in: float

    @property
    def right_in(self) -> float:
        return self.left_in + self.width_in

    @property
    def bottom_in(self) -> float:
        return self.top_in + self.height_in


@dataclass(frozen=True)
class Matrix:
    """Rows of cells sharing a column signature, and the gutters between them."""

    rows: list          # list[list[Cell]], top to bottom, each left to right
    across: list        # horizontal gutters, left to right
    down: list          # vertical gutters, top to bottom

    @property
    def cells(self) -> list:
        return [cell for row in self.rows for cell in row]


def matrix_components(cells: list, tolerance: float) -> list:
    """Every matrix among these cells.

    Deliberately generic over what a cell IS, because the rule reads shape
    profiles and the fixer has to reach the same answer from live shapes. The
    two disagreeing about a set is the bug class that half-spread a row for
    however long `_row_with` matched sizes to the EMU, and it is not a bug
    worth having twice.

    A row is two or more cells side by side; a matrix is two or more rows
    sharing a column signature -- the same left edges and widths, which is
    what makes a set of rows one object rather than several.
    """
    signatures: dict = {}
    for row in _rows_of(_outermost(cells), tolerance):
        for run in _contiguous_runs(row):
            key = tuple((round(c.left_in, 2), round(c.width_in, 2)) for c in run)
            signatures.setdefault(key, []).append(run)

    found = []
    for runs in signatures.values():
        if len(runs) < 2:
            continue
        runs.sort(key=lambda row: min(c.top_in for c in row))
        across = [b.left_in - a.right_in for a, b in zip(runs[0], runs[0][1:])]
        down = [
            min(c.top_in for c in lower) - max(c.bottom_in for c in upper)
            for upper, lower in zip(runs, runs[1:])
        ]
        found.append(Matrix(rows=runs, across=across, down=down))
    return found


def _rows_of(cells: list, tolerance: float) -> list:
    """The cells grouped into rows, each row ordered left to right.

    Clustered against the row being built rather than rounded into fixed
    buckets, because a bucket has edges: rounding tops to a tenth puts cells
    at 0.099 and 0.101in into different rows, which is the same component read
    as two and no gutter found in either.
    """
    rows: list = []
    for cell in sorted(cells, key=lambda c: c.top_in):
        if rows and cell.top_in - rows[-1][0].top_in <= tolerance:
            rows[-1].append(cell)
        else:
            rows.append([cell])
    return [sorted(row, key=lambda c: c.left_in) for row in rows]


def _outermost(cells: list) -> list:
    """The cells, minus anything drawn inside one of them.

    A body cell on a real slide carried a chart and the chart's caption, both
    sharing the row's top edge. Counted as cells they gave that row four
    members where the rows below it had two, the column signatures no longer
    matched, and a three-row component was read as the bottom two rows only --
    which is worse than missing it, because the top row then keeps a gutter
    the other two lose.

    A shape inside a cell is content. The test is containment rather than
    size: a wide caption and a narrow one are both content, and what they have
    in common is sitting within something else.
    """
    kept = []
    for cell in cells:
        if cell.width_in <= 0 or cell.height_in <= 0:
            continue
        inside = any(
            other is not cell
            and other.left_in <= cell.left_in + _TOUCH_IN
            and other.top_in <= cell.top_in + _TOUCH_IN
            and other.right_in >= cell.right_in - _TOUCH_IN
            and other.bottom_in >= cell.bottom_in - _TOUCH_IN
            and (other.width_in * other.height_in) > (cell.width_in * cell.height_in)
            for other in cells
        )
        if not inside:
            kept.append(cell)
    return kept


def _contiguous_runs(row: list) -> list:
    """A row split where a gap is a boundary between components.

    Two components side by side share every row, so without this the gap
    BETWEEN them is read as a gutter: on a real slide a row came out as four
    cells with gaps 0.06, 0.41, 0.06, and the 0.41 is the space between the
    left half of the slide and the right half.
    """
    gaps = [b.left_in - a.right_in for a, b in zip(row, row[1:])]
    if not gaps:
        return []
    floor = max(min(gaps), _BOUNDARY_FLOOR_IN)
    runs, run = [], [row[0]]
    for cell, gap in zip(row[1:], gaps):
        if gap > _BOUNDARY_FACTOR * floor:
            runs.append(run)
            run = [cell]
        else:
            run.append(cell)
    runs.append(run)
    return [r for r in runs if len(r) >= 2]


class MatrixGutterRule(Rule):
    """A component whose horizontal and vertical gutters nearly agree.

    The case off a real slide: a three-row, two-column component with every
    horizontal gutter at 0.058in and every vertical one at 0.097in. Each kind
    is identical to the thousandth, so nothing here is drifting within a kind
    -- `space.series_uneven` passes it -- and the component still reads as
    two spacings where it was drawn as one.

    NEARLY is the whole rule, and it is what makes it safe to ship. Measured
    across four real decks, EVERY multi-column component had a horizontal
    gutter different from its vertical one: eleven of eleven. Tighter
    horizontal than vertical spacing is how these decks are built, so a rule
    that asserted the two must match would fire on almost every component and
    be wrong almost every time.

    What separates this case from those is the size of the difference. This
    one is 0.038in apart; the next nearest in four decks is 0.137in, and the
    rest are 0.30in. So a difference under `_GUTTER_NEAR_IN` reads as two
    numbers that were meant to be one, and anything above it as two numbers
    somebody chose.

    The target is the smaller of the two gutters, for the reason set out on
    `_target_gutter`: it is the only choice that gives two halves of the same
    component the same answer, and tightening a component cannot push it into
    whatever sits beside it.
    """

    id = "space.matrix_gutter"
    category = Category.SPACE
    description = "A component's horizontal and vertical gutters nearly agree."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            if slide.hidden:
                continue
            cells = [
                Cell(s.shape_id, s.geometry.left_in, s.geometry.top_in,
                     s.geometry.width_in, s.geometry.height_in)
                for s in slide.shapes if not s.is_group
            ]
            by_id = {s.shape_id: s for s in slide.shapes}
            for matrix in matrix_components(cells, ctx.spec.tolerances.position_in):
                finding = self._mismatch(slide, matrix, by_id)
                if finding is not None:
                    yield finding

    def _mismatch(self, slide, matrix, by_id) -> Optional[Issue]:
        across, down = matrix.across, matrix.down
        if not across or not down:
            return None
        if min(across) <= 0 or min(down) <= 0:
            return None         # touching or overlapping: not this rule's
        # Each kind has to be consistent with itself first. Where it is not,
        # `space.series_uneven` has that to say and saying both would be
        # asking for two fixes to one row.
        if (max(across) - min(across) > _EVEN_SLACK_IN
                or max(down) - min(down) > _EVEN_SLACK_IN):
            return None

        wide, tall = _median(across), _median(down)
        apart = abs(wide - tall)
        if apart <= _EVEN_SLACK_IN or apart > _GUTTER_NEAR_IN:
            return None

        target = _target_gutter(across, down)
        anchor = by_id.get(matrix.rows[0][0].key)
        if anchor is None:
            return None
        return self.issue(
            f"This {len(matrix.rows)}x{len(matrix.rows[0])} component is "
            f"{wide:.2f}in apart across and {tall:.2f}in down, so it reads as "
            "two spacings where it was drawn as one.",
            slide=slide,
            shape=anchor,
            expected=f"one gutter of {target:.3f}in, across and down",
            found=f"{wide:.3f}in across, {tall:.3f}in down",
            suggestion=(
                f"Set both gutters to {target:.2f}in. The component is "
                "re-spaced from its top-left corner, so nothing outside it "
                "moves and no box is resized."
            ),
        )


# How far apart two gutters may be and still read as one number drifted into
# two. Set from four real decks: the case this was written for is 0.038in
# apart and the next nearest component is 0.137in, so a twentieth of an inch
# sits between them with room on both sides.
_GUTTER_NEAR_IN = 0.05


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _target_gutter(across: list[float], down: list[float]) -> float:
    """The gutter the component keeps: the smaller of the two.

    Counting which gutter occurs more often was the first rule and it is not
    stable. The two halves of one real slide are the same component drawn
    twice, and a difference of one detected row between them -- three rows on
    the right, two on the left where a chart sat inside a cell -- flipped the
    count and gave the two halves DIFFERENT targets: one loosened to 0.097in
    and the other tightened to 0.058in, so squaring each up would have made
    the slide less consistent than it started.

    The smaller is stable under that, and it cannot push a component into
    whatever sits beside it.
    """
    return min(_median(across), _median(down))


class UnevenSeriesRule(Rule):
    """A row of repeated shapes spaced unevenly.

    Four column headings 0.783, 0.743 and 0.761in apart. Nothing overlaps and
    nothing is out of line, so `space.series_crowded` and
    `space.row_out_of_line` both pass it, and the row still reads as a rhythm
    that stumbles.

    The same finding and the same remedy as a collapsed row, one step earlier:
    the set is distributed across the span it already occupies. So it borrows
    that rule's machinery whole -- the series grouping, the row test, the
    even-gap arithmetic and its fixer -- and differs only in the trigger.
    Collapsed is an error because it is broken; uneven is a warning because it
    is untidy.

    WHAT THIS DELIBERATELY DOES NOT DO. It compares gaps of one kind against
    each other, never a horizontal gutter against a vertical one. Measured on
    a real deck, a component had every horizontal gutter at 0.058in and every
    vertical one at 0.097in -- identical to the thousandth within each kind,
    and different between them. Nothing there is drifting; the two values are
    a decision, and tighter horizontal than vertical spacing is an ordinary
    typographic choice. Making them agree would need a declared gutter to
    appeal to, and there is not one.

    CONTIGUOUS SERIES ONLY. A row of four where the middle gap is 0.407in and
    the outer two are 0.058in is two components side by side, not one uneven
    row, and its fixer spreads everything it finds in the row -- so a series
    with a boundary gap in it is left alone rather than half-corrected.
    """

    id = "space.series_uneven"
    category = Category.SPACE
    description = "A row of repeated shapes is spaced unevenly."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            if slide.hidden:
                continue
            for series in _series_of(slide, ctx.tuning.repeat_min_members):
                finding = self._uneven(ctx, slide, series)
                if finding is not None:
                    yield finding

    def _uneven(self, ctx, slide, series) -> Optional[Issue]:
        ordered = sorted(series, key=lambda s: s.geometry.left_in)
        if not _is_a_row(ordered, ctx.spec.tolerances.position_in):
            return None

        gaps = _gaps_of(ordered)
        if not gaps or any(gap <= 0 for gap in gaps):
            return None         # collapsed: `space.series_crowded`'s finding
        if not _is_contiguous(gaps):
            return None
        spread = max(gaps) - min(gaps)
        if spread <= _EVEN_SLACK_IN:
            return None

        width = sum(s.geometry.width_in for s in ordered)
        start = ordered[0].geometry.left_in
        span = ordered[-1].geometry.left_in + ordered[-1].geometry.width_in
        gap = (span - start - width) / max(1, len(ordered) - 1)
        return self.issue(
            f"{len(ordered)} repeated shapes in a row sit "
            f"{min(gaps):.2f}-{max(gaps):.2f}in apart, so the row is spaced "
            "unevenly.",
            slide=slide,
            shape=ordered[0],
            expected=(
                f"evenly spaced across {start:.2f}-{span:.2f}in, "
                f"gap {gap:.2f}in"
            ),
            found=f"gaps of {', '.join(f'{g:.2f}' for g in gaps)}in",
            suggestion=(
                f"Select the {len(ordered)} shapes and distribute them "
                "horizontally across the space they already occupy."
            ),
        )


# How far the gaps in one row may disagree before it is a finding. Tight,
# because within one row of one repeated shape the gaps should be identical:
# the drift on real decks measured 0.03 to 0.04in, and EMU rounding is four
# orders of magnitude below that.
_EVEN_SLACK_IN = 0.02

# What makes a gap a boundary between two groups rather than a gutter: far
# larger than the smallest gap in the row. Read against the smallest rather
# than the median, which averages when there are only two gaps and then
# cannot tell 0.25in from 2.53in apart.
_BOUNDARY_FACTOR = 2.0
_BOUNDARY_FLOOR_IN = 0.05


def _gaps_of(ordered: list) -> list[float]:
    """The horizontal gaps between consecutive members of a row."""
    return [
        b.geometry.left_in - (a.geometry.left_in + a.geometry.width_in)
        for a, b in zip(ordered, ordered[1:])
    ]


def _is_contiguous(gaps: list[float]) -> bool:
    """Whether a row is one group rather than two sitting side by side."""
    if not gaps:
        return False
    floor = max(min(gaps), _BOUNDARY_FLOOR_IN)
    return max(gaps) <= _BOUNDARY_FACTOR * floor


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
