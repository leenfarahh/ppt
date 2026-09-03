"""Presentation space rules: safe margins, bleed, overlap, alignment.

"Presentation space" in the workflow means the usable area of the slide and how
the content sits inside it -- content outside the safe margins, running off the
canvas, colliding with other shapes, or misaligned against a grid the rest of
the deck follows.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from ..models import Category, Geometry, Issue, Severity
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
    description = "Content sits inside the safe margin."
    default_severity = Severity.WARNING
    requires_guidelines = True

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        margins = ctx.guidelines.safe_margins
        width, height = ctx.deck.width_in, ctx.deck.height_in
        tolerance = ctx.spec.tolerances.position_in

        for slide, shape in ctx.shapes():
            # Only text is judged against the safe margin; a background panel
            # or bleed image is supposed to cross it.
            if not shape.paragraphs or not shape.text.strip():
                continue
            box = shape.geometry
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
        # A left edge shared by this many shapes is an intended grid line.
        support = ctx.tuning.grid_support

        lefts = Counter(
            round(shape.geometry.left_in, 2)
            for _slide, shape in ctx.shapes()
            if shape.text.strip()
        )
        grid = [edge for edge, count in lefts.items() if count >= support]
        if not grid:
            return

        for slide, shape in ctx.shapes():
            if not shape.text.strip():
                continue
            left = shape.geometry.left_in
            nearest = min(grid, key=lambda edge: abs(edge - left))
            drift = abs(nearest - left)
            # Only near-misses: a shape deliberately placed elsewhere is not a
            # defect, a shape 0.06in off a grid line is.
            if tolerance < drift <= tolerance * ctx.tuning.near_miss_factor:
                yield self.issue(
                    f"Left edge is {drift:.2f}in off the {nearest:.2f}in grid line.",
                    slide=slide,
                    shape=shape,
                    expected=f"{nearest:.2f}in",
                    found=f"{left:.2f}in",
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
