"""Keeping distinct colours distinct when several are snapped at once.

The failure these exist for came off a real slide: a table of pills coloured
for High, Medium and Low, checked against a master whose palette holds one
warm mid-tone. Each pill's nearest entry was that tone, each fix was correct
on its own, and the corrected deck had a three-step legend drawn in one
colour.

Nothing here detects a legend. What it enforces is the one property every
encoding shares -- distinct colours stay distinct -- which preserves a legend,
a heatmap, a RAG column and a chart's series without knowing which it is
looking at.

The second half is what a displaced colour becomes. Leaving it alone kept the
set apart and left an off-palette colour in the deck, which is the one thing
the tool exists to remove. So the colour is matched on its RELATIONSHIP
instead: the contrast it had with the colour that displaced it, reproduced
against that colour's new value, with distance deciding between entries that
score alike.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from formatting_tool.apply import apply_fixes
from formatting_tool.apply.colorplan import build_color_plan
from formatting_tool.colorutil import contrast_ratio, delta_e, relative_luminance
from formatting_tool.models import Category, Issue, Severity, Source

# The three pill fills, near the ones off the slide this was found on.
HIGH, MEDIUM, LOW = "A8C8A0", "C8B48C", "C4C4C4"
# A palette with a single warm mid-tone: the trap, because all three pills
# measure closest to it.
ONE_WARM = {"tan": "C1A87C", "navy": "1F2A44", "white": "FFFFFF", "black": "000000"}
TOL = 3.0
LIMIT = 12.0


def _plan(selected, everything=None, palette=None):
    return build_color_plan(
        selected=selected,
        everything=everything if everything is not None else selected,
        palette=palette if palette is not None else ONE_WARM,
        tolerance=TOL,
        limit=LIMIT,
    )


def _issue(rule_id: str, **kwargs) -> Issue:
    issue = Issue(
        category=kwargs.pop("category", Category.COLOR),
        severity=kwargs.pop("severity", Severity.ERROR),
        message=kwargs.pop("message", f"{rule_id} finding"),
        source=Source.RULE,
        rule_id=rule_id,
        deck="messy.pptx",
        **kwargs,
    )
    issue.id = issue.fingerprint()
    return issue


# --------------------------------------------------------------------------- #
# The headline
# --------------------------------------------------------------------------- #

def test_three_colours_do_not_become_one() -> None:
    """All three pills are nearest the same tan. At most one may have it."""
    plan = _plan([HIGH, MEDIUM, LOW])

    finals = {
        name: (plan.choice_for(src).target or src)
        for name, src in (("high", HIGH), ("medium", MEDIUM), ("low", LOW))
    }
    pairs = [("high", "medium"), ("high", "low"), ("medium", "low")]
    for a, b in pairs:
        assert delta_e(finals[a], finals[b]) > TOL, (
            f"{a} and {b} both ended up {finals[a]}"
        )


def test_the_closest_match_is_the_one_that_keeps_the_entry() -> None:
    """Medium is 3.6 from the tan and the others are 13 and 19 away. The
    colour that most clearly IS the entry keeps it; the ones merely near it
    look elsewhere or stand down."""
    plan = _plan([HIGH, MEDIUM, LOW])

    assert plan.choice_for(MEDIUM).label == "tan"
    assert plan.choice_for(HIGH).target != ONE_WARM["tan"]


def test_every_colour_ends_up_on_the_palette() -> None:
    """The point of the exercise. A colour whose entry was taken does not stay
    off-palette: it takes another entry, chosen by contrast."""
    plan = _plan([HIGH, MEDIUM, LOW])
    on_palette = {v.upper() for v in ONE_WARM.values()}

    for src in (HIGH, MEDIUM, LOW):
        choice = plan.choice_for(src)
        assert choice.applies, f"#{src} was left off-palette"
        assert choice.target in on_palette
    assert plan.declined == []


def test_a_displaced_colour_keeps_its_contrast_to_the_one_that_displaced_it(
) -> None:
    """The rule this was built for. High is nearest the tan, Medium is nearer
    still and takes it, and High then picks the free entry whose contrast
    against the tan comes closest to the contrast High and Medium had between
    them to begin with."""
    plan = _plan([HIGH, MEDIUM])
    high, medium = plan.choice_for(HIGH), plan.choice_for(MEDIUM)

    assert medium.label == "tan"              # the closer match keeps it
    assert high.applies and high.label != "tan"

    want = contrast_ratio(HIGH, MEDIUM)       # the original relationship
    got = contrast_ratio(high.target, medium.target)
    # Nothing in a four-entry palette can hold a 1.1:1 relationship, so what
    # is asserted is that it was CHOSEN for it: no free entry does better.
    best = min(
        abs(contrast_ratio(entry, medium.target) - want)
        for label, entry in ONE_WARM.items()
        if label != "tan"
    )
    assert abs(got - want) == pytest.approx(best, abs=0.01)
    # And the reason shows the designer both numbers, since a relationship
    # that could not be held is a thing they have to be able to see.
    assert "contrast" in high.reason
    assert f"{want:.1f}:1" in high.reason


# --------------------------------------------------------------------------- #
# What is NOT being fixed still constrains what is
# --------------------------------------------------------------------------- #

def test_a_colour_nobody_ticked_still_reserves_its_own_colour() -> None:
    """The case the selection alone would miss. Only Medium is ticked, and it
    happens to sit close to a palette entry; the untouched High pill beside it
    must not end up the same colour as the fixed one."""
    palette = {"almost-high": "A9C9A1", "navy": "1F2A44"}

    plan = build_color_plan(
        selected=[MEDIUM], everything=[MEDIUM, HIGH],
        palette=palette, tolerance=TOL, limit=60.0,
    )
    choice = plan.choice_for(MEDIUM)

    # `almost-high` is 0.6 from the untouched High pill, so taking it would
    # make the two pills the same colour. Whether that ends in another entry
    # or in nothing at all is the plan's business; what must hold either way
    # is that the two pills still read as two.
    final = choice.target or MEDIUM
    assert delta_e(final, HIGH) > TOL
    assert final != palette["almost-high"]


def test_two_near_identical_palette_entries_are_not_both_used() -> None:
    """Distinct labels are not enough. A palette holding two tints a hair
    apart would satisfy a one-entry-each rule and still collapse the set, so
    the constraint is on the colours, not the names."""
    palette = {"tan-a": "C1A87C", "tan-b": "C2A97D", "navy": "1F2A44"}

    plan = build_color_plan(
        selected=[MEDIUM, "C6AE82"], everything=[MEDIUM, "C6AE82"],
        palette=palette, tolerance=TOL, limit=LIMIT,
    )
    finals = [
        plan.choice_for(src).target or src for src in (MEDIUM, "C6AE82")
    ]

    assert delta_e(finals[0], finals[1]) > TOL


# --------------------------------------------------------------------------- #
# Ordinary cases still work
# --------------------------------------------------------------------------- #

def test_one_colour_on_its_own_just_snaps() -> None:
    plan = _plan([MEDIUM])

    assert plan.choice_for(MEDIUM).target == "C1A87C"
    assert plan.choice_for(MEDIUM).fallback is False


def test_a_colour_with_no_defensible_entry_still_falls_back() -> None:
    """The fallback is kept: nearest, marked as such."""
    plan = _plan([LOW])
    choice = plan.choice_for(LOW)

    assert choice.applies
    assert choice.fallback is True
    assert "nearest" in choice.reason


def test_no_palette_means_no_plan() -> None:
    assert len(_plan([MEDIUM], palette={})) == 0
    assert _plan([MEDIUM], palette={}).choice_for(MEDIUM) is None


def test_the_plan_is_deterministic() -> None:
    """Two runs over one deck must produce one answer, or a designer's second
    look at the same file disagrees with their first."""
    first = _plan([HIGH, MEDIUM, LOW])
    second = _plan([LOW, HIGH, MEDIUM])       # a different order in

    for src in (HIGH, MEDIUM, LOW):
        assert first.choice_for(src).target == second.choice_for(src).target


def test_a_colour_is_allowed_to_land_on_the_entry_it_almost_is() -> None:
    """The separation rule must not fire on the colour being placed itself,
    or nothing would ever be snapped: every source is within tolerance of the
    entry it is nearly identical to."""
    plan = _plan(["C1A87D"])          # a hair off the tan

    assert plan.choice_for("C1A87D").target == "C1A87C"


# --------------------------------------------------------------------------- #
# Contrast, in the other sense: text on top of a recoloured fill
# --------------------------------------------------------------------------- #

def test_the_wcag_formula_matches_its_reference_values() -> None:
    assert contrast_ratio("FFFFFF", "000000") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("777777", "777777") == pytest.approx(1.0, abs=0.01)
    # The canonical AA boundary: #767676 is the darkest grey that passes 4.5
    # on white.
    assert contrast_ratio("767676", "FFFFFF") == pytest.approx(4.54, abs=0.01)
    assert relative_luminance("000000") == 0.0
    assert relative_luminance("FFFFFF") == pytest.approx(1.0)


def test_the_palette_wins_when_no_entry_keeps_the_text_readable(
    tmp_path: Path,
) -> None:
    """The over-constrained case, and a deliberate trade-off rather than a bug.

    A pale pill with dark text and a palette holding one dark entry: there is
    no colour that is both on the palette and readable under that text. Being
    on the palette is the requirement, so the colour is applied and the label
    is reported as needing a person. Leaving it off-palette -- which is what
    this did first -- fails the one thing the brand check is for.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    pale, ink = "D8DCE0", "1A1A1A"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pill = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1),
                                  Inches(1), Inches(2), Inches(0.5))
    pill.name = "Pill"
    pill.fill.solid()
    pill.fill.fore_color.rgb = RGBColor.from_string(pale)
    pill.text_frame.text = "Balanced"
    pill.text_frame.paragraphs[0].runs[0].font.color.rgb = \
        RGBColor.from_string(ink)
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    spec = SimpleNamespace(palette={"navy": "1F2A44"})
    issue = _issue(
        "color.shape.off_palette", slide=1, shape="Pill",
        shape_id=pill.shape_id,
        message=f"Shape fill #{pale} is off-palette.",
        found=f"#{pale}", expected="nearest navy #1F2A44",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, spec=spec)

    assert len(result.applied) == 1
    detail = result.applied[0].detail
    assert "readable" in detail and "by hand" in detail
    fixed = Presentation(str(out)).slides[0].shapes[0]
    assert str(fixed.fill.fore_color.rgb) == "1F2A44"
    # The trade-off is real, and the test says so out loud: the label really
    # is now under the floor, and the outcome line is what carries that.
    assert contrast_ratio("1F2A44", ink) < 3.0


