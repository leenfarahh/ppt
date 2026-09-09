"""A component spaced on two gutters that were meant to be one.

Off a real slide: a three-row, two-column component with every horizontal
gutter at 0.058in and every vertical one at 0.097in. Each kind is identical to
the thousandth, so `space.series_uneven` passes it -- nothing is drifting
within a kind -- and the component still reads as two spacings.

NEARLY EQUAL is the whole rule. Measured across four real decks, eleven of
eleven multi-column components had a horizontal gutter different from their
vertical one, most by around 0.30in: tighter across than down is how these
decks are built. So the trigger is not "these differ", it is "these differ by
less than anyone chose on purpose" -- 0.038in apart in the case this was
written for, against 0.137in for the next nearest component in four decks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from formatting_tool.extract import read_deck
from formatting_tool.extract.master_spec import derive_master_spec
from formatting_tool.models import BrandGuidelines
from formatting_tool.rules import RuleContext
from formatting_tool.rules.space import MatrixGutterRule

EMU = 914400


def _matrix(
    tmp_path: Path,
    *,
    across: float = 0.06,
    down: float = 0.10,
    rows: int = 3,
    widths: tuple = (1.5, 5.0),
    height: float = 1.2,
    left: float = 0.5,
    top: float = 1.0,
    nested: bool = False,
    bystander: bool = False,
    downs: Optional[tuple] = None,
) -> Path:
    """A rows x len(widths) component on the given gutters."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    def box(name, x, y, w, h):
        shape = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
        shape.name = name
        shape.text_frame.text = name
        return shape

    y = top
    for row in range(rows):
        x = left
        for column, width in enumerate(widths):
            box(f"Cell {row + 1}-{column + 1}", x, y, width, height)
            x += width + across
        if nested and row == 0:
            # A chart and its caption drawn inside the body cell, sharing its
            # top edge: content, not cells.
            body = left + widths[0] + across
            box("Chart", body + 0.2, y, 2.0, 0.8)
            box("Caption", body + 0.2, y, 0.9, 0.2)
        gap = downs[row] if downs and row < len(downs) else down
        y += height + gap

    if bystander:
        box("Footer", left, 6.8, 12.0, 0.3)
    path = tmp_path / "matrix.pptx"
    prs.save(str(path))
    return path


def _found(deck: Path) -> list:
    spec = derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())
    issues = list(MatrixGutterRule().check(
        RuleContext(deck=read_deck(deck), spec=spec)))
    for issue in issues:
        issue.id = issue.fingerprint()
    return issues


def _boxes(path: Path) -> dict:
    from pptx import Presentation

    return {s.name: s for s in Presentation(str(path)).slides[0].shapes}


def _gutters(path: Path) -> tuple:
    """(across on the top row, down the left column), in inches."""
    boxes = _boxes(path)
    top = sorted((s for s in boxes.values() if s.name.startswith("Cell 1-")),
                 key=lambda s: s.left)
    column = sorted((s for s in boxes.values() if s.name.startswith("Cell")
                     and s.name.endswith("-1")), key=lambda s: s.top)
    across = [round((b.left - (a.left + a.width)) / EMU, 3)
              for a, b in zip(top, top[1:])]
    down = [round((b.top - (a.top + a.height)) / EMU, 3)
            for a, b in zip(column, column[1:])]
    return across, down


# --------------------------------------------------------------------------- #
# The finding
# --------------------------------------------------------------------------- #

def test_two_nearly_equal_gutters_are_reported(tmp_path: Path) -> None:
    """0.06in across, 0.10in down: 0.04in apart, which nobody chose."""
    deck = _matrix(tmp_path, across=0.06, down=0.10)

    found = _found(deck)

    assert len(found) == 1
    assert found[0].shape == "Cell 1-1"          # the corner that will not move
    assert "3x2 component" in found[0].message
    assert "one gutter of 0.060in" in found[0].expected
    assert "0.060in across, 0.100in down" in found[0].found


