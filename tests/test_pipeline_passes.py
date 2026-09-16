"""The deterministic layer in two passes, and the order inside them.

The order is the whole point, so it is asserted rather than left to the
registry's line numbering. Three things have to hold and nothing in the rule
list makes them obvious to a reader adding a rule:

1. The master is applied before anything is measured. A report written before
   the restyle describes a deck nobody will send.
2. Pass 2 compares the slides against each other, and it runs after pass 1 has
   read every one of them.
3. The orphan check is last. It is the only rule whose subject is not in the
   file -- where a line breaks is the renderer's decision -- so anything that
   moves a box or changes a size above it invalidates its answer.
"""

from __future__ import annotations

from pathlib import Path

from formatting_tool.models import (
    BrandGuidelines,
    DeckProfile,
    Geometry,
    ParagraphProfile,
    RunProfile,
    ShapeProfile,
    SlideProfile,
    TextRole,
)
from formatting_tool.rules import (
    build_default_rules,
    build_first_pass_rules,
    build_master_rules,
    build_second_pass_rules,
)

# Rules whose verdict is about the deck rather than about a slide: each one
# derives a norm from every slide and reports the departures from it.
CROSS_SLIDE = {
    "size.role.inconsistent",
    "color.inconsistent_variants",
    "title.position_inconsistent",
    "space.alignment_grid",
}

ORPHANS = "typography.orphan_widow"


# --------------------------------------------------------------------------- #
# The split
# --------------------------------------------------------------------------- #

def test_the_two_passes_partition_the_rule_set() -> None:
    """Splitting the registry must not quietly drop a check. `rules`, the AI
    prompt and the recheck after fixes all still ask for the whole set."""
    first = [rule.id for rule in build_first_pass_rules()]
    second = [rule.id for rule in build_second_pass_rules()]
    every = [rule.id for rule in build_default_rules()]

    assert set(first).isdisjoint(second)
    assert first + second == every
    assert len(every) == len(set(every))


def test_slide_against_slide_checks_run_in_the_second_pass() -> None:
    first = {rule.id for rule in build_first_pass_rules()}
    second = {rule.id for rule in build_second_pass_rules()}

    assert CROSS_SLIDE <= second
    assert not (CROSS_SLIDE & first)


def test_the_orphan_check_is_the_last_thing_the_layer_does() -> None:
    """Not merely present in pass 2 -- at the end of it, and therefore at the
    end of the whole deterministic layer."""
    assert build_second_pass_rules()[-1].id == ORPHANS
    assert build_default_rules()[-1].id == ORPHANS
    assert ORPHANS not in {rule.id for rule in build_first_pass_rules()}


def test_the_orphan_check_is_after_every_cross_slide_check() -> None:
    order = [rule.id for rule in build_second_pass_rules()]
    assert all(order.index(rule) < order.index(ORPHANS) for rule in CROSS_SLIDE)


def test_the_master_rules_stay_out_of_both_passes() -> None:
    """They judge the template, not the deck, and the pipeline runs them once
    rather than once per deck."""
    master = {rule.id for rule in build_master_rules()}
    assert not (master & {rule.id for rule in build_default_rules()})


def test_both_passes_take_the_same_metrics_provider() -> None:
    """Whatever the caller chose -- a real renderer, or NullLineMetrics for a
    fast pass -- has to reach the rules in both passes, or turning line
    metrics off would silently only apply to half of them."""
    from formatting_tool.linemetrics import NullLineMetrics

    null = NullLineMetrics()
    for rule in build_first_pass_rules(null) + build_second_pass_rules(null):
        if hasattr(rule, "metrics"):
            assert rule.metrics is null, rule.id


# --------------------------------------------------------------------------- #
# The pipeline runs them in that order
# --------------------------------------------------------------------------- #

def _deck(name: str = "messy.pptx") -> DeckProfile:
    title = ShapeProfile(
        shape_id=2,
        name="Title 1",
        shape_type="PLACEHOLDER (14)",
        geometry=Geometry(left_in=0.6, top_in=0.5, width_in=11.0, height_in=1.2),
        placeholder_type="TITLE (13)",
        role=TextRole.TITLE,
        text="A title",
        paragraphs=[
            ParagraphProfile(
                text="A title",
                runs=[RunProfile(text="A title", font_name="Inter Tight",
                                 size_pt=36.0, color_hex="101820")],
            )
        ],
    )
    return DeckProfile(
        path=name,
        width_in=13.333,
        height_in=7.5,
        slides=[SlideProfile(number=1, layout_name="Title Slide", shapes=[title])],
        theme_fonts={"major": "Inter Tight", "minor": "Inter"},
    )


