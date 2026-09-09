"""A row of repeated shapes spaced unevenly.

Off a real deck: four column headings 0.783, 0.743 and 0.761in apart. Nothing
overlaps and nothing is out of line, so `space.series_crowded` and
`space.row_out_of_line` both pass it, and the row still reads as a rhythm that
stumbles.

It is the same finding and the same remedy as a collapsed row, one step
earlier, so it borrows that rule's machinery whole -- the series grouping, the
row test, the even-gap arithmetic and its fixer -- and differs only in the
trigger. Collapsed is an error because it is broken; uneven is a warning
because it is untidy.

WHAT IT DELIBERATELY DOES NOT DO, and why. It compares gaps of one kind
against each other, never a horizontal gutter against a vertical one. Measured
on a real slide, a component had every horizontal gutter at 0.058in and every
vertical one at 0.097in -- identical to the thousandth within each kind and
different between them. Nothing there drifts; the two values are a decision,
and tighter horizontal than vertical spacing is an ordinary typographic
choice, so this rule never has an opinion about it.

`space.matrix_gutter` covers the narrow case where those two numbers are so
close that they read as one that drifted, and it earns that by measuring how
far apart they usually are. See `tests/test_matrix_gutter.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool.extract import read_deck
from formatting_tool.extract.master_spec import derive_master_spec
from formatting_tool.models import BrandGuidelines
from formatting_tool.rules import RuleContext
from formatting_tool.rules.space import UnevenSeriesRule


def _row(tmp_path: Path, lefts, *, width: float = 2.0, top: float = 2.0,
         widths=None) -> Path:
    """A row of same-sized boxes at the given left edges."""
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for index, left in enumerate(lefts):
        shape = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(left), Inches(top),
            Inches((widths or [width] * len(lefts))[index]), Inches(0.6),
        )
        shape.name = f"Tab {index + 1}"
        shape.text_frame.text = f"Tab {index + 1}"
    path = tmp_path / "row.pptx"
    prs.save(str(path))
    return path


def _found(deck: Path) -> list:
    spec = derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())
    issues = list(UnevenSeriesRule().check(
        RuleContext(deck=read_deck(deck), spec=spec)))
    for issue in issues:
        issue.id = issue.fingerprint()
    return issues


def _gaps(path: Path) -> list[float]:
    from pptx import Presentation

    boxes = sorted(Presentation(str(path)).slides[0].shapes, key=lambda s: s.left)
    return [
        round((b.left - (a.left + a.width)) / 914400, 3)
        for a, b in zip(boxes, boxes[1:])
    ]


# --------------------------------------------------------------------------- #
# The finding
# --------------------------------------------------------------------------- #

def test_an_uneven_row_is_reported(tmp_path: Path) -> None:
    """Gaps of 0.30, 0.20 and 0.25in across four tabs."""
    deck = _row(tmp_path, [0.5, 2.8, 5.0, 7.25])

    found = _found(deck)

    assert len(found) == 1
    assert found[0].shape == "Tab 1"        # the leftmost, which will not move
    assert "spaced unevenly" in found[0].message
    assert "evenly spaced across" in found[0].expected


def test_an_even_row_says_nothing(tmp_path: Path) -> None:
    deck = _row(tmp_path, [0.5, 2.75, 5.0, 7.25])

    assert _found(deck) == []


def test_a_hair_of_disagreement_says_nothing(tmp_path: Path) -> None:
    """0.01in across a row is rounding, not a rhythm anybody can hear."""
    deck = _row(tmp_path, [0.5, 2.75, 5.01, 7.26])

    assert _found(deck) == []


def test_a_collapsed_row_is_left_to_the_other_rule(tmp_path: Path) -> None:
    """Overlapping is `space.series_crowded`'s finding, and it is an error
    where this is a warning. Saying both would be saying it twice."""
    deck = _row(tmp_path, [0.5, 2.0, 3.2, 5.0])      # 2.0 overlaps 0.5+2.0

    assert _found(deck) == []


def test_two_groups_side_by_side_are_not_one_uneven_row(tmp_path: Path) -> None:
    """The false positive off a real slide.

    Four boxes with gaps 0.06, 0.41, 0.06 are two components side by side, not
    one uneven row -- and the fixer spreads everything it finds in the row, so
    a series with a boundary gap in it would be half-corrected rather than
    left alone.
    """
    deck = _row(tmp_path, [0.5, 2.56, 4.97, 7.03], width=2.0)

    assert _found(deck) == []


def test_a_row_of_two_is_not_enough(tmp_path: Path) -> None:
    """Two shapes have one gap, and one gap cannot disagree with itself."""
    deck = _row(tmp_path, [0.5, 2.8])

    assert _found(deck) == []


# --------------------------------------------------------------------------- #
# Applying it
# --------------------------------------------------------------------------- #

def test_the_row_is_distributed_across_the_span_it_occupies(
    tmp_path: Path,
) -> None:
    """The ends stay put, so the row's footprint does not change and the fix
    cannot land on anything outside it."""
    from pptx import Presentation

    from formatting_tool.apply import apply_fixes

    deck = _row(tmp_path, [0.5, 2.8, 5.0, 7.25])
    was = [s.left for s in sorted(Presentation(str(deck)).slides[0].shapes,
                                  key=lambda s: s.left)]
    found = _found(deck)
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, found, out, selected=[i.id for i in found])

    assert len(result.applied) == 1
    gaps = _gaps(out)
    assert max(gaps) - min(gaps) <= 0.002, f"still uneven: {gaps}"
    now = [s.left for s in sorted(Presentation(str(out)).slides[0].shapes,
                                  key=lambda s: s.left)]
    assert now[0] == was[0] and now[-1] == was[-1], "an end moved"


def test_every_member_moves_not_just_the_exactly_sized_ones(
    tmp_path: Path,
) -> None:
    """The bug the real deck exposed, and it was in the older fixer too.

    Four column headings measured 2164854, 2164854, 2164855 and 2164855 EMU
    wide -- one EMU apart, a millionth of an inch. The series rules bucket on
    sizes rounded to two decimals, so the rule reported four shapes and named
    an even gap for four; `_row_with` matched width EXACTLY and returned two.
    The fixer spread those two and the row came out even by luck, because the
    pair it never touched happened to already sit right relative to each
    other. On another deck that is a half-corrected row reported as fixed.
    """
    from formatting_tool.apply import apply_fixes

    one_emu = 1 / 914400
    deck = _row(
        tmp_path, [0.5, 2.8, 5.0, 7.25],
        widths=[2.0, 2.0, 2.0 + one_emu, 2.0 + one_emu],
    )
    found = _found(deck)
    assert len(found) == 1
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, found, out, selected=[i.id for i in found])

    assert "spread 4 shapes" in result.applied[0].detail
    gaps = _gaps(out)
    assert max(gaps) - min(gaps) <= 0.002, f"still uneven: {gaps}"


def test_applying_it_twice_changes_nothing_the_second_time(
    tmp_path: Path,
) -> None:
    from formatting_tool.apply import apply_fixes

    deck = _row(tmp_path, [0.5, 2.8, 5.0, 7.25])
    found = _found(deck)
    out = tmp_path / "fixed.pptx"
    apply_fixes(deck, found, out, selected=[i.id for i in found])

    assert _found(out) == []


# --------------------------------------------------------------------------- #
# The line it does not cross
# --------------------------------------------------------------------------- #

def test_it_never_compares_a_horizontal_gutter_to_a_vertical_one() -> None:
    """The measurement that settled the design.

    On a real slide every horizontal gutter was 0.058in and every vertical one
    0.097in -- consistent within each kind, different between them. A rule
    that equalised the two on sight would restyle every component in the
    library, so this one only ever compares gaps of the same kind and reads
    one row at a time. Comparing the two kinds is `space.matrix_gutter`'s,
    and only where they are within a twentieth of an inch of each other.
    """
    import inspect

    from formatting_tool.rules.space import UnevenSeriesRule

    source = inspect.getsource(UnevenSeriesRule)
    # It reads left edges and widths only: nothing vertical enters the
    # comparison beyond the check that the members share a row.
    assert "_gaps_of" in source
    assert "height_in" not in source.split("def _uneven")[1]
