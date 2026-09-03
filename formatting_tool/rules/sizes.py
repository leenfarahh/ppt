"""Font size rules: against the role spec, and against the deck itself."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..models import Category, Issue, Severity
from .base import Rule, RuleContext


class RoleFontSizeRule(Rule):
    """Font size outside the range the role allows."""

    id = "size.role.out_of_range"
    category = Category.FONT_SIZE
    description = "Font size falls outside the range defined for its role."
    default_severity = Severity.ERROR
    requires_guidelines = True

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.size_pt

        for slide, shape, _paragraph, run in ctx.runs():
            spec = ctx.spec.roles.get(shape.role.value)
            if spec is None or run.size_pt is None:
                continue

            low = spec.min_size_pt
            high = spec.max_size_pt
            if low is not None and run.size_pt < low - tolerance:
                yield self.issue(
                    f"{shape.role.value.title()} text is {run.size_pt}pt, "
                    f"below the {low}pt minimum.",
                    slide=slide,
                    shape=shape,
                    expected=f"{low}-{high}pt" if high else f">= {low}pt",
                    found=f"{run.size_pt}pt",
                )
            elif high is not None and run.size_pt > high + tolerance:
                yield self.issue(
                    f"{shape.role.value.title()} text is {run.size_pt}pt, "
                    f"above the {high}pt maximum.",
                    slide=slide,
                    shape=shape,
                    expected=f"{low}-{high}pt" if low else f"<= {high}pt",
                    found=f"{run.size_pt}pt",
                )


class InconsistentRoleSizeRule(Rule):
    """The same role rendered at several sizes across the deck.

    Catches the case the role spec cannot: sizes that are all individually
    legal but visibly inconsistent slide to slide.
    """

    id = "size.role.inconsistent"
    category = Category.FONT_SIZE
    description = "One text role is set at several different sizes."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        # Roles where variation is a defect rather than a design choice. Body
        # copy legitimately varies with content density; a title does not.
        strict = set(ctx.tuning.strict_size_roles)
        by_role: dict[str, dict[float, list[int]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for slide, shape, _paragraph, run in ctx.runs():
            if shape.role.value in strict and run.size_pt:
                by_role[shape.role.value][run.size_pt].append(slide.number)

        for role, sizes in by_role.items():
            if len(sizes) < 2:
                continue
            # The size used on the most slides is the de facto standard; the
            # rest are the deviations worth naming.
            dominant = max(sizes, key=lambda size: len(set(sizes[size])))
            deviations = {s: v for s, v in sizes.items() if s != dominant}
            slides = sorted({n for numbers in deviations.values() for n in numbers})
            yield self.issue(
                f"{role.title()} is set at {len(sizes)} different sizes "
                f"({', '.join(f'{s}pt' for s in sorted(sizes))}).",
                expected=f"{dominant}pt on every slide",
                found=", ".join(f"{s}pt" for s in sorted(deviations)),
                suggestion=f"Set slides {slides} to {dominant}pt.",
            )


class AutofitShrinkRule(Rule):
    """Text that only fits because PowerPoint shrank it.

    Autofit produces sizes that appear nowhere in the brand system and differ
    per slide, so it defeats every other size check.
    """

    id = "size.autofit_shrink"
    category = Category.FONT_SIZE
    description = "Text box relies on autofit to shrink text."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, shape in ctx.text_shapes():
            if shape.autofit and "TEXT_TO_FIT_SHAPE" in shape.autofit:
                yield self.issue(
                    "Text box shrinks text to fit, so its rendered size is not "
                    "the size stored in the file.",
                    slide=slide,
                    shape=shape,
                    expected="fixed size, text edited to fit",
                    found=shape.autofit,
                )
