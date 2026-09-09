"""A row of parallel headings, squared up to the same number of lines.

Off a real slide: four column headings, two wrapping to a second line and two
sitting on one -- "MBA SURVEY*" and "MBA CASEBOOK" short beside "TAILORED
TRAININGS*" and "AMBASSADORS SITE ON OURSPACE*". Every heading correct on its
own and the row reading ragged, because what the eye compares across columns
is not the words but the block each one makes.

Equalised UP, to the longest in the row: a break can be added to a short
heading, and taking one out of a long one needs a wider box or smaller type,
which are the designer's to give.

The fix is a soft return, and that is a deliberate choice with a cost. The
alternatives both move geometry -- narrowing one box of a row leaves it a
different width from its three neighbours, which the repeat rules would then
report, and widening is the same trade the other way. A break changes nothing
about the composition, and it is the only one of the three that chooses WHERE
the line falls: "MBA SURVEY*" reads as MBA / SURVEY* and never MBA SURVEY / *.

The cost is that `typography.manual_line_break` exists to warn about exactly
this character, so that rule now exempts a row whose headings all take the
same number of lines -- the break there is doing the job the rule is
otherwise warning about the absence of.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool.extract import read_deck
from formatting_tool.extract.master_spec import derive_master_spec
from formatting_tool.linemetrics import ShapeKey
from formatting_tool.models import BrandGuidelines
from formatting_tool.rules import RuleContext
from formatting_tool.rules.typography import (
    HeadingBalanceRule,
    ManualLineBreakRule,
)

HEADS = ["MBA SURVEY*", "TAILORED TRAININGS*", "MBA CASEBOOK",
         "AMBASSADORS SITE ON OURSPACE*"]


class StubMetrics:
    """Line counts as a renderer would report them, by shape id."""

    def __init__(self, lines: dict[int, list[str]]) -> None:
        self._lines = lines

    @property
    def available(self) -> bool:
        return True

    def lines(self, key: ShapeKey):
        return self._lines.get(key.shape_id)

    def bounds(self, key: ShapeKey):
        return None


def _row(tmp_path: Path, heads=None, *, same_row: bool = True):
    """A row of same-sized heading boxes, as the slide has them."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    ids = []
    for col, head in enumerate(heads or HEADS):
        top = 1.55 if same_row else 1.55 + col * 0.9
        box = slide.shapes.add_textbox(Inches(0.75 + col * 3.15), Inches(top),
                                       Inches(2.35), Inches(0.45))
        box.name = f"Heading {col + 1}"
        box.text_frame.word_wrap = True
        box.text_frame.text = head
        box.text_frame.paragraphs[0].runs[0].font.size = Pt(13)
        ids.append(box.shape_id)
    path = tmp_path / "headings.pptx"
    prs.save(str(path))
    return path, ids


def _ctx(deck: Path):
    spec = derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())
    return RuleContext(deck=read_deck(deck), spec=spec)


def _counts(ids, counts):
    """Stub metrics giving each shape that many lines."""
    return StubMetrics({
        sid: [f"line {n + 1}" for n in range(count)]
        for sid, count in zip(ids, counts)
    })


# --------------------------------------------------------------------------- #
# The finding
# --------------------------------------------------------------------------- #

def test_the_short_headings_of_a_ragged_row_are_reported(tmp_path: Path) -> None:
    deck, ids = _row(tmp_path)

    found = list(HeadingBalanceRule(_counts(ids, [1, 2, 1, 2])).check(_ctx(deck)))

    assert {i.shape for i in found} == {"Heading 1", "Heading 3"}
    assert all("2 lines" in i.expected for i in found)


def test_a_row_that_already_matches_says_nothing(tmp_path: Path) -> None:
    deck, ids = _row(tmp_path)

    assert list(HeadingBalanceRule(_counts(ids, [2, 2, 2, 2])).check(_ctx(deck))) == []


def test_a_row_more_than_one_line_apart_is_left_alone(tmp_path: Path) -> None:
    """A one-line heading beside a four-line one is a copy-length problem.
    Balancing to four would make three bad headings out of one."""
    deck, ids = _row(tmp_path)

    assert list(HeadingBalanceRule(_counts(ids, [1, 1, 1, 4])).check(_ctx(deck))) == []


