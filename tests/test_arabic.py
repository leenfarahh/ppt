"""Arabic decks: which script the copy is in, and which way it reads.

Prezlab's decks are bilingual and the two halves are not interchangeable. The
scaffolding for this was already in place -- `BrandGuidelines.arabic_fonts`,
`RunProfile.language` with a comment saying it drives the Arabic font rules --
and none of it was connected to anything. `arabic_fonts` was merged into the
Latin list before any rule saw it, so Arabic set in a Latin face passed; `rtl`
was never read off the file at all; and the alignment grid measured left edges
only, so an Arabic deck, which aligns on its right edges, came back clean
because the rule could not see its columns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool.models import BrandGuidelines
from formatting_tool.script import has_arabic, is_arabic, is_rtl

# Kept as escapes so the file is readable in a terminal that cannot draw
# Arabic, and so a console codepage cannot corrupt the fixtures.
AR_HEADING = "التحول الصحي"
AR_WORD = "نيوم"
EN = "Health Systems Operations"


# --------------------------------------------------------------------------- #
# Telling the scripts apart
# --------------------------------------------------------------------------- #

def test_arabic_copy_reads_as_arabic() -> None:
    assert is_arabic(AR_HEADING)
    assert is_rtl(AR_HEADING)


def test_english_copy_does_not() -> None:
    assert not is_arabic(EN)
    assert not is_rtl(EN)


def test_a_tie_is_not_called_arabic() -> None:
    """"NEOM NYWM" is four letters each way. The direction of a tie is a coin
    flip, which is not a thing to set a paragraph's direction on."""
    assert not is_arabic(f"NEOM {AR_WORD}")


def test_one_arabic_word_still_needs_an_arabic_face() -> None:
    """Coverage is not a majority question. A Latin face has no Arabic glyphs,
    so ONE Arabic word in an English run renders as boxes however much Latin
    sits beside it."""
    assert has_arabic(f"NEOM {AR_WORD}")
    assert not has_arabic(EN)


def test_digits_and_punctuation_do_not_decide_the_script() -> None:
    """They are shared, and counting them makes a mostly-Arabic line with a
    long number in it read as mixed."""
    assert is_arabic(f"{AR_HEADING} 2024 (12%)")


# --------------------------------------------------------------------------- #
# A deck built the way an Arabic deck is built
# --------------------------------------------------------------------------- #

