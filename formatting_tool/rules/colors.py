"""Colour rules: text, fill and line colours against the brand palette."""

from __future__ import annotations

from typing import Iterable

from ..colorutil import nearest_palette_entry
from ..models import Category, Issue, Severity
from .base import Rule, RuleContext


class OffPaletteTextRule(Rule):
    id = "color.text.off_palette"
    category = Category.COLOR
    description = "Text colour is not in the brand palette."
    default_severity = Severity.ERROR
    # Not gated on a brand file. `spec.palette` is the authored palette
    # extended with the master's own theme scheme, so a master-only run -- which
    # is most runs -- has a palette to measure against: the one the master
    # declares. Requiring a brand file meant this never ran there, which is the
    # same stale gate the safe-margin check carried.

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.color_delta_e
        palette = ctx.spec.palette

        for slide, shape, _paragraph, run in ctx.runs():
            # A theme-bound colour is correct by construction; only a
            # hardcoded RGB can drift off-palette.
            if run.color_is_theme or not run.color_hex:
                continue
            label, distance = nearest_palette_entry(run.color_hex, palette)
            if distance is None or distance <= tolerance:
                continue
            yield self.issue(
                f"Text colour #{run.color_hex} is off-palette "
                f"(nearest: {label}, delta-E {distance:.1f}).",
                slide=slide,
                shape=shape,
                expected=f"{label} #{palette.get(label, '')}" if label else "brand palette",
                found=f"#{run.color_hex}",
                suggestion=(
                    f"Recolour to {label} or bind the run to the theme colour."
                    if label and distance < tolerance * ctx.tuning.suggestion_factor
                    else "Recolour to a palette entry."
                ),
            )


class OffPaletteShapeRule(Rule):
    id = "color.shape.off_palette"
    category = Category.COLOR
    description = "Shape fill or outline colour is not in the brand palette."
    default_severity = Severity.ERROR
    # Not gated on a brand file. `spec.palette` is the authored palette
    # extended with the master's own theme scheme, so a master-only run -- which
    # is most runs -- has a palette to measure against: the one the master
    # declares. Requiring a brand file meant this never ran there, which is the
    # same stale gate the safe-margin check carried.

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.color_delta_e
        palette = ctx.spec.palette

        for slide, shape in ctx.shapes():
            for kind, value in (("fill", shape.fill_hex), ("outline", shape.line_hex)):
                if not value:
                    continue
                label, distance = nearest_palette_entry(value, palette)
                if distance is None or distance <= tolerance:
                    continue
                yield self.issue(
                    f"Shape {kind} #{value} is off-palette "
                    f"(nearest: {label}, delta-E {distance:.1f}).",
                    slide=slide,
                    shape=shape,
                    expected=f"{label} #{palette.get(label, '')}" if label else "brand palette",
                    found=f"#{value}",
                )


class InconsistentColorUseRule(Rule):
    """Near-duplicate colours across the deck, whether or not they are on-brand.

    Fires when two colours sit close enough to read as the same intent but are
    not identical -- the signature of copy-paste from several source decks.
    """

    id = "color.inconsistent_variants"
    category = Category.COLOR
    description = "Several near-identical colours are used for the same purpose."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        # TODO: cluster every colour in the deck by perceptual distance
        # (single-link, threshold = tolerances.color_delta_e * 2), then report
        # any cluster with more than one member. Report deck-level, listing
        # the slides each variant appears on, and pick the cluster member that
        # matches the palette as the expected value.
        return ()