def test_boxes_not_in_a_row_are_not_a_row(tmp_path: Path) -> None:
    """Same size, different heights: nobody is comparing them across."""
    deck, ids = _row(tmp_path, same_row=False)

    assert list(HeadingBalanceRule(_counts(ids, [1, 2, 1, 2])).check(_ctx(deck))) == []


def test_without_a_renderer_it_says_nothing(tmp_path: Path) -> None:
    """How many lines a heading takes is not in the file."""
    from formatting_tool.linemetrics import NullLineMetrics

    deck, _ids = _row(tmp_path)

    assert list(HeadingBalanceRule(NullLineMetrics()).check(_ctx(deck))) == []


# --------------------------------------------------------------------------- #
# Where the break falls
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,head,tail", [
    ("MBA SURVEY*", "MBA", "SURVEY*"),
    ("MBA CASEBOOK", "MBA", "CASEBOOK"),
    ("TAILORED TRAININGS*", "TAILORED", "TRAININGS*"),
    # Three words: the split that leaves the two lines closest in width.
    ("FY27 AND FY28 AMBASSADORS", "FY27 AND FY28", "AMBASSADORS"),
])
def test_the_break_falls_at_the_evenest_word(text, head, tail) -> None:
    """At a word, and at the word that balances the two lines. A heading split
    by character count comes out lopsided: "AMBASSADORS" and "illliiill" are
    not the same width."""
    from formatting_tool.apply.fixers import _split_points

    at = _split_points(text, 1)[0]

    assert text[:at].rstrip() == head
    assert text[at:] == tail


def test_a_single_word_cannot_be_split() -> None:
    from formatting_tool.apply.fixers import _split_points

    assert _split_points("CASEBOOK", 1) is None


# --------------------------------------------------------------------------- #
# Applying it
# --------------------------------------------------------------------------- #

def test_the_heading_is_broken_where_the_split_says(tmp_path: Path) -> None:
    from pptx import Presentation

    from formatting_tool.apply import apply_fixes

    deck, ids = _row(tmp_path)
    found = list(HeadingBalanceRule(_counts(ids, [1, 2, 1, 2])).check(_ctx(deck)))
    for issue in found:
        issue.id = issue.fingerprint()
    spec = derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, found, out, selected=[i.id for i in found],
                         spec=spec)

    assert len(result.applied) == 2
    texts = {
        s.name: s.text_frame.text
        for s in Presentation(str(out)).slides[0].shapes
    }
    # A soft return reads as a vertical tab through python-pptx.
    assert texts["Heading 1"] == "MBA\vSURVEY*"
    assert texts["Heading 3"] == "MBA\vCASEBOOK"


