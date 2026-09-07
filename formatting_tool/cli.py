"""Command line entry point.

    formatting-tool validate --master brand.pptx --deck messy.pptx
    formatting-tool validate --master brand.pptx --deck a.pptx --deck b.pptx \
        --guidelines config/brand.yaml --format markdown --out review.md
    formatting-tool validate --master brand.pptx --deck messy.pptx \
        --ai-dry-run --payload-dir out/payloads
    formatting-tool rebuild --master brand.pptx --deck messy.pptx \
        --out out/messy.rebuilt.pptx
    formatting-tool extract-guidelines --from brandbook.pdf --master brand.pptx
    formatting-tool extract-guidelines --from brand.pptx --out config/brand.yaml
    formatting-tool profile --deck messy.pptx
    formatting-tool rules
    formatting-tool ui

Exit codes: 0 clean, 1 inconsistencies at or above --fail-on (or, for rebuild
--strict, a slide that matched no layout), 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence, TextIO

from . import __version__
from .ai.client import AIConfig, DEFAULT_EFFORT, DEFAULT_MODEL
from .ai.gemini import AIValidationError
from .ai.payload import DEFAULT_BATCH_SIZE
from .apply import ApplyError, apply_fixes, fixable, fixer_for, why_not_fixable
from .classify import fit_slide, layout_coverage, missing_kinds
from .brandbook import (
    BrandBookError,
    ExtractConfig,
    extract_guidelines,
    infer_gaps,
    write_guidelines_yaml,
)
from .extract import DeckReadError, read_deck
from .guidelines import GuidelinesError, load_guidelines
from .models import SEVERITY_RANK, Severity, ValidationReport, enum_safe
from .pipeline import RunConfig, run
from .rebuild import RebuildError, RebuildResult, rebuild
from .rebuild.matcher import structure_score
from .report import (
    ReportError,
    load_report,
    summarize_report,
    write_json,
    write_markdown,
    write_text,
)
from .rules import build_default_rules, build_master_rules, describe_rules

log = logging.getLogger("formatting_tool")

_WRITERS = {
    "json": write_json,
    "markdown": write_markdown,
    "text": write_text,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose, args.quiet)
    _load_dotenv()

    try:
        if args.command == "validate":
            return _cmd_validate(args)
        if args.command == "classify":
            return _cmd_classify(args)
        if args.command == "apply":
            return _cmd_apply(args)
        if args.command == "rebuild":
            return _cmd_rebuild(args)
        if args.command == "profile":
            return _cmd_profile(args)
        if args.command == "rules":
            return _cmd_rules(args)
        if args.command == "extract-guidelines":
            return _cmd_extract(args)
        if args.command == "ui":
            return _cmd_ui(args)
    except (
        DeckReadError,
        GuidelinesError,
        BrandBookError,
        RebuildError,
        ApplyError,
        ReportError,
        AIValidationError,
    ) as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("interrupted")
        return 2

    parser.print_help()
    return 2


def _load_dotenv() -> None:
    """Pull a local .env into the environment, if there is one.

    Real environment variables win: a key exported in the shell is the more
    deliberate of the two, and CI has no .env to read. Missing file, missing
    package, either is fine -- the SDK credential chain is still there.
    """
    try:
        from dotenv import find_dotenv, load_dotenv  # noqa: PLC0415
    except ImportError:
        log.debug("python-dotenv not installed; reading credentials from the environment only")
        return
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)
        log.debug("loaded %s", path)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def _cmd_validate(args: argparse.Namespace) -> int:
    config = RunConfig(
        master=args.master,
        decks=list(args.deck),
        guidelines=args.guidelines,
        use_ai=not args.no_ai,
        ai_dry_run=args.ai_dry_run,
        payload_dir=args.payload_dir,
        batch_size=args.batch_size,
        min_confidence=args.min_confidence,
        render=args.render,
        ai_debug=args.ai_debug,
        ai=AIConfig(model=args.model, effort=args.effort),
    )

    report = run(config)
    report = _filter_by_severity(report, args.min_severity)

    with _open_output(args.out) as stream:
        _WRITERS[args.format](report, stream)

    return _exit_code(report, args.fail_on)


def _cmd_extract(args: argparse.Namespace) -> int:
    """Read a brand reference into a reviewable guidelines file.

    The reference is a PDF brand book or an approved .pptx. When it is a deck,
    it is also the deck the gaps are inferred from -- naming a second one with
    --master would mean two conflicting sets of observations with no way to
    say which won.
    """
    source_is_deck = args.source.suffix.lower() == ".pptx"
    if source_is_deck and args.master is not None and args.master != args.source:
        raise BrandBookError(
            f"--from {args.source.name} is already a deck, so --master "
            f"{args.master.name} would be a second source of observations. "
            "Drop --master, or extract from the brand book PDF and keep "
            "--master for the deck."
        )

    result = extract_guidelines(
        args.source,
        ExtractConfig(model=args.model, effort=args.effort),
    )

    master_path = args.source if source_is_deck else args.master
    inference = None
    if master_path is not None:
        inference = infer_gaps(result.guidelines, read_deck(master_path))

    document = write_guidelines_yaml(
        result.guidelines,
        evidence=result.evidence,
        pages=result.pages,
        inference=inference,
        rejections=result.rejections,
        master=Path(master_path).name if master_path else None,
        unspecified=result.unspecified,
    )

    with _open_output(args.out) as stream:
        stream.write(document)

    _report_extraction(result, inference, args.out)
    return 0


def _report_extraction(result, inference, out: Optional[Path]) -> None:
    """Tell the user what to review, on stderr so stdout stays the document."""
    source = Path(result.source)
    from_deck = source.suffix.lower() == ".pptx"
    inferred = len(inference.inferred) if inference else 0
    missing = len(inference.still_missing) if inference else len(result.missing)

    lines = [
        f"{len(result.authored)} value(s) stated in {source.name}",
        f"{inferred} inferred from {source.name if from_deck else 'the master deck'}",
        f"{missing} still unspecified",
    ]
    if not from_deck:
        # A deck extraction makes no model call, so neither number means
        # anything and a "0 reading(s) discarded" line only reads as an error.
        lines.append(f"{len(result.rejections)} reading(s) discarded")
        lines.append(f"~{result.input_tokens} in / {result.output_tokens} out tokens")
    print("  ".join(lines), file=sys.stderr)

    if from_deck:
        print(
            "\nA deck demonstrates, it does not state. Every value above is "
            "inferred and\nthe report will hedge findings that rest on it "
            "until you confirm the value\nand mark it `authored` in the "
            "provenance block.",
            file=sys.stderr,
        )

    if inference and inference.inferred:
        print("\nInferred, so worth checking first:", file=sys.stderr)
        for path in inference.paths:
            print(f"  {path}: {inference.inferred[path]}", file=sys.stderr)

    if out is not None:
        print(f"\nReview {out}, correct what is wrong, then:", file=sys.stderr)
        print(
            f"  python -m formatting_tool validate --master MASTER.pptx "
            f"--deck DECK.pptx --guidelines {out}",
            file=sys.stderr,
        )


def _cmd_apply(args: argparse.Namespace) -> int:
    """Apply the findings a designer ticked, and optionally the master layouts."""
    report = load_report(args.report)

    if args.list:
        _print_fix_menu(report)
        return 0

    if not args.fix and not args.all and args.master is None:
        raise ApplyError(
            "nothing to do: pass --fix ID (repeatable) to apply chosen "
            "findings, --all for every mechanical fix, or --master to rebuild "
            "onto the master layouts. Run with --list to see the ids."
        )

    selected = None if args.all else list(args.fix or [])
    result = apply_fixes(
        deck=args.deck,
        issues=report.issues,
        out=args.out,
        selected=selected,
        master=args.master,
        tuning=load_guidelines(args.guidelines).tuning,
        tolerances=load_guidelines(args.guidelines).tolerances,
    )
    _report_apply(result)
    return 1 if result.skipped and args.strict else 0


def _print_fix_menu(report: ValidationReport) -> None:
    """The tick list: what can be applied, and what cannot, with reasons."""
    can = fixable(report.issues)
    cannot = [i for i in report.issues if fixer_for(i) is None]

    print(f"{len(can)} of {len(report.issues)} finding(s) can be applied\n")
    if can:
        width = max(len(i.rule_id or "") for i in can)
        for issue in can:
            where = f"slide {issue.slide}" if issue.slide else "deck"
            print(
                f"  {issue.id}  {issue.rule_id or '':<{width}}  {where:>8}  "
                f"{issue.shape or '-'}"
            )
    if cannot:
        print(f"\n{len(cannot)} need a designer:")
        seen: set[str] = set()
        for issue in cannot:
            key = issue.rule_id or issue.category.value
            if key in seen:
                continue
            seen.add(key)
            print(f"  {key}: {why_not_fixable(issue)}")

    print(
        "\nApply them with:\n"
        "  python -m formatting_tool apply --deck DECK.pptx --report REPORT.json "
        "--out FIXED.pptx --fix ID --fix ID"
    )


def _report_apply(result) -> None:
    out = sys.stderr
    print(f"{result.deck} -> {result.output}", file=out)
    print(
        f"  {len(result.applied)} fix(es) applied, {len(result.skipped)} skipped",
        file=out,
    )
    for outcome in result.applied:
        print(f"    {outcome}", file=out)
    if result.skipped:
        print("\n  Skipped:", file=out)
        for outcome in result.skipped:
            print(f"    {outcome}", file=out)
    if result.rebuilt is not None:
        print("", file=out)
        _report_rebuild(result.rebuilt)


def _cmd_classify(args: argparse.Namespace) -> int:
    """Say what each slide is and which master layout serves it."""
    master = read_deck(args.master)
    deck = read_deck(args.deck)
    floor = load_guidelines(args.guidelines).tuning.layout_match_floor

    fits = [
        fit_slide(
            slide, deck, master.layouts, score_structure=structure_score, floor=floor
        )
        for slide in deck.slides
    ]
    coverage = layout_coverage(master.layouts)
    missing = missing_kinds(fits, master.layouts)

    if args.format == "json":
        json.dump(
            {
                "master": master.name,
                "deck": deck.name,
                "layouts": {
                    kind.value: names for kind, names in sorted(
                        coverage.items(), key=lambda item: item[0].value
                    )
                },
                "kinds_the_master_cannot_serve": [k.value for k in missing],
                "slides": [
                    {
                        "slide": fit.slide,
                        "kind": fit.kind.value,
                        "confidence": round(fit.classification.confidence, 2),
                        "why": fit.classification.basis,
                        "layout": fit.layout_name,
                        "layout_kind": fit.layout_kind.value,
                        "fit": fit.fit,
                        "score": round(fit.score, 2),
                        "basis": fit.basis,
                    }
                    for fit in fits
                ],
            },
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 0

    _print_classification(master, deck, fits, coverage, missing)
    return 1 if (missing and args.strict) else 0


def _print_classification(master, deck, fits, coverage, missing) -> None:
    """A slide-by-slide table, then what the master cannot serve."""
    print(f"Deck:   {deck.name} ({len(deck.slides)} slides)")
    print(f"Master: {master.name} ({len(master.layouts)} layouts)\n")

    print("The master offers:")
    for kind, names in sorted(coverage.items(), key=lambda item: item[0].value):
        print(f"  {kind.value:<9} {', '.join(names)}")
    print()

    width = max((len(f.layout_name or '') for f in fits), default=10)
    header = f"{'#':>3}  {'is a':<9} {'conf':>4}  {'fits':<9} {'layout':<{width}}"
    print(header)
    print("-" * len(header))
    for fit in fits:
        mark = {"good": "", "loose": "  <-- check", "none": "  <-- no such layout"}[fit.fit]
        print(
            f"{fit.slide:>3}  {fit.kind.value:<9} "
            f"{fit.classification.confidence:>4.2f}  {fit.fit:<9} "
            f"{fit.layout_name or '-':<{width}}{mark}"
        )
        print(f"     because {fit.classification.basis}")
        print(f"     layout: {fit.basis}")

    if missing:
        print(
            f"\nThe master has no layout for: "
            f"{', '.join(k.value for k in missing)}."
        )
        print(
            "  Those slides were put on the closest layout there is, which is\n"
            "  not the same as a layout that fits. Either add the missing\n"
            "  layouts to the master, or expect to lay these out by hand."
        )


def _cmd_rebuild(args: argparse.Namespace) -> int:
    """Rebuild a deck onto the master and say what needs a designer's eye."""
    tuning = load_guidelines(args.guidelines).tuning
    result = rebuild(args.master, args.deck, args.out, tuning=tuning)
    _report_rebuild(result)
    return 1 if (result.unmatched or result.dropped) and args.strict else 0


