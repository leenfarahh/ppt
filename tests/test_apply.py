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
        "space.overlap",          # names two boxes, cannot know which moves
        "logo.missing",           # needs a logo file
        "title.missing",          # needs copy written
        "layout.not_in_master",   # that is what rebuild is for
    ):
        assert fixer_for(_issue(rule_id)) is None


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


def test_grid_snapping_is_not_applied_automatically() -> None:
    """A slide has several legitimate columns and the rule snaps to the busiest.

    On a real deck that pulled a section label 0.20in off the table it
    captioned. The detection stays; the move needs a person.
    """
    assert fixer_for(_issue("space.alignment_grid")) is None
    assert "design call" in why_not_fixable(_issue("space.alignment_grid"))
