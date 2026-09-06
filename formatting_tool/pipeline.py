"""The workflow, end to end.

    master + messy deck(s)
        |
        v
    read decks -> derive the master spec        (extract/)
        |
        v
    deterministic validation layer              (rules/)
        |  inconsistencies in sizing, fonts, colours, ...
        v
    brand guidelines + inconsistencies + slides (ai/payload.py)
        |
        v
    AI validation layer                         (ai/client.py)
        |  structured JSON
        v
    merged list of inconsistencies              (report/)

Each stage is a function taking and returning dataclasses, so any stage can be
run, tested, or replaced on its own. The AI layer is optional: with --no-ai the
pipeline stops after the deterministic layer and still produces a report.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .ai.client import AIConfig, AIResult, AIValidationError, AIValidator
from .ai.payload import DEFAULT_BATCH_SIZE, build_batches, estimate_tokens, payload_to_text, ref_for
from .extract import derive_master_spec, read_deck
from .guidelines import load_guidelines
from .linemetrics import LineMetricsProvider, default_provider
from .render import SlideImages, render_deck
from .models import (
    DeckProfile,
    Provenance,
    Issue,
    MasterSpec,
    ValidationReport,
)
from .report.merge import merge_issues, summarize_report
from .rules import (
    RuleContext,
    build_default_rules,
    build_master_rules,
    run_rules,
    skipped_rules,
)

log = logging.getLogger(__name__)


@dataclass
class RunConfig:
    """Everything one invocation needs."""

    master: Path
    decks: list[Path]
    guidelines: Optional[Path] = None
    use_ai: bool = True
    ai_dry_run: bool = False
    payload_dir: Optional[Path] = None
    batch_size: int = DEFAULT_BATCH_SIZE
    min_confidence: float = 0.0
    render: bool = False        # attach rendered slides to the AI layer
    ai_debug: bool = False      # keep the raw AI exchange on the report
    ai: AIConfig = field(default_factory=AIConfig)


def run(config: RunConfig) -> ValidationReport:
    """Run the full workflow and return the merged report."""
    guidelines = load_guidelines(config.guidelines)
    master = read_deck(config.master)
    spec = derive_master_spec(master, guidelines)
    log.info(
        "master %s: %d slides, %d palette entries, %d approved fonts",
        master.name,
        len(master.slides),
        len(spec.palette),
        len(spec.allowed_fonts),
    )

    report = ValidationReport(
        master=master.name,
        decks=[Path(d).name for d in config.decks],
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        guidelines=str(config.guidelines) if config.guidelines else None,
        ai_enabled=config.use_ai and not config.ai_dry_run,
        inferred_values=guidelines.paths_with(Provenance.INFERRED),
    )
    if report.inferred_values:
        log.info(
            "%d brand value(s) were inferred from a master deck, not stated in "
            "the reference file: %s",
            len(report.inferred_values),
            ", ".join(report.inferred_values),
        )

    # The master's own layouts are checked once, not once per deck: an
    # incomplete layout is a defect in the template, and repeating it for
    # every deck under review would bury the deck findings.
    all_issues: list[Issue] = _run_master_layer(master, spec)
    log.info("%s: %d master layout finding(s)", master.name, len(all_issues))
    summaries: list[str] = []

    # Which checks will not run at all. It depends only on the guidelines, not
    # on any deck, so it is settled once here rather than per deck.
    report.skipped_rules = skipped_rules(
        RuleContext(deck=master, spec=spec), build_default_rules()
    )
    if report.skipped_rules:
        log.warning(
            "%d check(s) will not run: no brand guidelines were supplied. "
            "Colour, typeface, type-scale and safe-margin findings cannot "
            "appear in this report. Run extract-guidelines --from %s to "
            "produce one.",
            len(report.skipped_rules),
            master.name,
        )

    for deck_path in config.decks:
        deck = read_deck(deck_path)
        _warn_on_size_mismatch(deck, spec)
        _warn_on_foreign_theme(deck, spec)

        rule_issues = _run_rule_layer(deck, spec)
        log.info("%s: %d rule finding(s)", deck.name, len(rule_issues))

        ai_result = _run_ai_layer(deck, spec, rule_issues, config)
        if ai_result.summary:
            summaries.append(f"{deck.name}: {ai_result.summary}")

        merged = merge_issues(
            rule_issues=rule_issues,
            ai_issues=ai_result.issues,
            ref_lookup={ref_for(i): issue for i, issue in enumerate(rule_issues)},
            min_confidence=config.min_confidence,
        )
        all_issues.extend(merged)
        if config.ai_debug and ai_result.exchanges:
            report.ai_exchanges.extend(ai_result.exchanges)

        if ai_result.calls:
            log.info(
                "%s: %d AI call(s), %d in / %d out tokens, %d read from cache",
                deck.name,
                ai_result.calls,
                ai_result.input_tokens,
                ai_result.output_tokens,
                ai_result.cache_read_tokens,
            )

    report.issues = all_issues
    # Assigned here, once, on the finished list: a finding that has been
    # merged, absorbed and sorted is what the designer sees and ticks.
    for issue in report.issues:
        issue.id = issue.fingerprint()
    # Every rule finding reaches the report, so the only thing standing
    # between what was checked and what is printed is the skipped rules.
    report.stats = summarize_report(report)
    report.ai_summary = " ".join(summaries) or None
    return report


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #

def _run_rule_layer(
    deck: DeckProfile,
    spec: MasterSpec,
    metrics: Optional[LineMetricsProvider] = None,
) -> list[Issue]:
    ctx = RuleContext(deck=deck, spec=spec)
    rules = build_default_rules(metrics=metrics or default_provider(deck.path))
    return run_rules(ctx, rules)


def _run_master_layer(master: DeckProfile, spec: MasterSpec) -> list[Issue]:
    """Check the master's own layouts for completeness.

    The context is built on the master so that findings are attributed to the
    template file they belong to rather than to whichever deck was under
    review when they were found.
    """
    return run_rules(RuleContext(deck=master, spec=spec), build_master_rules())


def _run_ai_layer(
    deck: DeckProfile,
    spec: MasterSpec,
    rule_issues: list[Issue],
    config: RunConfig,
) -> AIResult:
    result = AIResult()
    if not config.use_ai:
        return result

    validator = AIValidator(spec, config.ai)
    images = _render(deck, config)

    try:
        for payload in build_batches(deck, rule_issues, config.batch_size, spec=spec):
            if config.ai_dry_run:
                _dump_payload(payload, validator, config)
                continue
            batch = images.for_slides(payload.get("batch", {}).get("slides", []))
            try:
                result.merge(validator.validate_batch(payload, deck.name, batch))
            except AIValidationError as exc:
                # The deterministic findings are still worth reporting, so the
                # run continues without the AI layer rather than failing.
                #
                # AIValidationError already carries the actionable message, so
                # the traceback through the SDK adds nothing at normal
                # verbosity. It is kept for -vv, where an unexpected SDK error
                # needs the frames.
                log.error(
                    "AI layer failed on %s: %s -- reporting rule findings only",
                    deck.name,
                    exc,
                    exc_info=log.isEnabledFor(logging.DEBUG),
                )
                if not log.isEnabledFor(logging.DEBUG):
                    log.info("run with -vv for the full traceback")
                break
    finally:
        images.cleanup()

    _check_evidence(result, images)
    return result


def _render(deck: DeckProfile, config: RunConfig) -> SlideImages:
    """Render the deck for the AI layer, or explain why there are no pictures."""
    if not config.render or config.ai_dry_run:
        return SlideImages(renderer="none", reason="rendering was not requested")

    images = render_deck(deck.path)
    if not images:
        log.warning(
            "%s: no rendered slides, so the AI layer sees geometry only (%s)",
            deck.name,
            images.reason,
        )
    return images


def _check_evidence(result: AIResult, images: SlideImages) -> None:
    """Demote any claim that says it looked at a slide there was no picture of.

    The label is the whole value of the render: a designer weighing "the text
    does not collide" needs to know whether that was seen or supposed. A model
    that says "render" for a slide it was never shown would quietly turn a
    guess into evidence, so the claim is kept and the label is not.
    """
    for issue in result.issues:
        if issue.evidence != "render":
            continue
        if issue.slide is None or issue.slide not in images.images:
            log.debug(
                "slide %s was not rendered; recording the finding as geometry",
                issue.slide,
            )
            issue.evidence = "geometry"


def _dump_payload(payload: dict, validator: AIValidator, config: RunConfig) -> None:
    """Write exactly what would be sent, for inspection and token budgeting."""
    batch = payload.get("batch", {})
    body = payload_to_text(payload)
    system = validator.system_instruction()

    log.info(
        "dry run batch %s/%s: ~%d system tokens (cached) + ~%d payload tokens",
        batch.get("index"),
        batch.get("of"),
        estimate_tokens(system),
        estimate_tokens(body),
    )

    if config.payload_dir is None:
        return
    config.payload_dir.mkdir(parents=True, exist_ok=True)
    name = f"{payload.get('deck', 'deck')}.batch{batch.get('index', 0)}.json"
    target = config.payload_dir / name.replace(" ", "_")
    target.write_text(
        json.dumps(
            {
                "model": config.ai.model,
                "effort": config.ai.effort,
                "system": system,
                "user": body,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log.info("wrote %s", target)


def _warn_on_foreign_theme(deck: DeckProfile, spec: MasterSpec) -> None:
    """A deck carrying a different theme is carrying a different brand.

    Every theme-bound colour and inherited typeface in the deck resolves
    through this theme, so when it differs from the master's, the deck is
    internally consistent against the wrong standard. Nothing downstream reads
    the deck's theme any more, but a reader should know why so much of the
    deck is being reported.
    """
    if not spec.theme_fonts and not spec.theme_colors:
        return
    if deck.theme_fonts == spec.theme_fonts and deck.theme_colors == spec.theme_colors:
        return
    log.warning(
        "%s carries its own theme (%s), not the master's (%s); it was built "
        "from another file and is measured against the master throughout",
        deck.name,
        ", ".join(f"{k}={v}" for k, v in sorted(deck.theme_fonts.items())) or "none",
        ", ".join(f"{k}={v}" for k, v in sorted(spec.theme_fonts.items())) or "none",
    )


def _warn_on_size_mismatch(deck: DeckProfile, spec: MasterSpec) -> None:
    """A deck built at the wrong slide size invalidates every geometry rule."""
    if (
        abs(deck.width_in - spec.width_in) > 0.05
        or abs(deck.height_in - spec.height_in) > 0.05
    ):
        log.warning(
            "%s is %.2f x %.2f in, master is %.2f x %.2f in; "
            "position and margin findings will be unreliable",
            deck.name,
            deck.width_in,
            deck.height_in,
            spec.width_in,
            spec.height_in,
        )
