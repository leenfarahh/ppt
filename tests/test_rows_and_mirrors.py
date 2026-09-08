"""Rows, columns and mirrored pairs inside a repeated set.

The blind spot these close. Every other space rule models a set as shapes
sharing ONE edge: `space.repeat_out_of_line` finds a series and asks which
left or top edge the majority agrees on. Two columns of five have two lefts
and five tops, so no edge is shared, nothing is out of line with anything, and
a shape 0.08in low is invisible. Measured before these existed, both that
layout and an eleven-node ring with one node low produced not one space
finding.

The split between the two rules is about what the geometry can settle:

    3 or more in a row   a majority exists, the odd one out is the defect, and
                         it is fixable.
    exactly 2            no majority. They disagree about a height and nothing
                         says which moved, so both are named and a designer
                         decides.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from formatting_tool.models import BrandGuidelines


def _rules(deck: Path):
    from formatting_tool.extract import read_deck
    from formatting_tool.extract.master_spec import derive_master_spec
    from formatting_tool.linemetrics import NullLineMetrics
    from formatting_tool.rules import RuleContext, build_default_rules, run_rules

    spec = derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())
    context = RuleContext(deck=read_deck(deck), spec=spec)
    return run_rules(context, build_default_rules(metrics=NullLineMetrics()))


def _of(deck: Path, rule_id: str):
    return [i for i in _rules(deck) if i.rule_id == rule_id]


def _slide(tmp_path: Path, name: str):
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    return prs, prs.slides.add_slide(prs.slide_layouts[6]), tmp_path / name


def _box(slide, name, x, y, w=2.0, h=1.2):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h)
    )
    shape.name = name
    return shape


# --------------------------------------------------------------------------- #
# Rows and columns, where a majority settles it
# --------------------------------------------------------------------------- #

def test_a_cell_off_its_row_in_a_grid_is_found(tmp_path: Path) -> None:
    """A grid is where `space.repeat_out_of_line` correctly gives up: a row
    and a column both exist, so neither is "the" edge of the set."""
    prs, slide, path = _slide(tmp_path, "grid.pptx")
    for row in range(3):
        for column in range(3):
            low = 0.08 if (row, column) == (1, 1) else 0.0
            _box(slide, f"Cell {row}{column}", 1.0 + column * 3.0, 1.0 + row * 2.0 + low)
    prs.save(str(path))

    found = _of(path, "space.row_out_of_line")

    assert len(found) == 1
    assert found[0].shape == "Cell 11"
    assert "centre y" in found[0].expected


def test_a_tidy_grid_says_nothing(tmp_path: Path) -> None:
    prs, slide, path = _slide(tmp_path, "grid.pptx")
    for row in range(3):
        for column in range(3):
            _box(slide, f"Cell {row}{column}", 1.0 + column * 3.0, 1.0 + row * 2.0)
    prs.save(str(path))

    assert _of(path, "space.row_out_of_line") == []


def test_a_single_row_is_left_to_the_rule_that_already_owns_it(
    tmp_path: Path,
) -> None:
    """Five cards sharing a top edge is exactly `space.repeat_out_of_line`,
    and saying it twice helps nobody."""
    prs, slide, path = _slide(tmp_path, "cards.pptx")
    for i in range(5):
        _box(slide, f"Card {i}", 1.0 + i * 2.3, 3.0 + (0.08 if i == 2 else 0.0))
    prs.save(str(path))

    assert _of(path, "space.row_out_of_line") == []
    assert len(_of(path, "space.repeat_out_of_line")) == 1


# --------------------------------------------------------------------------- #
# Pairs, where nothing settles it
# --------------------------------------------------------------------------- #

def test_two_columns_with_one_row_off_are_found(tmp_path: Path) -> None:
    """The case that produced nothing at all before: two lefts, five tops, no
    shared edge anywhere."""
    prs, slide, path = _slide(tmp_path, "columns.pptx")
    for i in range(5):
        _box(slide, f"Left {i}", 2.0, 1.0 + i * 1.2)
        _box(slide, f"Right {i}", 9.0, 1.0 + i * 1.2 + (0.08 if i == 2 else 0.0))
    prs.save(str(path))

    found = _of(path, "space.mirror_pair_offset")

    assert len(found) == 1
    assert "Left 2" in found[0].message and "Right 2" in found[0].message


def test_a_ring_pairs_across_its_axis(tmp_path: Path) -> None:
    """Eleven nodes around a centre, one 0.08in low. The odd node at the
    bottom sits on the axis and has no partner, which is correct."""
    prs, slide, path = _slide(tmp_path, "ring.pptx")
    cx, cy, radius = 6.667, 3.75, 2.6
    for k in range(11):
        angle = math.radians(-90 + k * (360 / 11))
        low = 0.08 if k == 10 else 0.0
        _box(
            slide, f"Node {k:02d}",
            cx + radius * math.cos(angle) - 0.45,
            cy + radius * math.sin(angle) - 0.45 + low,
            0.9, 0.9,
        )
    prs.save(str(path))

    found = _of(path, "space.mirror_pair_offset")

    assert len(found) == 1
    assert "0.08in apart" in found[0].message


def test_a_vertical_stack_is_not_a_mirrored_arrangement(tmp_path: Path) -> None:
    """The false positive this cost on a real deck: six identical boxes in a
    column all share a centre x, that centre x is the axis, and every one of
    them reflects onto every other one for nothing. Eight findings about a
    stack that is not mirrored at all."""
    prs, slide, path = _slide(tmp_path, "stack.pptx")
    for i in range(6):
        _box(slide, f"Row {i}", 1.34, 2.41 + i * 0.74, 4.72, 0.63)
    prs.save(str(path))

    assert _of(path, "space.mirror_pair_offset") == []


def test_two_shapes_either_side_of_a_scatter_are_a_coincidence(
    tmp_path: Path,
) -> None:
    """Most of the set has to pair before the arrangement is a mirror. Without
    that, any busy slide produces findings."""
    prs, slide, path = _slide(tmp_path, "scatter.pptx")
    for i, (x, y) in enumerate([(1.0, 1.0), (11.0, 1.4), (3.4, 4.2), (8.1, 6.0)]):
        _box(slide, f"Loose {i}", x, y)
    prs.save(str(path))

    assert _of(path, "space.mirror_pair_offset") == []


def test_a_pair_is_left_for_a_designer() -> None:
    """Two shapes disagree about a height and nothing in the geometry says
    which moved. Moving both to the midpoint would level the pair and put both
    of them off the arrangement they belong to."""
    from formatting_tool.apply.fixers import FIXERS, NEEDS_A_PERSON

    assert "space.mirror_pair_offset" in NEEDS_A_PERSON
    assert "space.mirror_pair_offset" not in FIXERS
    assert "space.row_out_of_line" in FIXERS


# --------------------------------------------------------------------------- #
# And the fix
# --------------------------------------------------------------------------- #

def test_the_odd_one_out_is_put_back_on_the_row(tmp_path: Path) -> None:
    from formatting_tool.apply import apply_fixes
    from formatting_tool.extract import read_deck
    from formatting_tool.extract.master_spec import derive_master_spec
    from pptx import Presentation

    prs, slide, path = _slide(tmp_path, "grid.pptx")
    for row in range(3):
        for column in range(3):
            low = 0.08 if (row, column) == (1, 1) else 0.0
            _box(slide, f"Cell {row}{column}", 1.0 + column * 3.0, 1.0 + row * 2.0 + low)
    prs.save(str(path))

    findings = _of(path, "space.row_out_of_line")
    for issue in findings:
        issue.id = issue.fingerprint()
    out = tmp_path / "fixed.pptx"
    spec = derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())

    result = apply_fixes(
        path, findings, out, selected=[i.id for i in findings], spec=spec
    )

    assert len(result.applied) == 1
    moved = next(
        s for s in Presentation(str(out)).slides[0].shapes if s.name == "Cell 11"
    )
    assert round(moved.top / 914400, 2) == 3.0
