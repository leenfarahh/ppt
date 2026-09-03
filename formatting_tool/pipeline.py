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
from .models import (
    DeckProfile,
    Provenance,
    Issue,
    MasterSpec,
    ValidationReport,
)
from .report.merge import merge_issues, summarize
from .rules import RuleContext, build_default_rules, run_rules

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

    all_issues: list[Issue] = []
    summaries: list[str] = []

    for deck_path in config.decks:
        deck = read_deck(deck_path)
        _warn_on_size_mismatch(deck, spec)

        rule_issues = _run_rule_layer(deck, spec)
        log.info("%s: %d rule finding(s)", deck.name, len(rule_issues))

        ai_result = _run_ai_layer(deck, spec, rule_issues, config)
        if ai_result.summary:
            summaries.append(f"{deck.name}: {ai_result.summary}")

        merged = merge_issues(
            rule_issues=rule_issues,
            ai_issues=ai_result.issues,
            dismissals=ai_result.dismissals,
            ref_lookup={ref_for(i): issue for i, issue in enumerate(rule_issues)},
            min_confidence=config.min_confidence,
        )
        all_issues.extend(merged)

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
    report.stats = summarize(all_issues)
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

    for payload in build_batches(deck, rule_issues, config.batch_size):
        if config.ai_dry_run:
            _dump_payload(payload, validator, config)
            continue
        try:
            result.merge(validator.validate_batch(payload, deck.name))
        except AIValidationError:
            # The deterministic findings are still worth reporting, so the run
            # continues without the AI layer rather than failing outright.
            log.exception("AI layer failed on %s; reporting rule findings only", deck.name)
            break
    return result


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
