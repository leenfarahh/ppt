"""Smoke tests: the seams work without python-pptx, an API key, or a deck.

Every rule takes a DeckProfile, so a deck can be built in memory. That is the
point of the extract/rules split -- add a case here rather than a fixture file.
"""

from __future__ import annotations

import json

from formatting_tool.ai.payload import build_batches, build_reference_block
from formatting_tool.ai.schema import AI_RESPONSE_SCHEMA, issues_from_response
from formatting_tool.colorutil import delta_e, nearest_palette_entry
from formatting_tool.extract.master_spec import derive_master_spec
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
from formatting_tool.report.merge import merge_issues, summarize
from formatting_tool.rules import RuleContext, build_default_rules, run_rules


def _deck(*, font: str = "Comic Sans MS", color: str = "FF00FF") -> DeckProfile:
    """A one-slide deck with an off-brand title."""
    title = ShapeProfile(
        shape_id=2,
        name="Title 1",
        shape_type="PLACEHOLDER (14)",
        geometry=Geometry(left_in=0.6, top_in=0.5, width_in=11.0, height_in=1.2),
        placeholder_type="TITLE (13)",
        role=TextRole.TITLE,
        text="A title set in the wrong typeface",
        paragraphs=[
            ParagraphProfile(
                text="A title set in the wrong typeface",
                runs=[
                    RunProfile(
                        text="A title set in the wrong typeface",
                        font_name=font,
                        size_pt=36.0,
                        color_hex=color,
                    )
                ],
            )
        ],
    )
    return DeckProfile(
        path="messy.pptx",
        width_in=13.333,
        height_in=7.5,
        slides=[SlideProfile(number=1, layout_name="Title Slide", shapes=[title])],
        theme_fonts={"major": "Inter Tight", "minor": "Inter"},
    )


def _guidelines() -> BrandGuidelines:
    return BrandGuidelines(
        name="test",
        palette={"ink": "101820", "primary": "1F4FD8"},
        allowed_fonts=["Inter", "Inter Tight"],
    )


def _context() -> RuleContext:
    guidelines = _guidelines()
    master = _deck(font="Inter Tight", color="101820")
    spec = derive_master_spec(master, guidelines)
    return RuleContext(deck=_deck(), spec=spec)


def test_rules_run_and_find_the_planted_defects() -> None:
    ctx = _context()
    issues = run_rules(ctx, build_default_rules())
    found = {issue.rule_id for issue in issues}
    assert "font.family.unapproved" in found
    assert "color.text.off_palette" in found
    assert all(issue.deck == "messy.pptx" for issue in issues)


def test_every_rule_is_registered_once() -> None:
    ids = [rule.id for rule in build_default_rules()]
    assert len(ids) == len(set(ids))
    assert "unset" not in ids


def test_clean_deck_reports_nothing() -> None:
    guidelines = _guidelines()
    clean = _deck(font="Inter Tight", color="101820")
    spec = derive_master_spec(clean, guidelines)
    issues = run_rules(RuleContext(deck=clean, spec=spec), build_default_rules())
    brand_issues = [
        i for i in issues
        if i.rule_id in {"font.family.unapproved", "color.text.off_palette"}
    ]
    assert brand_issues == []


def test_reference_block_is_stable() -> None:
    """The cached prefix must be byte-identical across calls, or every cache
    hit is lost."""
    spec = _context().spec
    assert build_reference_block(spec) == build_reference_block(spec)
    assert json.loads(build_reference_block(spec))["brand"] == "test"


def test_batches_cover_every_slide() -> None:
    deck = _deck()
    deck.slides.extend(
        SlideProfile(number=n, layout_name="Content") for n in range(2, 26)
    )
    batches = list(build_batches(deck, [], batch_size=10))
    covered = [n for batch in batches for n in batch["batch"]["slides"]]
    assert covered == list(range(1, 26))
    assert batches[0]["batch"]["of"] == 3


def test_ai_response_maps_onto_issues() -> None:
    response = {
        "issues": [
            {
                "slide": 1,
                "shape": "Title 1",
                "category": "color",
                "severity": "error",
                "message": "Title colour is not in the palette.",
                "expected": "ink",
                "found": "#FF00FF",
                "suggestion": "Recolour to ink.",
                "confidence": 0.9,
                "confirms_refs": ["R2"],
            }
        ],
        "dismissed_refs": [{"ref": "R1", "reason": "deliberate accent"}],
        "summary": "One colour defect.",
    }
    issues, dismissals, summary = issues_from_response(response, "messy.pptx")
    assert len(issues) == 1
    assert issues[0].source.value == "ai"
    assert dismissals == {"R1": "deliberate accent"}
    assert summary == "One colour defect."


def test_merge_dismisses_and_dedupes() -> None:
    ctx = _context()
    rule_issues = run_rules(ctx, build_default_rules())
    target = rule_issues[0]

    merged = merge_issues(
        rule_issues=rule_issues,
        dismissals={"R1": "by design"},
        ref_lookup={"R1": target},
    )
    assert target not in merged
    assert len(merged) == len(rule_issues) - 1
    assert summarize(merged)["total"] == len(merged)


def test_schema_requires_every_property() -> None:
    """A json_schema output format has no optional keys."""
    item = AI_RESPONSE_SCHEMA["properties"]["issues"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False


def test_color_distance() -> None:
    assert delta_e("#101820", "#101820") == 0.0
    assert delta_e("101821", "101820") < 1.0        # imperceptible
    assert delta_e("#FF00FF", "#101820") > 20.0     # obviously different
    label, distance = nearest_palette_entry("1F4FD9", {"primary": "1F4FD8"})
    assert label == "primary" and distance < 1.0
