"""Command line entry point.

    formatting-tool validate --master brand.pptx --deck messy.pptx
    formatting-tool validate --master brand.pptx --deck a.pptx --deck b.pptx \
        --guidelines config/brand.yaml --format markdown --out review.md
    formatting-tool validate --master brand.pptx --deck messy.pptx \
        --ai-dry-run --payload-dir out/payloads
    formatting-tool profile --deck messy.pptx
    formatting-tool rules

Exit codes: 0 clean, 1 inconsistencies at or above --fail-on, 2 could not run.
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
from .brandbook import (
    BrandBookError,
    ExtractConfig,
    extract_from_pdf,
    infer_gaps,
    write_guidelines_yaml,
)
from .extract import DeckReadError, read_deck
from .guidelines import GuidelinesError
from .models import SEVERITY_RANK, Severity, ValidationReport, enum_safe
from .pipeline import RunConfig, run
from .report import summarize, write_json, write_markdown, write_text
from .rules import build_default_rules, describe_rules

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
        if args.command == "profile":
            return _cmd_profile(args)
        if args.command == "rules":
            return _cmd_rules(args)
        if args.command == "extract-guidelines":
            return _cmd_extract(args)
    except (DeckReadError, GuidelinesError, BrandBookError, AIValidationError) as exc:
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
        ai=AIConfig(model=args.model, effort=args.effort),
    )

    report = run(config)
    report = _filter_by_severity(report, args.min_severity)

    with _open_output(args.out) as stream:
        _WRITERS[args.format](report, stream)

    return _exit_code(report, args.fail_on)


def _cmd_extract(args: argparse.Namespace) -> int:
    """Read a brand reference PDF into a reviewable guidelines file."""
    result = extract_from_pdf(
        args.source,
        ExtractConfig(model=args.model, effort=args.effort),
    )

    inference = None
    if args.master is not None:
        master = read_deck(args.master)
        inference = infer_gaps(result.guidelines, master)

    document = write_guidelines_yaml(
        result.guidelines,
        evidence=result.evidence,
        pages=result.pages,
        inference=inference,
        rejections=result.rejections,
        master=Path(args.master).name if args.master else None,
        unspecified=result.unspecified,
    )

    with _open_output(args.out) as stream:
        stream.write(document)

    _report_extraction(result, inference, args.out)
    return 0


def _report_extraction(result, inference, out: Optional[Path]) -> None:
    """Tell the user what to review, on stderr so stdout stays the document."""
    authored = len(result.authored)
    inferred = len(inference.inferred) if inference else 0
    missing = len(inference.still_missing) if inference else len(result.missing)

    lines = [
        f"{authored} value(s) stated in {Path(result.source).name}",
        f"{inferred} inferred from the master deck",
        f"{missing} still unspecified",
        f"{len(result.rejections)} reading(s) discarded",
        f"~{result.input_tokens} in / {result.output_tokens} out tokens",
    ]
    print("  ".join(lines), file=sys.stderr)

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


def _cmd_profile(args: argparse.Namespace) -> int:
    """Dump the extracted DeckProfile. The first thing to check when a rule
    misfires: if the value is wrong here, the rule is not the problem."""
    deck = read_deck(args.deck)
    with _open_output(args.out) as stream:
        json.dump(enum_safe(_as_dict(deck)), stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    rules = describe_rules(build_default_rules())
    if args.format == "json":
        json.dump(rules, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    width = max(len(rule["id"]) for rule in rules)
    for rule in rules:
        needs = " (needs guidelines)" if rule["requires_guidelines"] == "True" else ""
        print(f"{rule['id']:<{width}}  {rule['severity']:<8}  {rule['description']}{needs}")
    return 0


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

    extract = sub.add_parser(
        "extract-guidelines",
        help="read a brand reference PDF into a reviewable guidelines file",
    )
    _add_shared(extract)
    extract.add_argument(
        "--from",
        dest="source",
        type=Path,
        required=True,
        help="the brand reference PDF",
    )
    extract.add_argument(
        "--master",
        type=Path,
        help=(
            "master deck used to infer values the reference file does not "
            "state; without it those values are left unspecified"
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
    report.stats = summarize(report.issues)
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