def test_gutters_far_apart_are_a_decision_and_are_left_alone(
    tmp_path: Path,
) -> None:
    """Eleven of eleven components on four real decks look like this. If the
    rule fired here it would restyle almost every component in the library."""
    deck = _matrix(tmp_path, across=0.06, down=0.36)

    assert _found(deck) == []


def test_the_next_nearest_real_component_is_still_outside_the_window(
    tmp_path: Path,
) -> None:
    """0.137in apart, measured. The window is 0.05in, so there is room on
    both sides of it rather than a threshold sitting on top of live data."""
    deck = _matrix(tmp_path, across=0.058, down=0.195)

    assert _found(deck) == []


def test_one_gutter_throughout_says_nothing(tmp_path: Path) -> None:
    deck = _matrix(tmp_path, across=0.06, down=0.06)

    assert _found(deck) == []


def test_a_hundredth_of_disagreement_says_nothing(tmp_path: Path) -> None:
    """Below the slack the two gutters already are one number."""
    deck = _matrix(tmp_path, across=0.06, down=0.07)

    assert _found(deck) == []


def test_a_kind_that_disagrees_with_itself_is_left_to_the_other_rule(
    tmp_path: Path,
) -> None:
    """`space.series_uneven` has drift within a kind to say. Reporting both
    would be asking two fixers to re-space one component."""
    deck = _matrix(tmp_path, across=0.06, downs=(0.10, 0.16))

    assert _found(deck) == []


def test_touching_rows_are_not_this_rule(tmp_path: Path) -> None:
    """A zero or negative gutter is an overlap, and `space.overlap` owns it."""
    deck = _matrix(tmp_path, across=0.06, down=0.0)

    assert _found(deck) == []


def test_a_single_row_has_no_vertical_gutter_to_compare(tmp_path: Path) -> None:
    deck = _matrix(tmp_path, rows=1)

    assert _found(deck) == []


def test_content_drawn_inside_a_cell_does_not_hide_the_component(
    tmp_path: Path,
) -> None:
    """The bug that made this worse than missing it.

    A body cell on the real slide carried a chart and a caption, both sharing
    the row's top edge. Counted as cells they gave the top row four members
    against two below, the column signatures stopped matching, and the
    three-row component was read as its bottom two rows -- so the fix would
    have re-spaced two rows and left the third on the old gutter.
    """
    deck = _matrix(tmp_path, nested=True)

    found = _found(deck)

    assert len(found) == 1
    assert "3x2 component" in found[0].message, found[0].message


def test_two_components_side_by_side_are_not_one_row(tmp_path: Path) -> None:
    """Both halves of the real slide share every row, so without a boundary
    test the space BETWEEN them reads as a horizontal gutter."""
    from pptx import Presentation
    from pptx.util import Inches

    deck = _matrix(tmp_path, across=0.06, down=0.10,
                   widths=(1.5, 3.0, 1.5, 3.0))

    # Widen the middle gap into a boundary: 0.06, 0.41, 0.06.
    prs = Presentation(str(deck))
    for shape in prs.slides[0].shapes:
        if shape.name.endswith(("-3", "-4")):
            shape.left += Inches(0.35)
    prs.save(str(deck))

    found = _found(deck)

    # Each half is its own component, and each is reported on its own terms.
    assert len(found) == 2
    assert all("3x2 component" in issue.message for issue in found)


# --------------------------------------------------------------------------- #
# Applying it
# --------------------------------------------------------------------------- #

def test_the_component_is_re_spaced_onto_the_smaller_gutter(
    tmp_path: Path,
) -> None:
    from formatting_tool.apply import apply_fixes

    deck = _matrix(tmp_path, across=0.06, down=0.10)
    found = _found(deck)
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, found, out, selected=[i.id for i in found])

    assert len(result.applied) == 1
    across, down = _gutters(out)
    assert across == [0.06]
    assert down == [0.06, 0.06], f"still two gutters: {down}"


