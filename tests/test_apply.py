"""Tests for applying selected findings to a deck.

The cases that matter here are the ones where applying the wrong thing damages
a client file: a fix landing on a shape that merely shares a name with the one
the finding meant, and two fixes on one shape where the second undoes the
first. Both were real, on a real deck, before these tests existed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from formatting_tool.apply import (
    ApplyError,
    apply_fixes,
    fixable,
    fixer_for,
    why_not_fixable,
)
from formatting_tool.apply.fixers import fix_order  # noqa: F401
from formatting_tool.rules.space import DECLARED_GRID
from formatting_tool.models import Category, Issue, Severity, Source
from formatting_tool.report.reader import load_report


def _issue(rule_id: str, **kwargs) -> Issue:
    issue = Issue(
        category=kwargs.pop("category", Category.SPACE),
        severity=kwargs.pop("severity", Severity.ERROR),
        message=kwargs.pop("message", f"{rule_id} finding"),
        source=kwargs.pop("source", Source.RULE),
        rule_id=rule_id,
        deck="messy.pptx",
        **kwargs,
    )
    issue.id = issue.fingerprint()
    return issue


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

def test_only_the_ticked_findings_are_applied(tmp_path: Path) -> None:
    """The whole point: the designer's list, and nothing else."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    a = slide.shapes.add_textbox(Inches(-1), Inches(1), Inches(2), Inches(1))
    a.name = "Stray A"
    b = slide.shapes.add_textbox(Inches(-1), Inches(3), Inches(2), Inches(1))
    b.name = "Stray B"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issues = [
        _issue("space.off_canvas", slide=1, shape="Stray A", shape_id=a.shape_id),
        _issue("space.off_canvas", slide=1, shape="Stray B", shape_id=b.shape_id),
    ]
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, issues, out, selected=[issues[0].id])

    assert len(result.applied) == 1
    fixed = {s.name: s.left for s in Presentation(str(out)).slides[0].shapes}
    assert fixed["Stray A"] == 0            # ticked, moved onto the canvas
    assert fixed["Stray B"] < 0             # not ticked, left exactly as it was


def test_an_unknown_id_is_refused(tmp_path: Path) -> None:
    """A selection that does not match the report has come apart from it.

    Applying the rest quietly would leave the designer believing a fix landed
    when it did not.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation

    deck = tmp_path / "messy.pptx"
    Presentation().save(str(deck))

    with pytest.raises(ApplyError, match="no finding with id"):
        apply_fixes(deck, [], tmp_path / "out.pptx", selected=["deadbeef"])


def test_the_input_deck_is_never_modified(tmp_path: Path) -> None:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(-1), Inches(1), Inches(2), Inches(1))
    box.name = "Stray"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))
    before = deck.read_bytes()

    apply_fixes(
        deck,
        [_issue("space.off_canvas", slide=1, shape="Stray", shape_id=box.shape_id)],
        tmp_path / "fixed.pptx",
        selected=None,
    )

    assert deck.read_bytes() == before


# --------------------------------------------------------------------------- #
# Finding the right shape
# --------------------------------------------------------------------------- #

def test_a_fix_lands_on_the_shape_id_not_a_shared_name(tmp_path: Path) -> None:
    """Shape names repeat freely: sixteen "Pentagon 7" on one slide is real.

    Matching on the name applied the fix to whichever came first, which on a
    client deck means editing a shape nobody asked about.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    first = slide.shapes.add_textbox(Inches(-1), Inches(1), Inches(2), Inches(1))
    second = slide.shapes.add_textbox(Inches(-1.5), Inches(3), Inches(2), Inches(1))
    first.name = second.name = "Twin"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "space.off_canvas", slide=1, shape="Twin", shape_id=second.shape_id
    )
    out = tmp_path / "fixed.pptx"

    apply_fixes(deck, [issue], out, selected=[issue.id])

    fixed = list(Presentation(str(out)).slides[0].shapes)
    by_id = {s.shape_id: s.left for s in fixed}
    assert by_id[second.shape_id] == 0                  # the one that was named
    assert by_id[first.shape_id] == Inches(-1)          # its twin, untouched