def _deck(tmp_path: Path, *, font: str = "Aptos", rtl: bool = False) -> Path:
    """A right-aligned Arabic column, one member 0.09in off the right edge."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for index in range(5):
        width = 3.0 + (index % 3) * 0.8
        right = 12.40 - (0.09 if index == 2 else 0.0)
        box = slide.shapes.add_textbox(
            Inches(right - width), Inches(1.0 + index * 0.9),
            Inches(width), Inches(0.6),
        )
        box.name = f"Arabic {index}"
        frame = box.text_frame
        frame.text = AR_HEADING
        for paragraph in frame.paragraphs:
            paragraph.alignment = PP_ALIGN.RIGHT
            if rtl:
                paragraph._p.get_or_add_pPr().set("rtl", "1")
            for run in paragraph.runs:
                run.font.size = Pt(18)
                run.font.name = font
    path = tmp_path / "arabic.pptx"
    prs.save(str(path))
    return path


def _guidelines() -> BrandGuidelines:
    guidelines = BrandGuidelines()
    guidelines.allowed_fonts = ["Aptos", "Aptos Display"]
    guidelines.arabic_fonts = ["Cairo"]
    return guidelines


def _findings(deck: Path, rule_id: str, guidelines=None):
    from formatting_tool.extract import read_deck
    from formatting_tool.extract.master_spec import derive_master_spec
    from formatting_tool.linemetrics import NullLineMetrics
    from formatting_tool.rules import RuleContext, build_default_rules, run_rules

    spec = derive_master_spec(
        read_deck("test_master1.pptx"), guidelines or _guidelines()
    )
    context = RuleContext(deck=read_deck(deck), spec=spec)
    found = [
        i for i in run_rules(context, build_default_rules(metrics=NullLineMetrics()))
        if i.rule_id == rule_id
    ]
    for issue in found:
        issue.id = issue.fingerprint()
    return found


def test_a_deck_of_arabic_reads_right_to_left(tmp_path: Path) -> None:
    from formatting_tool.extract import read_deck

    assert read_deck(_deck(tmp_path)).rtl


def test_arabic_in_a_latin_face_is_reported(tmp_path: Path) -> None:
    """The defect a merged font list cannot see: Aptos is on the brand's list,
    and it has no Arabic in it."""
    found = _findings(_deck(tmp_path, font="Aptos"), "font.family.arabic")

    assert len(found) == 5
    assert "Aptos" in found[0].found


def test_arabic_in_the_arabic_face_is_not(tmp_path: Path) -> None:
    found = _findings(_deck(tmp_path, font="Cairo"), "font.family.arabic")

    assert found == []


def test_nothing_is_said_without_a_declared_arabic_list(tmp_path: Path) -> None:
    """Guessing which of the brand's faces covers Arabic from its name would
    be a guess, and reporting every Arabic run in a deck whose guidelines
    never mentioned Arabic would be noise."""
    bare = BrandGuidelines()
    bare.allowed_fonts = ["Aptos"]

    assert _findings(_deck(tmp_path), "font.family.arabic", bare) == []


def test_unmarked_arabic_paragraphs_are_reported(tmp_path: Path) -> None:
    """The letters still shape and join -- that is the font's job -- so the
    slide looks almost right. What lands in the wrong place is the punctuation
    and the numbers."""
    found = _findings(_deck(tmp_path, rtl=False), "typography.rtl_not_set")

    assert len(found) == 5


def test_marked_arabic_paragraphs_are_not(tmp_path: Path) -> None:
    assert _findings(_deck(tmp_path, rtl=True), "typography.rtl_not_set") == []


# --------------------------------------------------------------------------- #
# The fixes
# --------------------------------------------------------------------------- #

def _apply(deck: Path, findings, out: Path):
    from formatting_tool.apply import apply_fixes
    from formatting_tool.extract import read_deck
    from formatting_tool.extract.master_spec import derive_master_spec

    spec = derive_master_spec(read_deck("test_master1.pptx"), _guidelines())
    return apply_fixes(
        deck, findings, out, selected=[i.id for i in findings], spec=spec
    )


def test_the_arabic_runs_are_set_in_the_arabic_face(tmp_path: Path) -> None:
    from formatting_tool.extract import read_deck

    deck = _deck(tmp_path, font="Aptos")
    out = tmp_path / "fixed.pptx"

    result = _apply(deck, _findings(deck, "font.family.arabic"), out)

    assert len(result.applied) == 5
    fonts = {
        run.font_name
        for slide in read_deck(out).slides
        for shape in slide.shapes
        for paragraph in shape.paragraphs
        for run in paragraph.runs
    }
    assert fonts == {"Cairo"}


def test_the_paragraphs_are_marked_right_to_left(tmp_path: Path) -> None:
    from formatting_tool.extract import read_deck

    deck = _deck(tmp_path, rtl=False)
    out = tmp_path / "fixed.pptx"

    result = _apply(deck, _findings(deck, "typography.rtl_not_set"), out)

    assert len(result.applied) == 5
    flags = {
        paragraph.rtl
        for slide in read_deck(out).slides
        for shape in slide.shapes
        for paragraph in shape.paragraphs
    }
    assert flags == {True}


def test_english_paragraphs_are_left_alone(tmp_path: Path) -> None:
    """A bilingual shape with an English heading over an Arabic body has one
    of each, and turning the heading round is the same defect pointed the
    other way."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    from formatting_tool.apply.fixers import fix_rtl_not_set

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(2))
    box.text_frame.text = EN
    box.text_frame.add_paragraph().text = AR_HEADING
    deck = tmp_path / "mixed.pptx"
    prs.save(str(deck))

    shape = Presentation(str(deck)).slides[0].shapes[0]
    detail = fix_rtl_not_set(shape, _rtl_issue(), None)

    assert "1 Arabic paragraph" in detail
    marked = [
        p._p.find(
            "{http://schemas.openxmlformats.org/drawingml/2006/main}pPr"
        )
        for p in shape.text_frame.paragraphs
    ]
    assert marked[0] is None or marked[0].get("rtl") is None
    assert marked[1] is not None and marked[1].get("rtl") == "1"


def _rtl_issue():
    from formatting_tool.models import Category, Issue, Severity, Source

    return Issue(
        category=Category.TYPOGRAPHY,
        severity=Severity.ERROR,
        message="unmarked",
        source=Source.RULE,
        rule_id="typography.rtl_not_set",
        slide=1,
        shape="TextBox 1",
    )


# --------------------------------------------------------------------------- #
# Alignment, which is the half that was silently passing
# --------------------------------------------------------------------------- #

def test_the_grid_measures_the_edge_the_deck_aligns_on(tmp_path: Path) -> None:
    """A right-to-left deck sets its copy flush right, so a column of Arabic
    shapes of different widths shares a right edge and nothing else. Read on
    left edges it is not a column at all and every shape in it looks correctly
    placed -- which reads as a clean bill of health."""
    found = _findings(_deck(tmp_path), "space.alignment_grid")

    assert len(found) == 1
    assert found[0].shape == "Arabic 2"
    assert found[0].expected.startswith("right ")


def test_the_snap_moves_the_right_edge(tmp_path: Path) -> None:
    """Snapping the LEFT edge of a right-aligned shape moves it by its own
    width off the column it belongs to."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    from formatting_tool.apply import apply_fixes
    from formatting_tool.models import Category, Issue, Severity, Source
    from formatting_tool.rules.space import DECLARED_GRID

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(9.0), Inches(1.0), Inches(3.31), Inches(0.6))
    box.name = "Arabic 2"
    box.text_frame.text = AR_HEADING
    deck = tmp_path / "snap.pptx"
    prs.save(str(deck))

    issue = Issue(
        category=Category.SPACE, severity=Severity.WARNING,
        message=f"Right edge is 0.09in off the 12.40in grid line {DECLARED_GRID}.",
        source=Source.RULE, rule_id="space.alignment_grid", slide=1,
        shape="Arabic 2", shape_id=box.shape_id, deck="snap.pptx",
        expected="right 12.40in", found="12.31in",
    )
    issue.id = issue.fingerprint()
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    fixed = Presentation(str(out)).slides[0].shapes[0]
    assert round((fixed.left + fixed.width) / 914400, 2) == 12.40
    assert round(fixed.width / 914400, 2) == 3.31       # resized nothing