def _spec():
    from formatting_tool.extract.master_spec import derive_master_spec

    return derive_master_spec(_deck("master.pptx"), BrandGuidelines(
        name="test", palette={"ink": "101820"}, allowed_fonts=["Inter Tight"],
    ))


def test_the_pipeline_applies_the_master_then_both_passes_then_the_ai(
    monkeypatch,
) -> None:
    """The sequence, recorded as it happens rather than inferred.

    A run with `apply_master` on has to restyle before it measures, measure
    twice, and hand BOTH passes' findings to the AI layer -- a deck-wide
    finding the model never sees cannot be restated, scored, or held back as
    a deliberate colour choice.
    """
    from formatting_tool import pipeline as pl
    from formatting_tool.ai.client import AIResult
    from formatting_tool.models import Category, Issue, Severity, Source

    calls: list[str] = []

    def issue(rule_id: str) -> Issue:
        return Issue(
            category=Category.SPACE, severity=Severity.WARNING,
            message=rule_id, source=Source.RULE, rule_id=rule_id,
            deck="messy.pptx",
        )

    monkeypatch.setattr(pl, "read_deck", lambda path: _deck())
    monkeypatch.setattr(pl, "derive_master_spec", lambda deck, g: _spec())
    monkeypatch.setattr(pl, "_run_master_layer", lambda m, s: [])

    def apply_master(deck, deck_path, spec, config, master_profile=None):
        calls.append("master")
        return deck, None

    def first(deck, spec, metrics=None):
        calls.append("pass1")
        return [issue("space.overlap")]

    def second(deck, spec, metrics=None):
        calls.append("pass2")
        return [issue("size.role.inconsistent"), issue(ORPHANS)]

    seen: list[list[str]] = []

    def ai(deck, spec, rule_issues, config):
        calls.append("ai")
        seen.append([i.rule_id for i in rule_issues])
        return AIResult()

    monkeypatch.setattr(pl, "_apply_master_first", apply_master)
    monkeypatch.setattr(pl, "_run_rule_layer", first)
    monkeypatch.setattr(pl, "_run_second_pass", second)
    monkeypatch.setattr(pl, "_run_ai_layer", ai)

    report = pl.run(pl.RunConfig(
        master=Path("test_master1.pptx"),
        decks=[Path("messy.pptx")],
        apply_master=True,
        use_ai=True,
    ))

    assert calls == ["master", "pass1", "pass2", "ai"]
    # Both passes reach the model, orphans included.
    assert seen == [["space.overlap", "size.role.inconsistent", ORPHANS]]
    assert {i.rule_id for i in report.issues} == {
        "space.overlap", "size.role.inconsistent", ORPHANS,
    }


def test_the_second_pass_measures_the_file_on_disk(monkeypatch, tmp_path) -> None:
    """Pass 2 re-reads rather than reusing pass 1's reading. The restyle
    writes a new file, and this pass exists to describe the file as it stands
    at the end of the run."""
    from formatting_tool import pipeline as pl

    written = tmp_path / "restyled.pptx"
    read: list[str] = []

    def read_deck(path):
        read.append(str(path))
        return _deck(str(written))

    monkeypatch.setattr(pl, "read_deck", read_deck)
    monkeypatch.setattr(pl, "run_rules", lambda ctx, rules: [])

    pl._run_second_pass(_deck(str(written)), _spec())

    assert read == [str(written)]


def test_a_file_the_second_pass_cannot_reopen_does_not_lose_the_run(
    monkeypatch,
) -> None:
    """Pass 1 has already produced a report worth reading. Losing it to a
    locked file would be the wrong trade, so the pass falls back to the
    reading it has."""
    from formatting_tool import pipeline as pl

    def boom(path):
        raise OSError("the file is open in PowerPoint")

    monkeypatch.setattr(pl, "read_deck", boom)
    seen: list[DeckProfile] = []

    def run_rules(ctx, rules):
        seen.append(ctx.deck)
        return []

    monkeypatch.setattr(pl, "run_rules", run_rules)

    deck = _deck()
    assert pl._run_second_pass(deck, _spec()) == []
    assert seen == [deck]
