"""Logo rules: presence, approved asset, size, placement, clear space."""

from __future__ import annotations

from typing import Iterable

from ..models import Category, Issue, Severity, ShapeProfile, SlideProfile
from .base import Rule, RuleContext


def _logo_candidates(ctx: RuleContext) -> list[tuple[SlideProfile, ShapeProfile]]:
    """Pictures that are probably the logo.

    Matched by approved asset hash when the guidelines list one, otherwise by
    shape name. Name matching is why an unapproved logo variant slips through:
    supply logo.asset_sha1 in the brand file to close that gap.
    """
    approved = set(ctx.guidelines.logo.asset_sha1)
    found: list[tuple[SlideProfile, ShapeProfile]] = []
    for slide, shape in ctx.shapes():
        if not shape.is_picture:
            continue
        if approved:
            if shape.image_sha1 in approved:
                found.append((slide, shape))
        elif "logo" in shape.name.lower():
            found.append((slide, shape))
    return found


class LogoPresenceRule(Rule):
    id = "logo.missing"
    category = Category.LOGO
    description = "Required logo is absent."
    default_severity = Severity.BLOCKER

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        spec = ctx.guidelines.logo
        candidates = _logo_candidates(ctx)
        slides_with_logo = {slide.number for slide, _ in candidates}

        if spec.required_on_first_slide and 1 not in slides_with_logo:
            yield self.issue(
                "Cover slide has no logo.",
                slide=ctx.deck.slides[0] if ctx.deck.slides else None,
                expected="logo present on slide 1",
                found="none detected",
            )

        if spec.required_on_every_slide:
            missing = [
                slide.number
                for slide in ctx.deck.slides
                if slide.number not in slides_with_logo and not slide.hidden
            ]
            if missing:
                yield self.issue(
                    f"Logo missing on {len(missing)} slide(s).",
                    severity=Severity.ERROR,
                    expected="logo on every slide",
                    found=f"missing on {missing}",
                )


class UnapprovedLogoAssetRule(Rule):
    """A logo-shaped picture whose file is not one of the approved assets."""

    id = "logo.unapproved_asset"
    category = Category.LOGO
    description = "Logo image is not an approved asset file."
    default_severity = Severity.ERROR
    requires_guidelines = True

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        approved = set(ctx.guidelines.logo.asset_sha1)
        if not approved:
            return
        for slide, shape in ctx.shapes():
            if not shape.is_picture or "logo" not in shape.name.lower():
                continue
            if shape.image_sha1 not in approved:
                yield self.issue(
                    "Picture is named as a logo but is not an approved asset "
                    "(likely a re-exported or stretched copy).",
                    slide=slide,
                    shape=shape,
                    expected="approved logo file",
                    found=f"sha1 {shape.image_sha1}",
                )


class LogoGeometryRule(Rule):
    """Logo size, corner and clear space against the master placement."""

    id = "logo.geometry"
    category = Category.LOGO
    description = "Logo size, position or clear space differs from the master."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        spec = ctx.guidelines.logo
        expected = ctx.spec.logo_geometry

        for slide, shape in _logo_candidates(ctx):
            box = shape.geometry

            if spec.min_width_in and box.width_in < spec.min_width_in:
                yield self.issue(
                    f"Logo is {box.width_in:.2f}in wide, below the "
                    f"{spec.min_width_in}in minimum.",
                    slide=slide,
                    shape=shape,
                    expected=f">= {spec.min_width_in}in wide",
                    found=f"{box.width_in:.2f}in",
                )

            if spec.allowed_corners:
                corner = _corner(shape, ctx.deck.width_in, ctx.deck.height_in)
                if corner not in spec.allowed_corners:
                    yield self.issue(
                        f"Logo sits in the {corner} corner.",
                        slide=slide,
                        shape=shape,
                        expected=" or ".join(spec.allowed_corners),
                        found=corner,
                    )

            if expected is not None:
                drift = max(
                    abs(box.left_in - expected.left_in),
                    abs(box.top_in - expected.top_in),
                )
                if drift > ctx.spec.tolerances.position_in:
                    yield self.issue(
                        f"Logo is {drift:.2f}in off the master position.",
                        slide=slide,
                        shape=shape,
                        severity=Severity.WARNING,
                        expected=f"{expected.left_in:.2f}, {expected.top_in:.2f}in",
                        found=f"{box.left_in:.2f}, {box.top_in:.2f}in",
                    )

            # TODO: clear space. Needs the nearest neighbouring shape edge in
            # each direction, then a comparison against spec.clear_space_in.
            # Reuse the overlap geometry helper in rules/space.py rather than
            # writing a second bounding-box pass here.

            # TODO: aspect-ratio distortion. Compare box.width_in / height_in
            # against the native aspect of the image part; python-pptx exposes
            # it as shape.image.size (px) -- record it in ShapeProfile first.


def _corner(shape: ShapeProfile, deck_w: float, deck_h: float) -> str:
    box = shape.geometry
    centre_x = box.left_in + box.width_in / 2
    centre_y = box.top_in + box.height_in / 2
    horizontal = "l" if centre_x < deck_w / 2 else "r"
    vertical = "t" if centre_y < deck_h / 2 else "b"
    return f"{vertical}{horizontal}"