def test_an_ambiguous_name_with_no_id_is_skipped(tmp_path: Path) -> None:
    """Editing the wrong shape is worse than editing none."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for _ in range(2):
        box = slide.shapes.add_textbox(Inches(-1), Inches(1), Inches(2), Inches(1))
        box.name = "Twin"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue("space.off_canvas", slide=1, shape="Twin")   # no shape_id
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert not result.applied
    assert "could not find" in result.skipped[0].detail


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #

def test_the_canvas_constraint_wins_over_the_grid_preference(tmp_path: Path) -> None:
    """Two fixes on one shape, and the second decides.

    Snapping a box to a grid line after clamping it back onto the canvas
    pushes it straight off again. A shape must be on the canvas and would
    merely prefer to be on the grid, so the hard constraint runs last.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(11.25), Inches(1), Inches(2.4), Inches(1))
    box.name = "TextBox 28"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issues = [
        _issue("space.off_canvas", slide=1, shape=box.name, shape_id=box.shape_id),
        _issue(
            "space.repeat_out_of_line", slide=1, shape=box.name,
            shape_id=box.shape_id, expected="left 11.19in, as the rest of the set",
        ),
    ]
    out = tmp_path / "fixed.pptx"

    apply_fixes(deck, issues, out, selected=[i.id for i in issues])

    fixed = Presentation(str(out))
    shape = fixed.slides[0].shapes[0]
    assert shape.left + shape.width <= fixed.slide_width
    # The grid fix ran first and was overruled, not skipped.
    assert fix_order(issues[1]) < fix_order(issues[0])


# --------------------------------------------------------------------------- #
# Which findings have a fixer
# --------------------------------------------------------------------------- #

def test_judgement_calls_have_no_fixer() -> None:
    """A finding a machine cannot correct without guessing is left alone."""
    for rule_id in (
        "logo.missing",           # needs a logo file
        "title.missing",          # needs copy written
        "layout.not_in_master",   # that is what rebuild is for
        # Two shapes disagreeing about a height, with nothing in the geometry
        # to say which of them moved.
        "space.mirror_pair_offset",
    ):
        assert fixer_for(_issue(rule_id)) is None


def test_an_overlap_moves_the_shape_on_top() -> None:
    """`space.overlap` was on this list, on the grounds that it names two boxes
    and cannot know which should move. The geometry cannot -- but z-order can:
    it records which of the two landed on the other. The rule now reports the
    finding on the shape in front, and that is the one that moves."""
    assert fixer_for(_issue("space.overlap")) is not None


def test_an_ai_finding_has_no_fixer() -> None:
    """AI findings are judgements phrased for a reader, with no target."""
    issue = _issue("space.overlap", source=Source.AI, slide=1, shape="A")
    assert fixer_for(issue) is None
    assert fixable([issue]) == []


# --------------------------------------------------------------------------- #
# Round trip through the report JSON
# --------------------------------------------------------------------------- #

def test_ids_survive_the_json_round_trip(tmp_path: Path) -> None:
    """The designer ticks in one place and applies in another."""
    issue = _issue("space.off_canvas", slide=2, shape="TextBox 28", shape_id=29)
    document = {
        "master": "m.pptx",
        "decks": ["messy.pptx"],
        "generated_at": "now",
        "issues": [issue.to_dict()],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_report(path)

    assert [i.id for i in loaded.issues] == [issue.id]
    assert loaded.issues[0].shape_id == 29


def test_a_report_without_ids_still_applies(tmp_path: Path) -> None:
    """An older report is not refused; the same inputs give the same id."""
    issue = _issue("space.off_canvas", slide=2, shape="TextBox 28", shape_id=29)
    entry = issue.to_dict()
    entry.pop("id")
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps({"master": "m", "decks": ["d"], "generated_at": "now",
                    "issues": [entry]}),
        encoding="utf-8",
    )

    loaded = load_report(path)

    assert loaded.issues[0].id == issue.id


# --------------------------------------------------------------------------- #
# The fixers added after a designer asked why the new findings were not fixable
# --------------------------------------------------------------------------- #