def _report_rebuild(result: RebuildResult) -> None:
    """Print what moved and what did not, on stderr.

    Weighted toward what is still wrong. A rebuild that reports only its
    successes invites the deck being sent on unchecked, and the two things it
    cannot do -- place loose content into the layout's regions, and carry a
    chart across -- are exactly the two a designer has to finish by hand.
    """
    out = sys.stderr
    print(f"{result.deck} rebuilt onto {result.master} -> {result.output}", file=out)
    print(
        f"  {len(result.slides)} slide(s), "
        f"{result.sample_slides_removed} master sample slide(s) dropped",
        file=out,
    )
    for name, count in sorted(result.layouts_used.items()):
        print(f"    {count:>3}  {name}", file=out)

    if result.size_note:
        print(f"\n  Canvas: {result.size_note}", file=out)

    print("\n  Every slide, by the job it does:", file=out)
    for record in result.slides:
        mark = "" if record.confident else "   <-- check"
        print(
            f"    slide {record.number:>3}  {record.kind.value:<9} -> "
            f"{record.target_layout}{mark}",
            file=out,
        )

    if result.unmatched:
        print(
            f"\n  {len(result.unmatched)} slide(s) had no good layout match; "
            "check these first:",
            file=out,
        )
        for record in result.unmatched:
            print(
                f"    slide {record.number} ({record.kind.value}): {record.basis}",
                file=out,
            )

    unfilled = [r for r in result.slides if r.unfilled]
    if unfilled:
        print(
            f"\n  {len(unfilled)} slide(s) have empty layout regions, because "
            "the copy sits in\n  loose text boxes that were moved across as "
            "they are. Deciding which box\n  belongs in which region is a "
            "judgement call, so it is left to you:",
            file=out,
        )
        for record in unfilled:
            print(
                f"    slide {record.number}: {len(record.unfilled)} empty "
                f"({', '.join(record.unfilled)})",
                file=out,
            )

    if result.dropped:
        print(
            f"\n  {len(result.dropped)} shape(s) could not be carried across "
            "and are missing\n  from the rebuild. Copy them over by hand:",
            file=out,
        )
        for shape in result.dropped:
            print(f"    {shape}", file=out)

    print(
        f"\nCheck the result:\n"
        f"  python -m formatting_tool validate --master {result.master} "
        f"--deck {result.output}",
        file=out,
    )


