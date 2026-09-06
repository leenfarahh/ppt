"""Tests for what the report says it left out, and for whose theme is authority.

Two themes run through these. First, a report has to account for the gap
between what the rules found and what it prints: a finding the AI dismissed
and a check that never ran are both invisible in an issue list, and both
change what the list means. Second, a messy deck arrives carrying the theme
and layouts of the file it was built from, and none of that is a standard to
measure it against.
"""

from __future__ import annotations

import io

from formatting_tool.ai.payload import build_batches, ref_for
from formatting_tool.extract.master_spec import derive_master_spec
from formatting_tool.models import (
    BrandGuidelines,
    Category,
    DeckProfile,
    Dismissal,
    Geometry,
    Issue,
    ParagraphProfile,
    RunProfile,
    ShapeProfile,
    SkippedRule,
    SlideProfile,
    Severity,
    Source,
    ValidationReport,
)
from formatting_tool.report.merge import merge_issues
from formatting_tool.report.writers import write_markdown, write_text
from formatting_tool.rules import RuleContext, build_default_rules, skipped_rules
from formatting_tool.rules.fonts import ThemeFontDriftRule


def _rule_issue(rule_id: str, slide: int, found: str = "x") -> Issue:
    return Issue(
        category=Category.SPACE,
        severity=Severity.ERROR,
        message=f"{rule_id} on slide {slide}.",
        source=Source.RULE,
        rule_id=rule_id,
        slide=slide,
        deck="d.pptx",
        found=found,
    )


# --------------------------------------------------------------------------- #
# Dismissals are recorded, not deleted
# --------------------------------------------------------------------------- #

def test_a_dismissal_is_recorded_with_its_reason() -> None:
    """Dropping a proved finding is a claim, and has to be reviewable.

    Without this the report is indistinguishable from one where the rule never
    fired, and the reader has no way to disagree with the AI.
    """
    rules = [_rule_issue("space.overlap", 1), _rule_issue("space.overlap", 2)]
    lookup = {ref_for(i): issue for i, issue in enumerate(rules)}
    record: list[Dismissal] = []

    kept = merge_issues(
        rules, [], dismissals={"R1": "overlap is by design"},
        ref_lookup=lookup, record=record,
    )

    assert len(kept) == 1
    assert len(record) == 1
    assert record[0].rule_id == "space.overlap"
    assert record[0].reason == "overlap is by design"
    assert record[0].slide == 1


def test_dismissals_reach_the_written_report() -> None:
    report = ValidationReport(
        master="m.pptx", decks=["d.pptx"], generated_at="now",
        issues=[_rule_issue("space.overlap", 3)],
        dismissals=[Dismissal(rule_id="space.overlap", reason="by design",
                              deck="d.pptx", slide=1, message="overlap")],
    )
    for writer in (write_text, write_markdown):
        buf = io.StringIO()
        writer(report, buf)
        out = buf.getvalue()
        assert "space.overlap" in out
        assert "by design" in out


def test_omissions_are_printed_even_when_nothing_was_found() -> None:
    """"No inconsistencies found" over a set of disabled checks is the most
    misleading line the tool can produce, so this is the case that matters."""
    report = ValidationReport(
        master="m.pptx", decks=["d.pptx"], generated_at="now", issues=[],
        skipped_rules=[SkippedRule("color.text.off_palette", "no guidelines",
                                   "extract-guidelines --from MASTER.pptx")],
    )
    for writer in (write_text, write_markdown):
        buf = io.StringIO()
        writer(report, buf)
        out = buf.getvalue()
        assert "No inconsistencies found" in out
        assert "color.text.off_palette" in out


# --------------------------------------------------------------------------- #
# Dedupe by ref
# --------------------------------------------------------------------------- #

def test_an_ai_restatement_collapses_into_the_finding_it_names() -> None:
    """The AI says which finding it is restating; that decides it.

    Matching on the text of `found` cannot work: the two layers describe the
    same defect in different words, which is the whole reason the AI layer
    exists.
    """
    rule = _rule_issue("logo.missing", 1, found="none detected")
    restatement = Issue(
        category=Category.SPACE, severity=Severity.BLOCKER, source=Source.AI,
        message="The cover slide is missing the brand logo.",
        confirms="R1", slide=1, deck="d.pptx", found="No logo detected",
        suggestion="Insert the brand logo.", confidence=0.9,
    )

    kept = merge_issues([rule], [restatement],
                        ref_lookup={ref_for(0): rule})

    assert len(kept) == 1
    assert kept[0].source is Source.RULE
    assert kept[0].suggestion == "Insert the brand logo."
    assert kept[0].confidence == 0.9


def test_an_ai_finding_with_no_ref_is_kept() -> None:
    """A genuinely new observation is the AI layer's whole point."""
    rule = _rule_issue("logo.missing", 1)
    fresh = Issue(
        category=Category.SPACE, severity=Severity.WARNING, source=Source.AI,
        message="Column spacing is uneven.", slide=9, deck="d.pptx",
        confidence=0.95,
    )

    kept = merge_issues([rule], [fresh], ref_lookup={ref_for(0): rule})

    assert len(kept) == 2