def test_a_repeat_out_of_line_is_aligned_onto_the_set(tmp_path: Path) -> None:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(6.63), Inches(2), Inches(0.53), Inches(0.63))
    box.name = "Text Placeholder 30"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "space.repeat_out_of_line", slide=1, shape=box.name, shape_id=box.shape_id,
        expected="left 6.48in, as the rest of the set",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    assert Presentation(str(out)).slides[0].shapes[0].left == Inches(6.48)


def test_a_margin_breach_is_moved_inside_the_frame(tmp_path: Path) -> None:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(11.25), Inches(2), Inches(2.4), Inches(0.5))
    box.name = "TextBox 28"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "space.safe_margin", slide=1, shape=box.name, shape_id=box.shape_id,
        expected="top 0.4in, right 0.19in, bottom 0.36in, left 0.21in safe margin",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    fixed = Presentation(str(out))
    shape = fixed.slides[0].shapes[0]
    assert shape.left + shape.width <= fixed.slide_width - Inches(0.19)


def test_a_shape_too_big_for_the_frame_is_left_for_a_person(tmp_path: Path) -> None:
    """Nudging cannot bring it inside, and resizing is a design decision."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(0), Inches(2), Inches(13.33), Inches(0.5))
    box.name = "Full width"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "space.safe_margin", slide=1, shape=box.name, shape_id=box.shape_id,
        expected="top 0.4in, right 0.19in, bottom 0.36in, left 0.21in safe margin",
    )

    result = apply_fixes(deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id])

    assert not result.applied
    assert "nothing to change" in result.skipped[0].detail


def test_the_margin_fix_runs_after_the_canvas_fix() -> None:
    """The safe margin sits inside the canvas, so it is the stronger constraint
    and has to have the last word on a shape both findings touch."""
    assert fix_order(_issue("space.safe_margin")) > fix_order(_issue("space.off_canvas"))


# --------------------------------------------------------------------------- #
# Guards added after the fixes visibly damaged a real deck
# --------------------------------------------------------------------------- #

def test_a_shape_parked_off_the_slide_is_never_dragged_into_view(tmp_path: Path) -> None:
    """It is invisible where it is, so nobody is looking at a defect.

    On a real deck two stray diagram fragments were parked off the left edge.
    "Fixing" them slid both into view, on top of an icon.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(-0.65), Inches(4), Inches(0.47), Inches(0.22))
    box.name = "Group 270"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue("space.off_canvas", slide=1, shape=box.name, shape_id=box.shape_id)
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert not result.applied
    assert "entirely off the slide" in result.skipped[0].detail
    assert Presentation(str(out)).slides[0].shapes[0].left == Inches(-0.65)


def test_a_shape_straddling_the_edge_is_still_brought_back(tmp_path: Path) -> None:
    """Partly visible is a real defect; the guard must not swallow it too."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(-0.4), Inches(4), Inches(2), Inches(0.5))
    box.name = "Half off"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue("space.off_canvas", slide=1, shape=box.name, shape_id=box.shape_id)
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    assert Presentation(str(out)).slides[0].shapes[0].left == 0


def test_a_move_that_buries_a_neighbour_is_reverted(tmp_path: Path) -> None:
    """Trading a measurable finding for a visible one is the wrong trade.

    The caption already overlapped the portrait above it, so it gained no new
    neighbour by sliding further under it. Comparing area rather than which
    neighbours are touched is what catches this.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    photo = slide.shapes.add_textbox(Inches(9.6), Inches(6.0), Inches(3.35), Inches(0.8))
    photo.name = "Group 39"
    caption = slide.shapes.add_textbox(Inches(11.25), Inches(6.74), Inches(2.4), Inches(0.44))
    caption.name = "TextBox 28"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "space.safe_margin", slide=1, shape=caption.name, shape_id=caption.shape_id,
        expected="top 0.4in, right 0.19in, bottom 0.36in, left 0.21in safe margin",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert not result.applied
    assert "further over Group 39" in result.skipped[0].detail
    kept = {s.name: s.left for s in Presentation(str(out)).slides[0].shapes}
    assert kept["TextBox 28"] == Inches(11.25)


