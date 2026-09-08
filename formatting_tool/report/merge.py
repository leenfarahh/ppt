"""Merge the two validation layers into one ordered list of inconsistencies.

Two things happen here, in order:

1. Deduplication. When the AI layer restates a rule finding, the two collapse
   into one entry that keeps the rule's precision and the AI's explanation.
   The AI names the finding it is restating by ref, and that is what decides
   it -- matching on the wording of `found` never worked, because the two
   layers describe the same defect in different words.

   The ref is not always there. On one real run the model labelled every
   restatement and 13 of 19 rule findings absorbed one; on another it labelled
   none, and 6 of its 11 findings reached the report as separate entries
   restating a rule finding sitting right beside them -- "The title
   placeholder is empty" next to `title.missing` on the same shape. Duplicated
   in the report, and worse than duplicated: the rule finding has a fixer and
   the restatement does not, so the same defect appeared twice, once
   correctable and once not.

   So an unlabelled AI finding is matched on WHERE it is -- slide, shape,
   category -- and only when exactly one rule finding sits there. Ambiguity is
   left alone: two `space` findings on one title are two different defects,
   and folding a restatement into whichever came first would attach the
   model's explanation to the wrong one.
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
    by_place = _by_place(rule_issues)
    absorbed: set[int] = set()
    # AI findings already kept, so two batches restating one another do not
    # both land. Rule findings are NOT in here: `dedupe_key` carries `found`,
    # and the two layers word that differently, so against a rule finding it
    # matched almost nothing -- and where it did match it was a plain dict,
    # so two rule findings sharing a key silently collapsed to whichever came
    # last. `_in_the_same_place` does that job now, and refuses the ambiguity
    # instead of resolving it by accident.
    seen_ai: dict[tuple, Issue] = {}

    for issue in ai_issues:
        if issue.confidence is not None and issue.confidence < min_confidence:
            continue
        # The AI names what it is restating. Trust that first; fall back to
        # where the finding sits only when it named nothing.
        existing = _same_defect(issue, by_ref.get(issue.confirms or ""))
        if existing is None and not issue.confirms:
            existing = (
                seen_ai.get(issue.dedupe_key())
                or _in_the_same_place(issue, by_place, absorbed)
            )
        if existing is not None:
            _absorb(existing, issue)
            absorbed.add(id(existing))
            continue
        kept.append(issue)
        seen_ai[issue.dedupe_key()] = issue

    return sort_issues(kept)


def _same_defect(issue: Issue, rule_issue: Optional[Issue]) -> Optional[Issue]:
    """The rule finding this restates, if it really is the same defect.

    A ref alone is not enough. The model is pressed hard to label its
    restatements, and pressed hard enough it labels findings that are not
    restatements at all: a production note on a shape that also breaches a
    margin came back naming the margin finding's ref, and absorbing it threw
    away both the note and the removal it proposed. The report kept the margin
    finding, which was already there, and lost the only thing on the slide
    that should not ship.

    Categories have to agree. A colour observation about a shape can restate a
    colour finding about it; a note about what the text SAYS cannot restate
    anything about where the shape sits.
    """
    if rule_issue is None:
        return None
    return rule_issue if rule_issue.category is issue.category else None


def _place(issue: Issue) -> tuple:
    """Where a finding is, on terms both layers can agree on.

    Not `found`: the two layers word the same defect differently and always
    have. Not the shape id either -- the AI is given shape names, not ids, so
    a name is the only handle it can return.
    """
    return (
        issue.deck or "",
        issue.slide,
        (issue.shape or "").strip().casefold(),
        issue.category.value,
    )


def _by_place(rule_issues: Sequence[Issue]) -> dict[tuple, list[Issue]]:
    places: dict[tuple, list[Issue]] = {}
    for issue in rule_issues:
        places.setdefault(_place(issue), []).append(issue)
    return places


def _in_the_same_place(
    issue: Issue, by_place: dict[tuple, list[Issue]], absorbed: set[int]
) -> Optional[Issue]:
    """The one rule finding this unlabelled AI finding restates, or None.

    One, or nothing. Two rule findings in the same place are two defects and
    there is no way to tell which was meant; and a rule finding that has
    already taken a restatement does not take a second, because absorbing
    overwrites what the first one contributed.
    """
    candidates = by_place.get(_place(issue), ())
    free = [c for c in candidates if id(c) not in absorbed]
    return free[0] if len(free) == 1 else None




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