def _cmd_profile(args: argparse.Namespace) -> int:
    """Dump the extracted DeckProfile. The first thing to check when a rule
    misfires: if the value is wrong here, the rule is not the problem."""
    deck = read_deck(args.deck)
    with _open_output(args.out) as stream:
        json.dump(enum_safe(_as_dict(deck)), stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    rules = describe_rules(build_default_rules() + build_master_rules())
    if args.format == "json":
        json.dump(rules, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    width = max(len(rule["id"]) for rule in rules)
    for rule in rules:
        needs = " (needs guidelines)" if rule["requires_guidelines"] == "True" else ""
        print(f"{rule['id']:<{width}}  {rule['severity']:<8}  {rule['description']}{needs}")
    return 0


def _cmd_ui(args: argparse.Namespace) -> int:
    """Serve the browser UI. Imported here so the CLI does not pay for the
    web module on every other invocation."""
    from .web import serve  # noqa: PLC0415

    return serve(
        port=args.port,
        host=args.host,
        root=args.root,
        open_browser=not args.no_browser,
        reload=args.reload,
    )


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="formatting-tool",
        description="Validate a deck against a master deck and brand guidelines.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_shared(parser)
    # The shared flags use SUPPRESS defaults so that `-v` before the
    # subcommand survives; a subparser default would otherwise overwrite it.
    parser.set_defaults(verbose=0, quiet=False)

    sub = parser.add_subparsers(dest="command")

    validate = sub.add_parser(
        "validate", help="run both validation layers and report inconsistencies"
    )
    _add_shared(validate)
    validate.add_argument(
        "--master", type=Path, required=True, help="the approved master deck"
    )
    validate.add_argument(
        "--deck",
        type=Path,
        required=True,
        action="append",
        help="a deck to check; repeat for several",
    )
    validate.add_argument(
        "--guidelines",
        type=Path,
        help="brand guidelines .yaml or .json (see config/)",
    )
    validate.add_argument(
        "--format", choices=sorted(_WRITERS), default="text", help="output format"
    )
    validate.add_argument("--out", type=Path, help="write to a file instead of stdout")
    validate.add_argument(
        "--no-ai", action="store_true", help="deterministic layer only"
    )
    validate.add_argument(
        "--render",
        action="store_true",
        help=(
            "render each slide and show it to the AI layer, so it can judge "
            "what only exists once drawn: clipped text, real collisions, "
            "contrast over an image. Needs PowerPoint and pywin32 on Windows; "
            "without them the run continues on geometry alone"
        ),
    )
    validate.add_argument(
        "--ai-debug",
        action="store_true",
        help=(
            "keep the raw AI exchange on the report, per batch: which slides "
            "and images went out, how many rule findings went with them, and "
            "the model's verbatim JSON back. The only way to tell a model that "
            "missed something from a payload that never described it"
        ),
    )
    validate.add_argument(
        "--ai-dry-run",
        action="store_true",
        help="build the AI payload and report its size without calling the API",
    )
    validate.add_argument(
        "--payload-dir", type=Path, help="with --ai-dry-run, write payloads here"
    )
    validate.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model id")
    validate.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=DEFAULT_EFFORT,
        help="how hard the AI layer thinks; raises cost with it",
    )
    validate.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="slides per AI call",
    )
    validate.add_argument(
        "--min-severity",
        choices=[s.value for s in Severity],
        default=Severity.INFO.value,
        help="drop findings below this severity",
    )
    validate.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="drop AI findings below this confidence (0.0-1.0)",
    )
    validate.add_argument(
        "--fail-on",
        choices=[s.value for s in Severity] + ["never"],
        default="never",
        help="exit 1 when a finding at this severity or above is present",
    )

    apply_cmd = sub.add_parser(
        "apply",
        help="apply the findings a designer ticked, and the master layouts",
        description=(
            "Make exactly the changes that were asked for. Findings are chosen "
            "by the id the report gives them, so a designer ticks a list and "
            "the tool applies that list and nothing else. With --master the "
            "corrected deck is then rebuilt onto the master's layouts. The "
            "input deck is never modified."
        ),
    )
    _add_shared(apply_cmd)
    apply_cmd.add_argument(
        "--deck", type=Path, required=True, help="the deck the report was run against"
    )
    apply_cmd.add_argument(
        "--report", type=Path, required=True, help="the JSON report from validate"
    )
    apply_cmd.add_argument("--out", type=Path, help="where to write the corrected deck")
    apply_cmd.add_argument(
        "--fix",
        action="append",
        metavar="ID",
        help="apply this finding; repeat for each one the designer ticked",
    )
    apply_cmd.add_argument(
        "--all",
        action="store_true",
        help="apply every finding that has a fixer, without picking",
    )
    apply_cmd.add_argument(
        "--master",
        type=Path,
        help="also rebuild the corrected deck onto this master's layouts",
    )
    apply_cmd.add_argument(
        "--guidelines",
        type=Path,
        help="brand guidelines .yaml or .json; only its tuning block is read",
    )
    apply_cmd.add_argument(
        "--list",
        action="store_true",
        help="show the ids that can be applied, and what needs a designer",
    )
    apply_cmd.add_argument(
        "--strict", action="store_true", help="exit 1 when a fix was skipped"
    )

    classify_cmd = sub.add_parser(
        "classify",
        help="say what each slide is and which master layout fits it",
        description=(
            "Classify every slide by the job it does -- cover, agenda, section, "
            "content, columns, diagram, closing -- and name the layout in the "
            "master that serves it. Reports the kinds of slide the master has "
            "no layout for. Reads only; writes nothing."
        ),
    )
    _add_shared(classify_cmd)
    classify_cmd.add_argument(
        "--master", type=Path, required=True, help="the approved master deck"
    )
    classify_cmd.add_argument(
        "--deck", type=Path, required=True, help="the deck to classify"
    )
    classify_cmd.add_argument(
        "--guidelines", type=Path,
        help="brand guidelines .yaml or .json; only tuning.layout_match_floor is read",
    )
    classify_cmd.add_argument(
        "--format", choices=["text", "json"], default="text", help="output format"
    )
    classify_cmd.add_argument(
        "--strict", action="store_true",
        help="exit 1 when the master has no layout for a kind the deck uses",
    )

    rebuild_cmd = sub.add_parser(
        "rebuild",
        help="rebuild a deck onto the master's layouts and write a new file",
        description=(
            "Open the master as the base file and recreate every slide on the "
            "layout it belongs to. Placeholder copy moves across and loses its "
            "direct formatting, so the master's geometry and type take effect; "
            "loose shapes are transplanted as they are. The input deck is "
            "never modified."
        ),
    )
    _add_shared(rebuild_cmd)
    rebuild_cmd.add_argument(
        "--master", type=Path, required=True, help="the approved master deck"
    )
    rebuild_cmd.add_argument(
        "--deck", type=Path, required=True, help="the deck to rebuild"
    )
    rebuild_cmd.add_argument(
        "--out", type=Path, required=True, help="where to write the rebuilt deck"
    )
    rebuild_cmd.add_argument(
        "--guidelines",
        type=Path,
        help=(
            "brand guidelines .yaml or .json; only its tuning block is read, "
            "for layout_match_floor"
        ),
    )
    rebuild_cmd.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when a slide matched no layout or a shape was left behind",
    )

    extract = sub.add_parser(
        "extract-guidelines",
        help="read a brand reference into a reviewable guidelines file",
    )
    _add_shared(extract)
    extract.add_argument(
        "--from",
        dest="source",
        type=Path,
        required=True,
        help=(
            "the brand reference: a .pdf brand book, whose stated rules are "
            "read as authored, or an approved .pptx, whose values are read as "
            "inferred"
        ),
    )
    extract.add_argument(
        "--master",
        type=Path,
        help=(
            "master deck used to infer values the reference file does not "
            "state; without it those values are left unspecified. Not accepted "
            "when --from is already a deck"
        ),
    )
    extract.add_argument(
        "--out", type=Path, help="write the guidelines file here instead of stdout"
    )
    extract.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model id")
    extract.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=DEFAULT_EFFORT,
        help="how hard the extractor thinks; raises cost with it",
    )

    profile = sub.add_parser(
        "profile", help="dump what the reader extracted from a deck"
    )
    _add_shared(profile)
    profile.add_argument("--deck", type=Path, required=True)
    profile.add_argument("--out", type=Path)

    rules = sub.add_parser("rules", help="list the deterministic rules")
    _add_shared(rules)
    rules.add_argument("--format", choices=["text", "json"], default="text")

    ui = sub.add_parser("ui", help="serve the browser UI on localhost")
    _add_shared(ui)
    ui.add_argument("--port", type=int, default=8000, help="first port to try")
    ui.add_argument(
        "--host", default="127.0.0.1", help="interface to bind; loopback by default"
    )
    ui.add_argument(
        "--root",
        type=Path,
        default=None,
        help="project root the guidelines dropdown reads config/ from",
    )
    ui.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )
    ui.add_argument(
        "--reload",
        action="store_true",
        help=(
            "restart the server when a source file changes. Without it a "
            "server started before an edit keeps serving the old code, "
            "silently, for as long as it runs"
        ),
    )

    return parser


