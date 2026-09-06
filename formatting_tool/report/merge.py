"""Merge the two validation layers into one ordered list of inconsistencies.

Two things happen here, in order:

1. Deduplication. When the AI layer restates a rule finding, the two collapse
   into one entry that keeps the rule's precision and the AI's explanation.
   The AI names the finding it is restating by ref, and that is what decides
   it -- matching on the wording of `found` never worked, because the two
   layers describe the same defect in different words.
2. Ordering. Severity first, then deck, then slide, so the report reads in the
   order a designer would work through it.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

from ..models import SEVERITY_RANK, Issue, Source

log = logging.getLogger(__name__)


def merge_issues(
    rule_issues: Sequence[Issue],
    ai_issues: Sequence[Issue] = (),
    ref_lookup: Optional[dict[str, Issue]] = None,
    min_confidence: float = 0.0,
) -> list[Issue]:
    """Combine both layers into the final list.

    Every rule finding survives. The AI layer has no way to remove one: what
    the deterministic layer proved from the file reaches the report, and
    context that argues against a finding rides along on it as a lowered
    confidence and an explanation.
    """
    ref_lookup = ref_lookup or {}
    kept: list[Issue] = list(rule_issues)
    by_ref = dict(ref_lookup)
    by_key = {issue.dedupe_key(): issue for issue in kept}

    for issue in ai_issues:
        if issue.confidence is not None and issue.confidence < min_confidence:
            continue
        # The AI names what it is restating. Trust that before guessing from
        # the text, which is what the dedupe key can only do.
        existing = by_ref.get(issue.confirms or "") or by_key.get(issue.dedupe_key())
        if existing is not None:
            _absorb(existing, issue)
            continue
        kept.append(issue)
        by_key[issue.dedupe_key()] = issue

    return sort_issues(kept)




def _absorb(rule_issue: Issue, ai_issue: Issue) -> None:
    """Fold an AI restatement into the rule finding it duplicates.

    The rule finding keeps its identity -- it is the provable one -- and gains
    whatever the AI layer added: a suggestion, or a severity it argued up.
    """
    if not rule_issue.suggestion and ai_issue.suggestion:
        rule_issue.suggestion = ai_issue.suggestion
    if SEVERITY_RANK[ai_issue.severity.value] < SEVERITY_RANK[rule_issue.severity.value]:
        rule_issue.severity = ai_issue.severity
    rule_issue.confidence = ai_issue.confidence


def sort_issues(issues: Iterable[Issue]) -> list[Issue]:
    return sorted(
        issues,
        key=lambda issue: (
            SEVERITY_RANK.get(issue.severity.value, 9),
            issue.deck or "",
            issue.slide if issue.slide is not None else -1,
            issue.category.value,
            issue.shape or "",
        ),
    )


def summarize_report(report) -> dict[str, int]:
    """Counts for the report, including the checks that never ran.

    Separate from `summarize` because the issue list alone cannot answer "is
    this a short report or a quiet one". Anything that recomputes stats after
    filtering has to come through here, or the skipped count vanishes and the
    report silently goes back to looking complete.
    """
    stats = summarize(report.issues)
    if report.skipped_rules:
        stats["rules_skipped"] = len(report.skipped_rules)
    return dict(sorted(stats.items()))


def summarize(issues: Sequence[Issue]) -> dict[str, int]:
    """Counts for the report header, by severity, category and source."""
    stats: dict[str, int] = {"total": len(issues)}
    for issue in issues:
        stats[f"severity.{issue.severity.value}"] = (
            stats.get(f"severity.{issue.severity.value}", 0) + 1
        )
        stats[f"category.{issue.category.value}"] = (
            stats.get(f"category.{issue.category.value}", 0) + 1
        )
        key = "source.rule" if issue.source is Source.RULE else "source.ai"
        stats[key] = stats.get(key, 0) + 1
    return dict(sorted(stats.items()))