def test_without_a_plan_the_fixer_still_refuses_an_illegible_fill(
    tmp_path: Path,
) -> None:
    """The no-palette path, which the CLI and any hand-built finding take.

    With no plan there is no whole-palette search behind the target, so a
    colour that buries the text is not a considered trade-off -- it is just
    the nearest entry, and the veto is the only thing standing between it and
    an unreadable pill.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    pale, ink = "D8DCE0", "1A1A1A"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pill = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1),
                                  Inches(1), Inches(2), Inches(0.5))
    pill.name = "Pill"
    pill.fill.solid()
    pill.fill.fore_color.rgb = RGBColor.from_string(pale)
    pill.text_frame.text = "Balanced"
    pill.text_frame.paragraphs[0].runs[0].font.color.rgb =         RGBColor.from_string(ink)
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "color.shape.off_palette", slide=1, shape="Pill",
        shape_id=pill.shape_id,
        message=f"Shape fill #{pale} is off-palette.",
        found=f"#{pale}", expected="nearest navy #1F2A44",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out)      # no spec, so no plan

    assert result.applied == []
    assert "contrast" in result.skipped[0].detail
    kept = Presentation(str(out)).slides[0].shapes[0]
    assert str(kept.fill.fore_color.rgb) == pale


def test_a_legible_entry_is_chosen_over_an_illegible_nearer_one(
    tmp_path: Path,
) -> None:
    """The reason legibility is a filter and not a veto. The navy is nearest
    and would bury the label; a paler entry is further away and readable. The
    plan takes the paler one, so the colour still ends up on the palette --
    which vetoing after the choice could never do."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    pale, ink = "D8DCE0", "1A1A1A"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pill = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1),
                                  Inches(1), Inches(2), Inches(0.5))
    pill.name = "Pill"
    pill.fill.solid()
    pill.fill.fore_color.rgb = RGBColor.from_string(pale)
    pill.text_frame.text = "Balanced"
    pill.text_frame.paragraphs[0].runs[0].font.color.rgb =         RGBColor.from_string(ink)
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    spec = SimpleNamespace(palette={"navy": "1F2A44", "stone": "D8D4CC"})
    issue = _issue(
        "color.shape.off_palette", slide=1, shape="Pill",
        shape_id=pill.shape_id,
        message=f"Shape fill #{pale} is off-palette.",
        found=f"#{pale}", expected="nearest navy #1F2A44",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, spec=spec)

    assert len(result.applied) == 1
    fixed = Presentation(str(out)).slides[0].shapes[0]
    assert str(fixed.fill.fore_color.rgb) == "D8D4CC"
    assert contrast_ratio("D8D4CC", ink) >= 3.0


