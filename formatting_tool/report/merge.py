"""Merge the two validation layers into one ordered list of inconsistencies.

Three things happen here, in order:

1. Dismissal. A rule finding the AI layer judged a false positive is dropped,
   and the reason is recorded on the report.
2. Deduplication. When the AI layer restates a rule finding, the two collapse
   into one entry that keeps the rule's precision and the AI's explanation.
3. Ordering. Severity first, then deck, then slide, so the report reads in the
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
    dismissals: Optional[dict[str, str]] = None,
    ref_lookup: Optional[dict[str, Issue]] = None,
    min_confidence: float = 0.0,
) -> list[Issue]:
    """Combine both layers into the final list."""
    dismissals = dismissals or {}
    dismissed = _resolve_dismissals(dismissals, ref_lookup or {})

    kept: list[Issue] = []
    for issue in rule_issues:
        if id(issue) in dismissed:
            log.debug(
                "dismissed %s on slide %s: %s",
                issue.rule_id,
                issue.slide,
                dismissed[id(issue)],
            )
            continue
        kept.append(issue)

    by_key = {issue.dedupe_key(): issue for issue in kept}

    for issue in ai_issues:
        if issue.confidence is not None and issue.confidence < min_confidence:
            continue
        existing = by_key.get(issue.dedupe_key())
        if existing is not None:
            _absorb(existing, issue)
            continue
        kept.append(issue)
        by_key[issue.dedupe_key()] = issue

    return sort_issues(kept)


def _resolve_dismissals(
    dismissals: dict[str, str],
    ref_lookup: dict[str, Issue],
) -> dict[int, str]:
    """Map ref -> reason onto the identities of the Issues to drop."""
    resolved: dict[int, str] = {}
    for ref, reason in dismissals.items():
        issue = ref_lookup.get(ref)
        if issue is None:
            log.debug("dismissal names an unknown ref: %s", ref)
            continue
        resolved[id(issue)] = reason
    return resolved


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