def test_grid_snapping_is_available() -> None:
    """Registered, after a spell disabled: a slide carries several legitimate
    columns and snapping to the wrong one damaged a real deck. What changed is
    that the grid can now come from the master rather than from the deck being
    audited, and that the applier refuses a move which ends an alignment the
    shape already had. See the two tests below."""
    assert fixer_for(_issue("space.alignment_grid")) is not None


def test_snapping_that_would_break_an_alignment_is_refused(tmp_path: Path) -> None:
    """The exact failure that got this fixer disabled.

    A section label sharing its left edge with the table it captions, and a
    grid line 0.15in to the left of both. Snapping the label alone satisfies
    the rule and pulls the label off the table, which is the defect a client
    sees. No overlap changes, so the older guard could not catch it.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    table = slide.shapes.add_textbox(Inches(3.0), Inches(2.0), Inches(4.0), Inches(2.0))
    table.name = "Table"
    label = slide.shapes.add_textbox(Inches(3.0), Inches(4.2), Inches(2.0), Inches(0.4))
    label.name = "Section label"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "space.alignment_grid",
        message=f"off the 2.85in grid line {DECLARED_GRID}.",
        slide=1,
        shape="Section label",
        shape_id=label.shape_id,
        expected="2.85in",
        found="3.00in",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out)

    assert result.applied == []
    assert "break its alignment" in result.skipped[0].detail
    kept = {s.name: s.left for s in Presentation(str(out)).slides[0].shapes}
    assert kept["Section label"] == Inches(3.0)


def test_a_shape_with_nothing_to_break_is_snapped(tmp_path: Path) -> None:
    """The other side of that guard, so it cannot refuse everything."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    stray = slide.shapes.add_textbox(Inches(3.0), Inches(4.2), Inches(2.0), Inches(0.4))
    stray.name = "Stray"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "space.alignment_grid",
        message=f"off the 2.85in grid line {DECLARED_GRID}.",
        slide=1,
        shape="Stray",
        shape_id=stray.shape_id,
        expected="2.85in",
        found="3.00in",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out)

    assert len(result.applied) == 1
    kept = {s.name: s.left for s in Presentation(str(out)).slides[0].shapes}
    assert kept["Stray"] == Inches(2.85)


def test_alignment_with_a_shape_the_report_also_faults_is_not_protected(
    tmp_path: Path,
) -> None:
    """Two shapes wrong the same way are not a relationship worth keeping.

    Both boxes sit off the canvas at the same left edge. Protecting that
    alignment would refuse to bring either back, which is how this guard first
    broke the off-canvas fixer.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    a = slide.shapes.add_textbox(Inches(-1), Inches(1), Inches(2), Inches(1))
    a.name = "Stray A"
    b = slide.shapes.add_textbox(Inches(-1), Inches(3), Inches(2), Inches(1))
    b.name = "Stray B"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issues = [
        _issue("space.off_canvas", slide=1, shape="Stray A", shape_id=a.shape_id),
        _issue("space.off_canvas", slide=1, shape="Stray B", shape_id=b.shape_id),
    ]
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, issues, out, selected=[issues[0].id])

    assert len(result.applied) == 1
    kept = {s.name: s.left for s in Presentation(str(out)).slides[0].shapes}
    assert kept["Stray A"] == 0


def test_snapping_to_an_inferred_grid_is_refused(tmp_path: Path) -> None:
    """The failure that got this fixer disabled, reproduced and then declined.

    Run against a grid inferred from the deck's own habits, this snapped a
    5.48in section header 0.20in onto a line whose entire support was five
    1.67in pentagon labels elsewhere on the slide. A line four shapes happen
    to share is not a column, so an inferred grid is not acted on at all.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    header = slide.shapes.add_textbox(Inches(7.46), Inches(1.0), Inches(5.48), Inches(0.4))
    header.name = "Section header"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "space.alignment_grid",
        message="off the 7.66in grid line the rest of the deck follows.",
        slide=1,
        shape="Section header",
        shape_id=header.shape_id,
        expected="7.66in",
        found="7.46in",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out)

    assert result.applied == []
    assert "inferred from this deck" in result.skipped[0].detail
    kept = {s.name: s.left for s in Presentation(str(out)).slides[0].shapes}
    assert kept["Section header"] == Inches(7.46)


