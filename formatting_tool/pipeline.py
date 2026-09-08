"""The workflow, end to end.

    master + messy deck(s)
        |
        v
    read decks -> derive the master spec        (extract/)
        |
        v
    apply the master's layouts (optional)       (rebuild/)
        |  which layout each slide belongs on is read off its render
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

Applying the master comes FIRST when it is asked for, and everything after it
measures the restyled deck. That ordering is the point of it: putting content
into the master's placeholders resolves a great many findings by itself and
raises a few of its own, so a report made before it describes a deck nobody
will send. It also has to be first because findings name shapes by id, and
PowerPoint's placeholder matching renames and replaces them.
"""

from __future__ import annotations

import json
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .ai.client import AIConfig, AIResult, AIValidationError, AIValidator
from .ai.gemini import Exhausted
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
    # Put every slide on the master's layouts BEFORE anything is measured, and
    # measure the restyled deck.
    #
    # This is the order that makes the findings actionable. Applying the master
    # is what moves content into the master's placeholders, so it resolves a
    # great many geometry findings on its own and creates a few of its own
    # (content that now overlaps, a colour that now sits off the new theme's
    # palette). Measuring first and applying afterwards produces a report about
    # a deck that no longer exists: the findings name shapes by id, and
    # PowerPoint's placeholder matching renames and replaces them.
    # How many AI calls may be in flight at once. They are independent and
    # spend their time waiting, so running them one at a time was 74% of a
    # run's wall clock.
    #
    # Three, not more. At six this deck's key was rate-limited on three calls
    # out of five; those are waited out rather than dropped now, but waiting
    # sixty seconds to get a slide back is slower than not having asked for it
    # yet. Three keeps most of the speed-up and stays inside the quota.
    ai_concurrency: int = 3
    apply_master: bool = False
    # Where the restyled deck goes. None keeps it in a temporary directory,
    # which is right for a validate-only run: the report is the output.
    master_out: Optional[Path] = None


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
        # Named from the rules that actually opted out, not from a list kept
        # by hand. The hand-kept one said safe margins were among them, which
        # stopped being true when the frame started coming off the master's
        # own layouts, and the report then warned that a finding could not
        # appear directly above that finding.
        categories = sorted({
            rule.category.value.replace("_", " ")
            for rule in build_default_rules()
            if rule.id in {s.rule_id for s in report.skipped_rules}
        })
        log.warning(
            "%d check(s) will not run: no brand guidelines were supplied. "
            "No %s findings can appear in this report. Run "
            "extract-guidelines --from %s to produce one.",
            len(report.skipped_rules),
            ", ".join(categories) or "brand",
            master.name,
        )

    for deck_path in config.decks:
        deck = read_deck(deck_path)
        _warn_on_size_mismatch(deck, spec)
        _warn_on_foreign_theme(deck, spec)

        if config.apply_master:
            deck, applied = _apply_master_first(
                deck, deck_path, spec, config, master_profile=master
            )
            if applied is not None:
                report.master_applied.append(applied)

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
    # The values the findings were measured against, carried so that applying
    # a fix later does not have to read the master a second time. That second
    # read happens minutes after the run, by which time the file may be gone
    # -- an uploaded master lives in a temp directory that outlives nothing in
    # particular -- and even where it survives it could have changed
    # underneath, which would check a proposal against a master the report
    # never saw.
    report.spec = spec
    return report


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #

def _apply_master_first(
    deck: DeckProfile,
    deck_path: Path,
    spec: MasterSpec,
    config: RunConfig,
    master_profile: Optional[DeckProfile] = None,
) -> tuple[DeckProfile, Optional[dict]]:
    """Restyle the deck onto the master, then re-read it.

    Everything after this measures the restyled deck, which is the one the
    designer will send. Returns the deck to carry on with -- the original when
    the restyle could not run, so a host without PowerPoint still gets a report
    rather than an error.
    """
    from .rebuild import rebuild  # noqa: PLC0415 - avoids a circular import

    out = config.master_out or (
        Path(tempfile.mkdtemp(prefix="formatting-tool-master-")) / deck_path.name
    )

    # What the model reads off the picture, before anything is changed. Only
    # what a layout should be; the review of the restyled deck comes later.
    seen = _layout_picks(deck, spec, config)

    try:
        result = rebuild(
            config.master, deck_path, out, tuning=spec.guidelines.tuning,
            seen=seen, master_profile=master_profile, deck_profile=deck,
        )
    except Exception as exc:
        log.error(
            "could not put %s on %s's layouts (%s); measuring the deck as it "
            "arrived instead", deck.name, spec.source, exc,
        )
        return deck, None

    log.info(
        "%s: restyled onto %s by %s, now measuring the result",
        deck.name, spec.source, result.applied_by,
    )
    return read_deck(out), {
        "deck": deck.name,
        "output": str(out),
        "applied_by": result.applied_by,
        "layouts_used": result.layouts_used,
        "unmatched": [record.number for record in result.unmatched],
        "dropped": [str(shape) for shape in result.dropped],
        "stragglers": result.stragglers,
        "masters": result.masters,
    }


