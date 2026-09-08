"""The deterministic validation layer: registry and runner.

Layer 1 of the workflow. Everything it reports is provable from the file --
a hex value that is not in the palette, a shape 0.3in off the canvas. Nothing
here judges taste; that is layer 2's job.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

from ..linemetrics import LineMetricsProvider
from ..models import Issue, SkippedRule
from .base import Rule, RuleContext
from .colors import (
    InconsistentColorUseRule,
    OffPaletteShapeRule,
    ThemeMismatchRule,
    OffPaletteTextRule,
)
from .fonts import MixedFontsInShapeRule, ThemeFontDriftRule, UnapprovedFontRule
from .layouts import LayoutBandRule, LayoutHeaderFooterRule, LayoutMissingRule
from .logo import LogoGeometryRule, LogoPresenceRule, UnapprovedLogoAssetRule
from .repeats import RepeatedElementRule, SatelliteOffsetRule
from .sizes import AutofitShrinkRule, InconsistentRoleSizeRule, RoleFontSizeRule
from .space import (
    AlignmentGridRule,
    OffCanvasRule,
    OverlapRule,
    SafeMarginRule,
    TextOverflowRule,
)
from .titles import (
    DetachedTitleRule,
    MissingTitleRule,
    SubtitleRule,
    TitlePositionConsistencyRule,
)
from .typography import (
    ManualLineBreakRule,
    OrphanWidowRule,
    TitlePunctuationRule,
    WhitespaceHygieneRule,
)

log = logging.getLogger(__name__)

__all__ = [
    "RuleContext",
    "Rule",
    "build_default_rules",
    "build_master_rules",
    "run_rules",
    "skipped_rules",
]


def skipped_rules(ctx: RuleContext, rules: Sequence[Rule]) -> list[SkippedRule]:
    """The rules that will not run against this context, and why.

    A rule reporting nothing because it is disabled is indistinguishable, in
    the issue list, from a rule reporting nothing because the deck is clean.
    The report carries this so the difference is visible without reading the
    source.
    """
    skipped: list[SkippedRule] = []
    for rule in rules:
        if rule.applies(ctx):
            continue
        skipped.append(
            SkippedRule(
                rule_id=rule.id,
                reason="no brand guidelines file was supplied",
                unlocked_by=(
                    "extract-guidelines --from BRANDBOOK.pdf (or the approved "
                    "MASTER.pptx), then validate --guidelines that file"
                ),
            )
        )
    return skipped


def build_master_rules() -> list[Rule]:
    """Rules whose subject is the master, not the deck being checked.

    They read `ctx.spec` alone, so running them once per deck would report the
    same incomplete layout N times. The pipeline runs them a single time.
    """
    return [
        LayoutHeaderFooterRule(),
        LayoutBandRule(),
    ]


def build_default_rules(
    metrics: Optional[LineMetricsProvider] = None,
) -> list[Rule]:
    """Every rule, in report order (structure, then brand, then polish)."""
    return [
        # Structure
        LayoutMissingRule(),
        MissingTitleRule(),
        DetachedTitleRule(),
        SubtitleRule(),
        LogoPresenceRule(),
        UnapprovedLogoAssetRule(),
        # Brand
        UnapprovedFontRule(),
        MixedFontsInShapeRule(),
        ThemeFontDriftRule(),
        ThemeMismatchRule(),
        OffPaletteTextRule(),
        OffPaletteShapeRule(),
        RoleFontSizeRule(),
        LogoGeometryRule(),
        # Consistency
        InconsistentRoleSizeRule(),
        InconsistentColorUseRule(),
        TitlePositionConsistencyRule(),
        AlignmentGridRule(),
        RepeatedElementRule(),
        SatelliteOffsetRule(),
        # Space
        OffCanvasRule(),
        SafeMarginRule(),
        OverlapRule(),
        TextOverflowRule(),
        AutofitShrinkRule(),
        # Typography
        OrphanWidowRule(metrics=metrics),
        ManualLineBreakRule(),
        WhitespaceHygieneRule(),
        TitlePunctuationRule(),
    ]


def run_rules(ctx: RuleContext, rules: Sequence[Rule]) -> list[Issue]:
    """Run every applicable rule and collect its findings.

    A rule that raises is logged and skipped: one broken check should not cost
    the report the other twenty-four.
    """
    issues: list[Issue] = []
    for rule in rules:
        if not rule.applies(ctx):
            log.debug("skipping %s: no guidelines supplied", rule.id)
            continue
        try:
            found = list(rule.check(ctx) or ())
        except Exception:
            log.exception("rule %s failed on %s", rule.id, ctx.deck.name)
            continue
        for issue in found:
            issue.deck = ctx.deck.name
        log.debug("%s: %d finding(s)", rule.id, len(found))
        issues.extend(found)
    return issues


def describe_rules(rules: Iterable[Rule]) -> list[dict[str, str]]:
    """Rule metadata, for `formatting-tool rules` and the AI prompt."""
    return [
        {
            "id": rule.id,
            "category": rule.category.value,
            "severity": rule.default_severity.value,
            "requires_guidelines": str(rule.requires_guidelines),
            "description": rule.description,
        }
        for rule in rules
    ]
