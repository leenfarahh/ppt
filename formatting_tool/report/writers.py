"""Output formats for the final list of inconsistencies.

JSON is the contract for anything downstream (a UI, an Odoo task, a CI gate).
Markdown is what gets pasted into a review. Text is what you read in the
terminal while working.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TextIO

from ..models import Issue, Severity, Source, ValidationReport

_MARKERS = {
    Severity.BLOCKER: "[!]",
    Severity.ERROR: "[x]",
    Severity.WARNING: "[~]",
    Severity.INFO: "[i]",
}


def write_json(report: ValidationReport, stream: TextIO) -> None:
    json.dump(report.to_dict(), stream, indent=2, ensure_ascii=False)
    stream.write("\n")


def write_text(report: ValidationReport, stream: TextIO) -> None:
    """Grouped by deck and slide, in severity order within each slide."""
    stream.write(f"Deck(s):   {', '.join(report.decks)}\n")
    stream.write(f"Master:    {report.master}\n")
    stream.write(f"Brand:     {report.guidelines or 'none supplied'}\n")
    stream.write(f"AI layer:  {'on' if report.ai_enabled else 'off'}\n")
    stream.write(f"Generated: {report.generated_at}\n")
    if report.inferred_values:
        stream.write(
            f"Inferred:  {len(report.inferred_values)} brand value(s) came from "
            f"the master deck, not the brand reference:\n"
        )
        for path in report.inferred_values:
            stream.write(f"           {path}\n")
    stream.write("\n")

    if not report.issues:
        stream.write("No inconsistencies found.\n")
        return

    stream.write(_counts_line(report) + "\n\n")

    for (deck, slide), issues in _grouped(report.issues):
        heading = f"{deck} - " + (
            f"slide {slide}" if slide is not None else "deck level"
        )
        stream.write(heading + "\n")
        stream.write("-" * len(heading) + "\n")
        for issue in issues:
            stream.write(f"  {_MARKERS.get(issue.severity, '[ ]')} {issue.message}\n")
            if issue.shape:
                stream.write(f"      shape:    {issue.shape}\n")
            if issue.expected:
                stream.write(f"      expected: {issue.expected}\n")
            if issue.found:
                stream.write(f"      found:    {issue.found}\n")
            if issue.suggestion:
                stream.write(f"      fix:      {issue.suggestion}\n")
            stream.write(f"      {_provenance(issue)}\n")
        stream.write("\n")

    if report.ai_summary:
        stream.write("AI summary\n----------\n")
        stream.write(report.ai_summary + "\n")


def write_markdown(report: ValidationReport, stream: TextIO) -> None:
    stream.write("# Deck formatting review\n\n")
    stream.write(f"- **Deck(s):** {', '.join(report.decks)}\n")
    stream.write(f"- **Master:** {report.master}\n")
    stream.write(f"- **Brand guidelines:** {report.guidelines or 'none supplied'}\n")
    stream.write(f"- **AI layer:** {'on' if report.ai_enabled else 'off'}\n")
    stream.write(f"- **Generated:** {report.generated_at}\n")
    if report.inferred_values:
        stream.write(
            f"- **Inferred:** {len(report.inferred_values)} brand value(s) came "
            f"from the master deck, not the brand reference "
            f"(`{'`, `'.join(report.inferred_values)}`)\n"
        )
    stream.write("\n")

    if not report.issues:
        stream.write("No inconsistencies found.\n")
        return

    stream.write(_counts_line(report) + "\n\n")

    if report.ai_summary:
        stream.write(f"{report.ai_summary}\n\n")

    stream.write("| Severity | Slide | Shape | Category | Issue | Expected | Found |\n")
    stream.write("| --- | --- | --- | --- | --- | --- | --- |\n")
    for issue in report.issues:
        stream.write(
            "| {sev} | {slide} | {shape} | {cat} | {msg} | {exp} | {found} |\n".format(
                sev=issue.severity.value,
                slide=issue.slide if issue.slide is not None else "-",
                shape=_cell(issue.shape),
                cat=issue.category.value,
                msg=_cell(issue.message),
                exp=_cell(issue.expected),
                found=_cell(issue.found),
            )
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _grouped(issues: list[Issue]):
    buckets: dict[tuple[str, object], list[Issue]] = defaultdict(list)
    for issue in issues:
        buckets[(issue.deck or "", issue.slide)].append(issue)
    return sorted(
        buckets.items(),
        key=lambda item: (item[0][0], item[0][1] if item[0][1] is not None else -1),
    )


def _counts_line(report: ValidationReport) -> str:
    parts = [
        f"{report.stats.get(f'severity.{sev.value}', 0)} {sev.value}"
        for sev in Severity
        if report.stats.get(f"severity.{sev.value}", 0)
    ]
    return f"{len(report.issues)} inconsistencies: " + ", ".join(parts)


def _provenance(issue: Issue) -> str:
    if issue.source is Source.RULE:
        return f"[rule {issue.rule_id}]"
    confidence = (
        f", confidence {issue.confidence:.2f}" if issue.confidence is not None else ""
    )
    return f"[ai review{confidence}]"


def _cell(value: object) -> str:
    """Keep a pipe in the copy from breaking the markdown table."""
    if value is None:
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\v", " ")