def test_a_deck_level_finding_says_so_rather_than_blaming_the_deck(
    tmp_path: Path,
) -> None:
    """The systematic grid finding names no shape, because it is about the
    deck. The missing-shape path would have said the deck had changed."""
    pytest.importorskip("pptx")
    from pptx import Presentation

    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue("space.alignment_grid", expected="0.92in", found="0.48in")
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out)

    assert result.applied == []
    assert "the deck as a whole" in result.skipped[0].detail



# --------------------------------------------------------------------------- #
# The fixers added after the "187 need a designer" count was looked into
# --------------------------------------------------------------------------- #

def test_a_satellite_is_nudged_back_onto_the_offset_its_cohort_shares(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(
        Inches(6.63), Inches(2.42), Inches(0.53), Inches(0.63)
    )
    box.name = "Text Placeholder 30"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "space.satellite_offset", slide=1, shape=box.name, shape_id=box.shape_id,
        expected=(
            "offset +3.04, +0.00in from the 4.72 x 0.63in shape it pairs with, "
            "as the other 5 have"
        ),
        found="offset +3.19, +0.01in",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    fixed = Presentation(str(out)).slides[0].shapes[0]
    assert fixed.left == Inches(6.48)
    assert fixed.top == Inches(2.41)


def test_a_satellite_fix_stands_down_once_another_fix_has_moved_the_shape(
    tmp_path: Path,
) -> None:
    """The whole hazard of a delta: it is only true of the position it was
    measured against. On a real deck both repeat rules fired on one shape and
    described the same 0.15in drift, and applying both subtracted it twice.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(
        Inches(6.63), Inches(2.42), Inches(0.53), Inches(0.63)
    )
    box.name = "Text Placeholder 30"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    edge = _issue(
        "space.repeat_out_of_line", slide=1, shape=box.name, shape_id=box.shape_id,
        expected="left 6.48in, as the rest of the set",
    )
    offset = _issue(
        "space.satellite_offset", slide=1, shape=box.name, shape_id=box.shape_id,
        expected="offset +3.04, +0.00in from the 4.72 x 0.63in shape it pairs with",
        found="offset +3.19, +0.01in",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [edge, offset], out, selected=[edge.id, offset.id])

    assert [o.issue.rule_id for o in result.applied] == ["space.repeat_out_of_line"]
    assert "already moved this shape" in result.skipped[0].detail
    # 6.33in is where the doubled subtraction would have put it.
    assert Presentation(str(out)).slides[0].shapes[0].left == Inches(6.48)


def test_a_drifting_title_is_moved_onto_the_decks_title_position(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(
        Inches(0.71), Inches(0.63), Inches(12.36), Inches(0.92)
    )
    box.name = "Title 1"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "title.position_inconsistent", category=Category.TITLE,
        slide=1, shape=box.name, shape_id=box.shape_id,
        expected="0.48, 0.42in", found="0.71, 0.63in",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    fixed = Presentation(str(out)).slides[0].shapes[0]
    assert (fixed.left, fixed.top) == (Inches(0.48), Inches(0.42))


def test_a_title_placed_somewhere_else_entirely_is_left_alone(tmp_path: Path) -> None:
    """A divider or a cover is not a content slide with a drifting title."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(
        Inches(0.48), Inches(3.10), Inches(12.36), Inches(0.92)
    )
    box.name = "Title 1"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "title.position_inconsistent", category=Category.TITLE,
        slide=1, shape=box.name, shape_id=box.shape_id,
        expected="0.48, 0.42in", found="0.48, 3.10in",
    )

    result = apply_fixes(deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id])

    assert not result.applied
    assert "too far to be drift" in result.skipped[0].detail


