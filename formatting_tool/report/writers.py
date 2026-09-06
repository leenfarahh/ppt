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
        # Still print what did not run. "No inconsistencies found" over a set
        # of disabled checks is the most misleading line this can produce.
        stream.write("No inconsistencies found.\n\n")
        _write_omissions_text(report, stream)
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

    _write_omissions_text(report, stream)

    if report.ai_summary:
        stream.write("AI summary\n----------\n")
        stream.write(report.ai_summary + "\n")


def _write_omissions_text(report: ValidationReport, stream: TextIO) -> None:
    """What the list above does not contain.

    Printed after the findings and before the summary, because it is the
    context for both: a short report can mean a clean deck or a report with
    most of its checks turned off, and the reader cannot tell those apart from
    the findings alone.
    """
    if report.dismissals:
        heading = (
            f"Dismissed by the AI layer ({len(report.dismissals)} rule finding(s))"
        )
        stream.write(heading + "\n" + "-" * len(heading) + "\n")
        stream.write(
            "  Proved from the file, then judged a false positive in context.\n"
            "  Re-run with --no-ai to see them all.\n\n"
        )
        for rule_id, group in _by_rule(report.dismissals):
            stream.write(f"  {len(group):>4}  {rule_id}\n")
            for reason in sorted({d.reason for d in group if d.reason})[:2]:
                stream.write(f"        reason: {reason}\n")
        stream.write("\n")

    if report.skipped_rules:
        heading = f"Not checked ({len(report.skipped_rules)} rule(s) did not run)"
        stream.write(heading + "\n" + "-" * len(heading) + "\n")
        for skipped in report.skipped_rules:
            stream.write(f"  {skipped.rule_id}: {skipped.reason}\n")
        unlocked = next(
            (s.unlocked_by for s in report.skipped_rules if s.unlocked_by), ""
        )
        if unlocked:
            stream.write(f"\n  To enable: {unlocked}\n")
        stream.write("\n")


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
        stream.write("No inconsistencies found.\n\n")
        _write_omissions_markdown(report, stream)
        return

    stream.write(_counts_line(report) + "\n\n")

    if report.ai_summary:
        stream.write(f"{report.ai_summary}\n\n")

    _write_omissions_markdown(report, stream)

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


def _write_omissions_markdown(report: ValidationReport, stream: TextIO) -> None:
    if report.dismissals:
        stream.write(
            f"> **{len(report.dismissals)} rule finding(s) dismissed by the AI "
            f"layer** and not listed below. Each was proved from the file, then "
            f"judged a false positive in context. Re-run with `--no-ai` to see "
            f"them all.\n\n"
        )
        stream.write("| Dismissed | Rule | Reason given |\n| --- | --- | --- |\n")
        for rule_id, group in _by_rule(report.dismissals):
            reason = next((d.reason for d in group if d.reason), "")
            stream.write(f"| {len(group)} | `{rule_id}` | {_cell(reason)} |\n")
        stream.write("\n")

    if report.skipped_rules:
        stream.write(
            f"> **{len(report.skipped_rules)} check(s) did not run.** "
            f"{report.skipped_rules[0].reason}. Nothing below can be read as "
            f"evidence that these are clean.\n\n"
        )
        for skipped in report.skipped_rules:
            stream.write(f"- `{skipped.rule_id}`\n")
        if report.skipped_rules[0].unlocked_by:
            stream.write(f"\nTo enable: `{report.skipped_rules[0].unlocked_by}`\n")
        stream.write("\n")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _by_rule(dismissals: list) -> list[tuple[str, list]]:
    """Dismissals grouped by rule, commonest first.

    Grouped because they arrive in bulk: the AI dismisses a whole class of
    finding at once, and a flat list of several hundred would bury the report
    it is supposed to qualify.
    """
    buckets: dict[str, list] = defaultdict(list)
    for dismissal in dismissals:
        buckets[dismissal.rule_id or "(unattributed)"].append(dismissal)
    return sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))


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