def _layout_picks(deck: DeckProfile, spec: MasterSpec, config: RunConfig):
    """The model's reading of which layout each slide belongs on, or nothing.

    Skipped without the AI layer or without a renderer, and the structural
    matcher decides alone, which is what it did before this existed.
    """
    if not config.use_ai or config.ai_dry_run:
        return []
    images = _render(deck, config)
    if not images:
        log.info("no layout pass: %s", images.reason)
        return []
    try:
        from .ai.layout import choose_layouts  # noqa: PLC0415 - lazy

        return choose_layouts(
            spec,
            sorted(images.images.items()),
            model=config.ai.model,
            thinking_budget=config.ai.thinking_budget,
            api_key_env=config.ai.api_key_env,
            concurrency=config.ai_concurrency,
        )
    finally:
        images.cleanup()


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
    """Review every batch, several at a time.

    The calls are independent -- one batch per slide by default, each carrying
    its own slide and its own findings -- and each spends its time waiting on
    the model rather than working. Run one after another they were 74% of a
    run's wall clock: five slides, sixty seconds each, five minutes of a
    seven-minute run with nothing happening locally.

    Nothing about a call changes, so nothing about the answers changes. What
    changes is only how many are in flight, and the results are put back in
    batch order before merging so two runs of the same deck still produce the
    same report in the same sequence.
    """
    result = AIResult()
    if not config.use_ai:
        return result

    validator = AIValidator(spec, config.ai)
    # Rendered here and not shared with the layout pass: that pass looks at the
    # deck as it arrived, this one at the deck after the master was applied.
    # Two different files, so two different sets of pictures.
    images = _render(deck, config)

    payloads = list(build_batches(deck, rule_issues, config.batch_size, spec=spec))
    try:
        if config.ai_dry_run:
            for payload in payloads:
                _dump_payload(payload, validator, config)
            return result

        for _index, part in _review_batches(validator, deck, payloads, images, config):
            result.merge(part)
    finally:
        images.cleanup()

    _check_evidence(result, images)
    return result


def _review_batches(
    validator: AIValidator,
    deck: DeckProfile,
    payloads: list[dict],
    images: SlideImages,
    config: RunConfig,
):
    """Every batch's result, in batch order, however they finished.

    Bounded rather than unbounded: the ceiling is what keeps a long deck from
    opening ninety sockets and running into the model's rate limit, which
    would turn a speed-up into a pile of retries.
    """
    workers = max(1, min(config.ai_concurrency, len(payloads)))
    # Set the moment the account turns out to be spent. Every remaining batch
    # then returns immediately instead of asking again and being refused
    # again: seventeen slides each learning the same thing separately is
    # seventeen copies of one message, and with a retry attached it was half
    # an hour of sleeping to reach the same report.
    spent: list[AIValidationError] = []

    reviewed = 0
    if workers == 1:
        for index, payload in enumerate(payloads):
            part = _review_one(validator, deck, payload, images, spent)
            if part is None:
                break
            reviewed += 1
            yield index, part
    else:
        done: dict[int, AIResult] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai") as pool:
            futures = {
                pool.submit(_review_one, validator, deck, payload, images, spent):
                    index
                for index, payload in enumerate(payloads)
            }
            for future in as_completed(futures):
                part = future.result()
                if part is not None:
                    done[futures[future]] = part
        reviewed = len(done)
        for index in sorted(done):
            yield index, done[index]

    if spent:
        log.error(
            "the AI layer stopped: %s. %d of %d batch(es) were reviewed; the "
            "rest of the report is the deterministic findings, which are "
            "unaffected.",
            spent[0], reviewed, len(payloads),
        )


def _review_one(
    validator: AIValidator,
    deck: DeckProfile,
    payload: dict,
    images: SlideImages,
    spent: Optional[list] = None,
) -> Optional[AIResult]:
    """One batch, or None when the AI layer could not answer for it.

    A failure costs that batch and no other. It used to stop the whole layer,
    which made sense when the calls ran in order and a broken key would have
    failed the rest anyway; running several at once, the rest are already in
    flight and throwing their answers away would lose work for nothing.
    """
    if spent:
        return None            # the account is out; do not ask again
    batch = images.for_slides(payload.get("batch", {}).get("slides", []))
    try:
        return validator.validate_batch(payload, deck.name, batch)
    except Exhausted as exc:
        # Recorded once and reported once by the caller. Nothing here is worth
        # retrying and nothing else will succeed either.
        if spent is not None and not spent:
            spent.append(exc)
        return None
    except AIValidationError as exc:
        # The deterministic findings are still worth reporting, so the run
        # carries on without this batch rather than failing.
        #
        # AIValidationError already carries the actionable message, so the
        # traceback through the SDK adds nothing at normal verbosity. It is
        # kept for -vv, where an unexpected SDK error needs the frames.
        log.error(
            "AI layer failed on %s slide(s) %s: %s -- reporting rule findings "
            "for those",
            deck.name,
            payload.get("batch", {}).get("slides"),
            exc,
            exc_info=log.isEnabledFor(logging.DEBUG),
        )
        if not log.isEnabledFor(logging.DEBUG):
            log.info("run with -vv for the full traceback")
        return None


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