def test_only_the_position_half_of_the_logo_rule_is_mechanical(
    tmp_path: Path,
) -> None:
    """One rule id, three findings. Two of them are composition decisions and
    say so rather than failing silently.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(0.90), Inches(0.55), Inches(1.2), Inches(0.4))
    box.name = "Logo"
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    def _logo(**kwargs):
        return _issue(
            "logo.geometry", category=Category.LOGO, slide=1,
            shape=box.name, shape_id=box.shape_id, **kwargs,
        )

    position = _logo(expected="0.48, 0.42in", found="0.90, 0.55in")
    width = _logo(expected=">= 2.00in wide", found="1.20in")
    corner = _logo(expected="top-left or top-right", found="bottom-right")

    result = apply_fixes(
        deck, [position], tmp_path / "moved.pptx", selected=[position.id]
    )
    assert len(result.applied) == 1
    moved = Presentation(str(tmp_path / "moved.pptx")).slides[0].shapes[0]
    assert (moved.left, moved.top) == (Inches(0.48), Inches(0.42))

    for issue, reason in ((width, "minimum width"), (corner, "which corner")):
        skipped = apply_fixes(
            deck, [issue], tmp_path / "same.pptx", selected=[issue.id]
        )
        assert not skipped.applied
        assert reason in skipped.skipped[0].detail


def test_the_full_stop_comes_off_a_title(tmp_path: Path) -> None:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.name = "Title 1"
    box.text_frame.text = "Where the value comes from. "
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "typography.terminal_punctuation", category=Category.TYPOGRAPHY,
        severity=Severity.INFO, slide=1, shape=box.name, shape_id=box.shape_id,
        expected="no terminal punctuation on titles",
        found="Where the value comes from.",
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    text = Presentation(str(out)).slides[0].shapes[0].text_frame.text
    assert text == "Where the value comes from"


def test_an_ellipsis_on_a_title_is_left_alone(tmp_path: Path) -> None:
    """Three dots are a mark somebody chose; taking one off makes it a typo."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.name = "Title 1"
    box.text_frame.text = "And then..."
    deck = tmp_path / "messy.pptx"
    prs.save(str(deck))

    issue = _issue(
        "typography.terminal_punctuation", category=Category.TYPOGRAPHY,
        severity=Severity.INFO, slide=1, shape=box.name, shape_id=box.shape_id,
        expected="no terminal punctuation on titles", found="And then...",
    )

    result = apply_fixes(deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id])

    assert not result.applied
    assert "ellipsis" in result.skipped[0].detail


# --------------------------------------------------------------------------- #
# The classification itself
# --------------------------------------------------------------------------- #
#
# Every one of these was a real gap. `space.satellite_offset` and
# `logo.geometry` had neither a fixer nor an explanation, so the UI told the
# designer "no fixer is written for this rule", which reads as an oversight
# rather than an answer. And `NEEDS_A_PERSON` carried a key for
# `color.inconsistent_use`, a rule id that does not exist -- the rule is
# `color.inconsistent_variants` -- so its explanation could never fire.
#
# Neither is visible from a fixer's own tests: the fixers all passed. Only
# comparing the two tables against the rule registry finds them.

def _all_rule_ids() -> set[str]:
    from formatting_tool.rules import build_default_rules, build_master_rules

    return {
        rule.id for rule in list(build_default_rules()) + list(build_master_rules())
    }


def test_every_rule_is_fixable_or_says_why_not() -> None:
    """No finding may reach a designer without an answer to "why me?"."""
    from formatting_tool.apply.fixers import FIXERS, NEEDS_A_PERSON

    unanswered = sorted(
        rule_id
        for rule_id in _all_rule_ids()
        if rule_id not in FIXERS and rule_id not in NEEDS_A_PERSON
    )
    assert unanswered == [], (
        "these rules would be reported as 'no fixer is written for this rule': "
        + ", ".join(unanswered)
    )


def test_the_tables_name_no_rule_that_does_not_exist() -> None:
    """A stale key is an explanation that never appears, and looks fine."""
    from formatting_tool.apply.fixers import FIXERS, NEEDS_A_PERSON

    known = _all_rule_ids()
    assert sorted(set(FIXERS) - known) == []
    assert sorted(set(NEEDS_A_PERSON) - known) == []


def test_a_rule_is_never_both_fixable_and_a_designers_job() -> None:
    from formatting_tool.apply.fixers import FIXERS, NEEDS_A_PERSON

    assert sorted(set(FIXERS) & set(NEEDS_A_PERSON)) == []
