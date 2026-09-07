"""Presentation space rules: safe margins, bleed, overlap, alignment.

"Presentation space" in the workflow means the usable area of the slide and how
the content sits inside it -- content outside the safe margins, running off the
canvas, colliding with other shapes, or misaligned against a grid the rest of
the deck follows.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

from ..models import Category, Geometry, Issue, Margins, Severity, SlideProfile, TextRole

# How a grid finding says its lines came from the master rather than from
# the deck under audit. A constant because the fixer keys on it: only a
# declared grid is safe to snap to, and a phrase drifting on one side of
# that would silently disable or silently enable the fix.
DECLARED_GRID = "the master declares"
from .base import Rule, RuleContext


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


class OverlapRule(Rule):
    """Text boxes that collide.

    Only reported between two text-bearing shapes: text over an image or a
    coloured panel is normal layout, text over text is not.
    """

    id = "space.overlap"
    category = Category.SPACE
    description = "Two text boxes overlap."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        # Below this, two boxes are touching from rounding, not colliding.
        floor = ctx.tuning.min_overlap_in2
        for slide in ctx.deck.slides:
            texts = [
                s for s in slide.shapes
                if s.text.strip() and not s.is_picture and s.geometry.width_in
            ]
            for i, first in enumerate(texts):
                for second in texts[i + 1:]:
                    area = _overlap_area(first.geometry, second.geometry)
                    if area > floor:
                        yield self.issue(
                            f"Text boxes {first.name!r} and {second.name!r} "
                            f"overlap by {area:.2f} sq in.",
                            slide=slide,
                            shape=first,
                            found=f"{area:.2f} sq in overlap with {second.name!r}",
                        )


class AlignmentGridRule(Rule):
    """Shapes that miss the alignment the rest of the deck follows."""

    id = "space.alignment_grid"
    category = Category.SPACE
    description = "Shape edge misses the deck's dominant alignment."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.position_in

        grid, source = self._grid(ctx)
        if not grid:
            return

        misses: dict[tuple, list] = {}
        for slide, shape in ctx.shapes():
            if not shape.text.strip():
                continue
            left = shape.geometry.left_in
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
            for slide, shape, left, drift in members:
                yield self.issue(
                    f"Left edge is {drift:.2f}in off the {nearest:.2f}in "
                    f"grid line {source}.",
                    slide=slide,
                    shape=shape,
                    # Bare inches, because the (unregistered) grid fixer parses
                    # this back as its target.
                    expected=f"{nearest:.2f}in",
                    found=f"{left:.2f}in",
                )

    @staticmethod
    def _grid(ctx: RuleContext) -> tuple[list[float], str]:
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
        declared = ctx.spec.grid_edges_in
        if declared:
            return list(declared), DECLARED_GRID

        # A left edge shared by this many shapes is an intended grid line.
        support = ctx.tuning.grid_support
        lefts = Counter(
            round(shape.geometry.left_in, 2)
            for _slide, shape in ctx.shapes()
            if shape.text.strip()
        )
        return (
            [edge for edge, count in lefts.items() if count >= support],
            "the rest of the deck follows",
        )


class TextOverflowRule(Rule):
    """Text that does not fit its box.

    TODO: needs rendered line metrics (formatting_tool/linemetrics.py) to know
    the rendered height. Two cheap proxies are already available and worth
    wiring in first: word_wrap disabled on a box narrower than its single line
    of text, and autofit set to NONE on a box whose paragraph count times line
    height exceeds the box height at the run's point size.
    """

    id = "space.text_overflow"
    category = Category.SPACE
    description = "Text overflows its shape."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        return ()


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
