"""Read a report back from its JSON.

The JSON is the contract between a run that found things and a run that fixes
them, and between the browser UI and the CLI. A designer ticks findings in one
place and applies them in another, so the ids have to survive the round trip
intact and mean the same thing on the other side.

Unknown keys are ignored rather than rejected: a report written by a newer
build should still be applicable by an older one, minus whatever it does not
understand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..models import (
    Category,
    FixAction,
    Issue,
    Severity,
    SkippedRule,
    Source,
    ValidationReport,
)


class ReportError(ValueError):
    """Raised when a report file cannot be read."""


def load_report(path: str | Path) -> ValidationReport:
    path = Path(path)
    if not path.exists():
        raise ReportError(f"report not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReportError(f"{path.name}: expected an object at the top level")

    report = ValidationReport(
        master=str(raw.get("master", "")),
        decks=[str(d) for d in raw.get("decks", [])],
        generated_at=str(raw.get("generated_at", "")),
        guidelines=raw.get("guidelines"),
        ai_enabled=bool(raw.get("ai_enabled", False)),
        ai_summary=raw.get("ai_summary"),
        stats=dict(raw.get("stats", {})),
        inferred_values=[str(v) for v in raw.get("inferred_values", [])],
        skipped_rules=[
            SkippedRule(
                rule_id=str(entry.get("rule_id", "")),
                reason=str(entry.get("reason", "")),
                unlocked_by=str(entry.get("unlocked_by", "")),
            )
            for entry in raw.get("skipped_rules", [])
            if isinstance(entry, dict)
        ],
    )
    report.issues = [
        _issue(entry) for entry in raw.get("issues", []) if isinstance(entry, dict)
    ]
    # A report written before ids existed still applies: the same inputs give
    # the same fingerprint, so one is derived rather than refused.
    for issue in report.issues:
        if not issue.id:
            issue.id = issue.fingerprint()
    return report


def _issue(entry: dict[str, Any]) -> Issue:
    return Issue(
        category=_enum(Category, entry.get("category"), Category.OTHER),
        severity=_enum(Severity, entry.get("severity"), Severity.WARNING),
        message=str(entry.get("message", "")),
        source=_enum(Source, entry.get("source"), Source.RULE),
        rule_id=entry.get("rule_id"),
        confirms=entry.get("confirms"),
        slide=entry.get("slide"),
        shape=entry.get("shape"),
        shape_id=entry.get("shape_id"),
        deck=entry.get("deck"),
        expected=entry.get("expected"),
        found=entry.get("found"),
        suggestion=entry.get("suggestion"),
        confidence=entry.get("confidence"),
        evidence=entry.get("evidence"),
        fix=_fix(entry.get("fix")),
        id=entry.get("id"),
    )


def _fix(raw: Any) -> Optional[FixAction]:
    """The proposed action off a saved report, or None.

    Validated on the way back in, not trusted: a report is a file a person can
    edit, and an `op` nothing implements would reach the tick list as a fix
    that cannot run.
    """
    if not isinstance(raw, dict):
        return None
    action = FixAction(
        op=str(raw.get("op") or ""),
        shape=raw.get("shape"),
        shape_id=raw.get("shape_id"),
        hex=raw.get("hex"),
        font=raw.get("font"),
        size_pt=raw.get("size_pt"),
        left_in=raw.get("left_in"),
        top_in=raw.get("top_in"),
        width_in=raw.get("width_in"),
        height_in=raw.get("height_in"),
    )
    return action if action.valid else None


def _enum(cls, value: Any, fallback):
    try:
        return cls(str(value))
    except ValueError:
        return fallback