def _add_shared(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS,
        help="repeat for debug logging",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="errors only",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _configure_logging(verbose: int, quiet: bool) -> None:
    level = logging.WARNING
    if quiet:
        level = logging.ERROR
    elif verbose == 1:
        level = logging.INFO
    elif verbose > 1:
        level = logging.DEBUG
    logging.basicConfig(
        level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    # The Gemini SDK narrates its own internals -- automatic function calling
    # is on, and a recommendation not to use it that does not apply to a
    # single-shot generate_content. None of it is actionable, and it drowns
    # our own lines. Full debug logging (-vv) still shows it.
    if level > logging.DEBUG:
        logging.getLogger("google_genai").setLevel(logging.ERROR)


class _StdoutProxy:
    """Context manager that yields stdout without closing it."""

    def __enter__(self) -> TextIO:
        return sys.stdout

    def __exit__(self, *exc_info) -> bool:
        return False


def _open_output(path: Optional[Path]):
    if path is None:
        return _StdoutProxy()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("w", encoding="utf-8")


def _filter_by_severity(
    report: ValidationReport, minimum: str
) -> ValidationReport:
    threshold = SEVERITY_RANK[minimum]
    report.issues = [
        issue
        for issue in report.issues
        if SEVERITY_RANK[issue.severity.value] <= threshold
    ]
    # Stats are computed over what the report actually shows, so the header
    # counts and the body cannot disagree.
    report.stats = summarize_report(report)
    return report


def _exit_code(report: ValidationReport, fail_on: str) -> int:
    if fail_on == "never":
        return 0
    threshold = SEVERITY_RANK[fail_on]
    triggered = any(
        SEVERITY_RANK[issue.severity.value] <= threshold for issue in report.issues
    )
    return 1 if triggered else 0


def _as_dict(deck) -> dict:
    from dataclasses import asdict

    return asdict(deck)


if __name__ == "__main__":
    raise SystemExit(main())