def test_nothing_is_resized(tmp_path: Path) -> None:
    """Holding the outer box and letting the cells absorb the change was the
    other option. Resizing a text cell rewraps its copy, and a rewrap can push
    text out of its box, so that trades a spacing defect for an overflow.
    Moving cells cannot do that."""
    from formatting_tool.apply import apply_fixes

    deck = _matrix(tmp_path, across=0.06, down=0.10)
    was = {name: (s.width, s.height) for name, s in _boxes(deck).items()}
    found = _found(deck)
    out = tmp_path / "fixed.pptx"

    apply_fixes(deck, found, out, selected=[i.id for i in found])

    now = {name: (s.width, s.height) for name, s in _boxes(out).items()}
    assert now == was


def test_the_top_left_corner_holds_and_bystanders_do_not_move(
    tmp_path: Path,
) -> None:
    """Re-spacing from the corner outwards means the component can only
    tighten towards where it already starts, so nothing outside it is
    disturbed."""
    from formatting_tool.apply import apply_fixes

    deck = _matrix(tmp_path, across=0.06, down=0.10, bystander=True)
    was = _boxes(deck)
    corner_was = (was["Cell 1-1"].left, was["Cell 1-1"].top)
    footer_was = (was["Footer"].left, was["Footer"].top)
    found = _found(deck)
    out = tmp_path / "fixed.pptx"

    apply_fixes(deck, found, out, selected=[i.id for i in found])

    now = _boxes(out)
    assert (now["Cell 1-1"].left, now["Cell 1-1"].top) == corner_was
    assert (now["Footer"].left, now["Footer"].top) == footer_was


def test_applying_it_twice_changes_nothing_the_second_time(
    tmp_path: Path,
) -> None:
    from formatting_tool.apply import apply_fixes

    deck = _matrix(tmp_path, across=0.06, down=0.10)
    found = _found(deck)
    out = tmp_path / "fixed.pptx"
    apply_fixes(deck, found, out, selected=[i.id for i in found])

    assert _found(out) == []


# --------------------------------------------------------------------------- #
# Which gutter wins
# --------------------------------------------------------------------------- #

def test_the_target_is_the_smaller_gutter_whatever_the_counts() -> None:
    """Counting which gutter occurs more often was the first rule, and it gave
    the two halves of one real slide DIFFERENT targets.

    They are the same component drawn twice, and one detected row of
    difference between them -- three rows on the right, two on the left where
    a chart sat inside a cell -- flipped the count: one half loosened to
    0.097in and the other tightened to 0.058in. Squaring both up would have
    left the slide less consistent than it started. The smaller is stable
    under a miscount, and it cannot push a component into its neighbour.
    """
    from formatting_tool.rules.space import _target_gutter

    assert _target_gutter([0.058], [0.097, 0.097]) == pytest.approx(0.058)
    assert _target_gutter([0.058, 0.058], [0.097]) == pytest.approx(0.058)
    assert _target_gutter([0.097, 0.097], [0.058]) == pytest.approx(0.058)


def test_a_row_is_clustered_against_its_neighbours_not_rounded_into_buckets(
    tmp_path: Path,
) -> None:
    """Rounding tops into fixed buckets has edges: a tenth-of-an-inch bucket
    puts 0.099 and 0.101in into different rows, and one component read as two
    finds no vertical gutter in either."""
    from pptx import Presentation
    from pptx.util import Inches

    deck = _matrix(tmp_path, across=0.06, down=0.10, top=1.049)

    prs = Presentation(str(deck))
    for shape in prs.slides[0].shapes:
        if shape.name == "Cell 1-2":
            shape.top += Inches(0.004)          # 1.049 and 1.053: one row
    prs.save(str(deck))

    assert len(_found(deck)) == 1