def test_a_ref_pointing_at_a_dismissed_finding_does_not_resurrect_it() -> None:
    """Dismissed and confirmed at once is contradictory; dismissal wins.

    Folding the restatement into a finding that is no longer in the list would
    put it back in the report through the side door.
    """
    rule = _rule_issue("space.overlap", 1)
    restatement = Issue(
        category=Category.SPACE, severity=Severity.ERROR, source=Source.AI,
        message="Boxes overlap.", confirms="R1", slide=1, deck="d.pptx",
        found="different wording",
    )

    kept = merge_issues([rule], [restatement],
                        dismissals={"R1": "by design"},
                        ref_lookup={ref_for(0): rule})

    assert [i.source for i in kept] == [Source.AI]
    assert all(i.rule_id != "space.overlap" for i in kept)


# --------------------------------------------------------------------------- #
# Skipped rules
# --------------------------------------------------------------------------- #

def test_skipped_rules_name_the_checks_that_cannot_run() -> None:
    spec = derive_master_spec(
        DeckProfile(path="m.pptx", width_in=13.333, height_in=7.5),
        BrandGuidelines(name="none-supplied"),
    )
    ctx = RuleContext(
        deck=DeckProfile(path="d.pptx", width_in=13.333, height_in=7.5), spec=spec
    )

    skipped = skipped_rules(ctx, build_default_rules())
    ids = {s.rule_id for s in skipped}

    assert "color.text.off_palette" in ids
    assert "font.family.unapproved" in ids
    assert all(s.unlocked_by for s in skipped)
    # A rule that needs no brand file is not listed as skipped.
    assert "space.overlap" not in ids


# --------------------------------------------------------------------------- #
# The master's theme is the only authority
# --------------------------------------------------------------------------- #

def _deck_with_font(font: str) -> DeckProfile:
    shape = ShapeProfile(
        shape_id=1, name="Body 1", shape_type="TEXT_BOX (17)",
        geometry=Geometry(1, 1, 4, 1), text="copy",
        paragraphs=[ParagraphProfile(
            text="copy", runs=[RunProfile(text="copy", font_name=font)]
        )],
    )
    return DeckProfile(
        path="messy.pptx", width_in=13.333, height_in=7.5,
        slides=[SlideProfile(number=1, shapes=[shape])],
        # The foreign theme the deck arrived carrying.
        theme_fonts={"major": "Georgia", "minor": "Comic Sans MS"},
    )


def _spec_with_theme() -> object:
    master = DeckProfile(
        path="master.pptx", width_in=13.333, height_in=7.5,
        theme_fonts={"major": "Inter Tight", "minor": "Inter"},
    )
    return derive_master_spec(master, BrandGuidelines(name="none-supplied"))


def test_theme_drift_is_measured_against_the_master_theme() -> None:
    """A run hardcoding the master's typeface is drift the rule should see."""
    ctx = RuleContext(deck=_deck_with_font("Inter"), spec=_spec_with_theme())

    issues = list(ThemeFontDriftRule().check(ctx))

    assert len(issues) == 1
    assert "Inter" in issues[0].found


def test_the_decks_own_theme_is_not_treated_as_a_standard() -> None:
    """The old behaviour read the deck's own theme, so a run set in the foreign
    brand's typeface counted as correctly inheriting. It is not drift from the
    master's theme, and the unapproved-font rule owns it instead."""
    ctx = RuleContext(deck=_deck_with_font("Comic Sans MS"), spec=_spec_with_theme())

    assert list(ThemeFontDriftRule().check(ctx)) == []


def test_the_batch_payload_sends_the_master_theme() -> None:
    """The model was being handed the foreign theme under a neutral name."""
    deck = _deck_with_font("Inter")
    batch = next(build_batches(deck, [], spec=_spec_with_theme()))

    assert batch["master_theme_fonts"] == {"major": "Inter Tight", "minor": "Inter"}
    assert "theme_fonts" not in batch
    # The deck's own theme still travels, named for what it is.
    assert batch["deck_own_theme"]["fonts"]["major"] == "Georgia"


def test_a_deck_on_the_master_theme_carries_no_foreign_theme_block() -> None:
    master = DeckProfile(
        path="master.pptx", width_in=13.333, height_in=7.5,
        theme_fonts={"major": "Inter Tight", "minor": "Inter"},
    )
    spec = derive_master_spec(master, BrandGuidelines(name="none-supplied"))
    deck = DeckProfile(
        path="d.pptx", width_in=13.333, height_in=7.5,
        slides=[SlideProfile(number=1)],
        theme_fonts={"major": "Inter Tight", "minor": "Inter"},
    )

    batch = next(build_batches(deck, [], spec=spec))

    assert "deck_own_theme" not in batch
