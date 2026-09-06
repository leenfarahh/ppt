"""Layout rules: what a slide is bound to, and whether the master is complete.

Two different subjects, so two different scopes:

- `LayoutMissingRule` looks at the messy deck. A slide whose layout is not in
  the master's set was built on a foreign master, which is the root cause
  behind most of what the other rules report downstream.
- `LayoutHeaderFooterRule` and `LayoutBandRule` look at the master itself. A
  layout that never carried footer plumbing cannot produce a compliant slide
  no matter how carefully the deck is rebuilt onto it.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..models import Category, Issue, Severity
from .base import Rule, RuleContext

# Placeholder tokens that constitute the header and the footer furniture.
# PowerPoint has no slide header placeholder -- headers exist only on notes
# and handouts -- so the title placeholder is what occupies the header band.
HEADER_PLACEHOLDERS = {"TITLE", "CENTER_TITLE", "VERTICAL_TITLE"}
FOOTER_PLACEHOLDERS = {"FOOTER", "SLIDE_NUMBER", "DATE"}

# Shape-name fragments that mark a designed header or footer band, checked
# after the geometric test so a correctly placed but oddly named bar counts.
HEADER_NAMES = ("header", "eyebrow", "kicker", "running head")
FOOTER_NAMES = ("footer", "foot ", "pagination", "page number", "slide number")


class LayoutMissingRule(Rule):
    """A slide bound to a layout the master does not define.

    Reported per slide, because the fix is per slide: the copy has to be moved
    onto a layout that exists. Matching is on the normalized name, so a rename
    from "Title Content" to "title_content" is not reported as drift.
    """

    id = "layout.not_in_master"
    category = Category.LAYOUT
    description = "Slide uses a layout the master does not define."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        if not ctx.spec.layouts:
            return
        available = ", ".join(ctx.spec.layout_names)
        for slide in ctx.deck.slides:
            if slide.hidden or not slide.layout_name:
                continue
            if ctx.spec.layout_named(slide.layout_name) is not None:
                continue
            yield self.issue(
                f"Slide is built on layout {slide.layout_name!r}, which is not "
                f"in the master.",
                slide=slide,
                expected=f"one of: {available}",
                found=slide.layout_name,
                suggestion=(
                    "Rebuild the deck onto the master "
                    "(formatting-tool rebuild) to move it onto an approved layout."
                ),
            )


class LayoutHeaderFooterRule(Rule):
    """A master layout missing its header or footer placeholders.

    Without a footer, slide-number and date placeholder, PowerPoint's Header
    and Footer dialog has nothing to write into, so no slide built on the
    layout can ever carry a page number. Without a title placeholder there is
    nothing for the copy to bind to and every title becomes a loose text box.
    """

    id = "layout.header_footer_missing"
    category = Category.LAYOUT
    description = "Master layout has no header or no footer placeholder."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for layout in ctx.spec.layouts:
            tokens = {
                shape.placeholder_token
                for shape in layout.placeholders
                if shape.placeholder_token
            }
            missing = []
            if not tokens & HEADER_PLACEHOLDERS:
                missing.append("header (title placeholder)")
            if not tokens & FOOTER_PLACEHOLDERS:
                missing.append("footer (footer, slide number or date placeholder)")
            if not missing:
                continue
            yield self.issue(
                f"Layout {layout.name!r} has no {' and no '.join(missing)}.",
                expected="a title placeholder and at least one footer placeholder",
                found=", ".join(sorted(tokens)) or "no placeholders",
                suggestion=(
                    f"In the slide master, add the missing placeholder(s) to "
                    f"{layout.name!r} via Insert Placeholder."
                ),
            )


class LayoutBandRule(Rule):
    """A master layout missing its designed header or footer band.

    The placeholder check above tests the plumbing; this tests the furniture.
    A layout can carry a footer placeholder and still have none of the rule
    line, logo lockup or running title that the brand's header and footer are
    actually made of.

    A band is a non-placeholder shape sitting in the top or bottom strip of
    the canvas. `tuning.header_band_fraction` and `tuning.footer_band_fraction`
    set how deep those strips are.
    """

    id = "layout.band_missing"
    category = Category.LAYOUT
    description = "Master layout has no header or footer band shape."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        height = ctx.spec.height_in
        if height <= 0:
            return
        header_limit = height * ctx.tuning.header_band_fraction
        footer_start = height - height * ctx.tuning.footer_band_fraction

        for layout in ctx.spec.layouts:
            furniture = [s for s in layout.shapes if not s.placeholder_type]
            header = _band_shape(furniture, HEADER_NAMES, top=0.0, bottom=header_limit)
            footer = _band_shape(furniture, FOOTER_NAMES, top=footer_start, bottom=height)

            missing = []
            if header is None:
                missing.append("header band")
            if footer is None:
                missing.append("footer band")
            if not missing:
                continue
            present = [s.name for s in furniture] or ["no non-placeholder shapes"]
            yield self.issue(
                f"Layout {layout.name!r} has no {' and no '.join(missing)}.",
                expected=(
                    f"a shape within {header_limit:.2f}in of the top and one "
                    f"below {footer_start:.2f}in"
                ),
                found=", ".join(present),
                suggestion=(
                    f"Add the brand's header and footer furniture to "
                    f"{layout.name!r} in the slide master."
                ),
            )


def _band_shape(
    shapes: list,
    name_fragments: tuple[str, ...],
    *,
    top: float,
    bottom: float,
) -> Optional[object]:
    """The first shape whose centre falls in the band, or which is named for it.

    Position is the stronger signal and is tried first: a band drawn as a
    thin rectangle called "Rectangle 12" is still a band. The name check is
    the fallback for furniture placed outside the strip, such as a side tab.
    """
    for shape in shapes:
        centre = shape.geometry.top_in + shape.geometry.height_in / 2
        if top <= centre <= bottom:
            return shape
    for shape in shapes:
        lowered = shape.name.lower()
        if any(fragment in lowered for fragment in name_fragments):
            return shape
    return None
