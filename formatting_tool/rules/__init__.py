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
    TextColorUnusedByMasterRule,
)
from .contrast import TextContrastRule
from .textframe import (
    AnchorBlocksFitRule,
    AutofitScaleRule,
    TextInsetRule,
)
from .direction import (
    RightToLeftRule,
    RtlAlignmentRule,
    RtlLeadingEdgeRule,
)
from .fonts import (
    ArabicFontRule,
    MixedFontsInShapeRule,
    ThemeFontDriftRule,
    UnapprovedFontRule,
)
from .layouts import LayoutBandRule, LayoutHeaderFooterRule, LayoutMissingRule
from .logo import LogoGeometryRule, LogoPresenceRule, UnapprovedLogoAssetRule
from .repeats import (
    MirroredPairRule,
    RepeatedElementRule,
    SeriesFormattingRule,
    SatelliteOffsetRule,
    SeriesRowRule,
)
from .sizes import AutofitShrinkRule, InconsistentRoleSizeRule, RoleFontSizeRule
from .tables import TableHeaderAlignmentRule, TableHeaderRowsRule
from .space import (
    AlignmentGridRule,
    OffCanvasRule,
    CrowdedSeriesRule,
    OverlapRule,
    SafeMarginRule,
    BandWidthRule,
    TextCollisionRule,
    MatrixGutterRule,
    UnevenSeriesRule,
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
    HeadingBalanceRule,
    OrphanWidowRule,
    TitlePunctuationRule,
    WhitespaceHygieneRule,
)

log = logging.getLogger(__name__)

__all__ = [
    "RuleContext",
    "Rule",
    "build_default_rules",
    "build_first_pass_rules",
    "build_master_rules",
    "build_second_pass_rules",
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


def build_first_pass_rules(
    metrics: Optional[LineMetricsProvider] = None,
) -> list[Rule]:
    """Pass one: everything that can be judged one slide at a time.

    In report order (structure, then brand, then space, then polish). Every
    rule here reaches its verdict from the slide in front of it, so the order
    inside this list is presentation and nothing depends on it.

    What is NOT here is the other half of the answer -- see
    `build_second_pass_rules`.
    """
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
        ArabicFontRule(),
        RightToLeftRule(),
        RtlAlignmentRule(),
        MixedFontsInShapeRule(),
        ThemeFontDriftRule(),
        ThemeMismatchRule(),
        OffPaletteTextRule(),
        TextColorUnusedByMasterRule(),
        OffPaletteShapeRule(),
        # After the palette rules and reading the result of them: every colour
        # on a slide can be on the palette and the slide still be unreadable,
        # because a palette says which colours are allowed and never which
        # pairs of them may be stacked.
        TextContrastRule(),
        # What a shape does with the text INSIDE its box, as opposed to where
        # the box sits: three settings that each defeat something the tool
        # otherwise does well. See `rules.textframe`.
        AutofitScaleRule(),
        AnchorBlocksFitRule(metrics=metrics),
        TextInsetRule(),
        RoleFontSizeRule(),
        LogoGeometryRule(),
        # Within one slide: a series, a row, a mirrored pair. Deck-wide
        # consistency is pass two's job.
        RepeatedElementRule(),
        SeriesFormattingRule(),
        SatelliteOffsetRule(),
        SeriesRowRule(),
        MirroredPairRule(),
        # Space
        OffCanvasRule(),
        SafeMarginRule(),
        OverlapRule(),
        CrowdedSeriesRule(),
        TextOverflowRule(metrics=metrics),
        TextCollisionRule(metrics=metrics),
        BandWidthRule(),
        UnevenSeriesRule(),
        MatrixGutterRule(),
        # After the space rules and before the tables: a shape on the wrong
        # side of the page is the largest of these moves, and the report reads
        # better with it beside the other edge findings than filed under
        # direction with the paragraph ones.
        RtlLeadingEdgeRule(),
        TableHeaderRowsRule(),
        TableHeaderAlignmentRule(),
        AutofitShrinkRule(),
        # Typography, minus the orphan check. That one is deliberately held
        # back to the very end of pass two; see `build_second_pass_rules`.
        HeadingBalanceRule(metrics=metrics),
        ManualLineBreakRule(metrics=metrics),
        WhitespaceHygieneRule(),
        TitlePunctuationRule(),
    ]


def build_second_pass_rules(
    metrics: Optional[LineMetricsProvider] = None,
) -> list[Rule]:
    """Pass two: the checks that need the whole deck, run after pass one.

    Two kinds of thing live here, and the order between them is load-bearing.

    First, slide against slide. These rules do not judge a slide, they judge
    the deck: the size a role is set at on most slides and the handful that
    disagree, the column the deck follows and the shapes that miss it, the
    height titles sit at and the slides that do not. Each one derives a
    deck-wide norm and then reports the departures from it, which it can only
    do once every slide has been read and restyled -- a norm taken while the
    master was still being applied would be a norm over a deck that no longer
    exists.

    Then, LAST, the orphan and widow check. Last because it is the only rule
    whose subject is not in the file. Where a line breaks is the renderer's
    decision, so this rule measures the deck as PowerPoint draws it, and
    PowerPoint draws it differently after anything upstream has moved a box,
    changed a size or swapped a typeface. Running it earlier reports wraps
    that the rest of the pass then invalidates. Keep it at the end of this
    list.
    """
    return [
        # Slide against slide
        InconsistentRoleSizeRule(),
        InconsistentColorUseRule(),
        TitlePositionConsistencyRule(),
        AlignmentGridRule(),
        # Dead last. Nothing goes below this line.
        OrphanWidowRule(metrics=metrics),
    ]


def build_default_rules(
    metrics: Optional[LineMetricsProvider] = None,
) -> list[Rule]:
    """Every rule, both passes, in the order the pipeline runs them.

    Kept for the callers that want the whole set in one go and have no second
    pass to speak of -- `formatting-tool rules`, the AI prompt's rule list,
    and the recheck after fixes are applied. The pipeline itself asks for the
    two passes separately, because the point of the split is that something
    happens between them.
    """
    return build_first_pass_rules(metrics) + build_second_pass_rules(metrics)


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