def test_text_that_was_already_unreadable_does_not_veto_the_recolour(
    tmp_path: Path,
) -> None:
    """Only what this fix would BREAK. A pill whose label was already
    invisible has a finding of its own, and holding the colour hostage to it
    would leave the deck wrong twice over."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    dark, nearly_dark = "202020", "262626"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pill = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1),
                                  Inches(1), Inches(2), Inches(0.5))
    pill.name = "Pill"
    pill.fill.solid()
    pill.fill.fore_color.rgb = RGBColor.from_string(dark)
    pill.text_frame.text = "invisible already"
    pill.text_frame.paragraphs[0].runs[0].font.color.rgb = \
        RGBColor.from_string(nearly_dark)
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    spec = SimpleNamespace(palette={"black": "000000"})
    issue = _issue(
        "color.shape.off_palette", slide=1, shape="Pill",
        shape_id=pill.shape_id,
        message=f"Shape fill #{dark} is off-palette.",
        found=f"#{dark}", expected="black #000000",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, spec=spec)

    assert len(result.applied) == 1
    fixed = Presentation(str(out)).slides[0].shapes[0]
    assert str(fixed.fill.fore_color.rgb) == "000000"


def test_an_outline_is_not_held_to_the_text_contrast(tmp_path: Path) -> None:
    """An outline is not behind the text, so moving it cannot make the text
    unreadable."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    pale, ink = "D8DCE0", "1A1A1A"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    pill = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1),
                                  Inches(1), Inches(2), Inches(0.5))
    pill.name = "Pill"
    pill.fill.solid()
    pill.fill.fore_color.rgb = RGBColor.from_string("FFFFFF")
    pill.line.color.rgb = RGBColor.from_string(pale)
    pill.text_frame.text = "Balanced"
    pill.text_frame.paragraphs[0].runs[0].font.color.rgb = \
        RGBColor.from_string(ink)
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    spec = SimpleNamespace(palette={"navy": "1F2A44"})
    issue = _issue(
        "color.shape.off_palette", slide=1, shape="Pill",
        shape_id=pill.shape_id,
        message=f"Shape outline #{pale} is off-palette.",
        found=f"#{pale}", expected="nearest navy #1F2A44",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, spec=spec)

    assert len(result.applied) == 1
    assert "outline" in result.applied[0].detail