def test_the_break_keeps_the_formatting_of_the_run_it_split() -> None:
    """The second line is a copy of the run it came out of, so a heading in
    13pt red does not come back with half of it in the default face."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    from formatting_tool.apply.fixers import _break_at, _split_points

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(0.5))
    box.text_frame.text = "MBA SURVEY*"
    run = box.text_frame.paragraphs[0].runs[0]
    run.font.size = Pt(13)
    run.font.bold = True
    run.font.color.rgb = RGBColor.from_string("C00000")
    paragraph = box.text_frame.paragraphs[0]

    assert _break_at(paragraph, _split_points(paragraph.text, 1)[0])

    both = paragraph.runs
    assert [r.text for r in both] == ["MBA", "SURVEY*"]
    for r in both:
        assert r.font.size == Pt(13)
        assert r.font.bold is True
        assert str(r.font.color.rgb) == "C00000"


def test_a_heading_already_broken_by_hand_is_left_alone(tmp_path: Path) -> None:
    """It is being shaped deliberately, and squaring it up is a design call."""
    from formatting_tool.apply import apply_fixes

    deck, ids = _row(tmp_path, heads=["MBA\vSURVEY*", "TAILORED TRAININGS*",
                                      "MBA CASEBOOK", "AMBASSADORS SITE ON"])
    found = [
        i for i in HeadingBalanceRule(_counts(ids, [1, 2, 1, 2])).check(_ctx(deck))
        if i.shape == "Heading 1"
    ]
    for issue in found:
        issue.id = issue.fingerprint()
    spec = derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, found, out, selected=[i.id for i in found],
                         spec=spec)

    assert result.applied == []
    assert "already carries a manual break" in result.skipped[0].detail


# --------------------------------------------------------------------------- #
# Not fighting itself
# --------------------------------------------------------------------------- #

def test_a_balanced_row_is_exempt_from_the_manual_break_warning(
    tmp_path: Path,
) -> None:
    """Otherwise the tool files a defect against its own fix."""
    deck, ids = _row(tmp_path, heads=["MBA\vSURVEY*", "TAILORED TRAININGS*",
                                      "MBA\vCASEBOOK", "AMBASSADORS SITE ON"])

    balanced = list(
        ManualLineBreakRule(_counts(ids, [2, 2, 2, 2])).check(_ctx(deck))
    )
    ragged = list(
        ManualLineBreakRule(_counts(ids, [2, 1, 2, 1])).check(_ctx(deck))
    )

    assert balanced == []
    # Still reported where the breaks are not squaring anything up.
    assert {i.shape for i in ragged} == {"Heading 1", "Heading 3"}


def test_the_second_round_cannot_undo_the_balance() -> None:
    """The guard that made this work at all.

    The recheck reported the new breaks as forced wraps and the second round
    applied `fix_manual_line_break` to them, so the headings came out exactly
    as they went in -- the tool reversing its own fix inside one run.
    """
    from formatting_tool.apply.applier import _OPPOSED, _reverses_a_fix
    from formatting_tool.models import Category, Issue, Severity, Source

    assert _OPPOSED["typography.manual_line_break"] == {
        "typography.heading_balance"
    }
    issue = Issue(
        category=Category.TYPOGRAPHY, severity=Severity.WARNING, message="x",
        source=Source.RULE, rule_id="typography.manual_line_break",
        slide=1, shape="Heading 1", shape_id=7,
    )

    assert _reverses_a_fix(issue, {(1, 7): {"typography.heading_balance"}})
    assert not _reverses_a_fix(issue, {(1, 7): {"typography.whitespace"}})
    assert not _reverses_a_fix(issue, {(1, 9): {"typography.heading_balance"}})


def test_the_whole_row_ends_up_level(tmp_path: Path) -> None:
    """The goal, measured on the real renderer rather than on a stub.

    Worth having separately because the stub and the renderer can disagree,
    and when they do the renderer is right. This was written first with the
    stub claiming "TAILORED TRAININGS*" already took two lines: it takes one
    at this width, the recheck measured that, and the second round squared it
    up as well -- correctly, and against what the test then asserted.
    """
    from formatting_tool import powerpoint
    from formatting_tool.apply import apply_fixes
    from formatting_tool.linemetrics import PowerPointComMetrics
    from formatting_tool.rules import build_default_rules, run_rules

    if not powerpoint.available():
        pytest.skip("desktop PowerPoint is needed to count lines")

    deck, ids = _row(tmp_path)
    counts = [
        len(PowerPointComMetrics(deck).lines(ShapeKey(1, sid)) or [])
        for sid in ids
    ]
    assert len(set(counts)) > 1, f"the fixture is already level: {counts}"

    spec = derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())
    ctx = RuleContext(deck=read_deck(deck), spec=spec)
    found = [
        i for i in run_rules(ctx, build_default_rules(PowerPointComMetrics(deck)))
        if i.rule_id == "typography.heading_balance"
    ]
    for issue in found:
        issue.id = issue.fingerprint()
    out = tmp_path / "level.pptx"

    apply_fixes(deck, found, out, selected=[i.id for i in found], spec=spec)

    now = [
        len(PowerPointComMetrics(out).lines(ShapeKey(1, sid)) or [])
        for sid in ids
    ]
    assert len(set(now)) == 1, f"the row is still ragged: {now}"
    assert now[0] == max(counts), "it should level up, not down"
