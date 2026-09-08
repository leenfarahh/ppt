"""Overlaps, and the row that has collapsed into itself.

`space.overlap` had no fixer on the grounds that it names two boxes and cannot
know which should move. The geometry cannot -- but z-order can: it records
which of the two landed on the other. The rule reports the finding on the
shape in FRONT, and that is the one that moves, which is the call a designer
makes without thinking about it.

A collapsed row is a different problem wearing the same tag. Five navigation
tabs each overlapping the next is four overlaps and none of them is fixable
one pair at a time: push the second clear of the first and it lands on the
third, which is exactly what the move guard refuses. That is one finding about
the set, and the fix spreads all of them at once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool.models import BrandGuidelines


def _spec():
    from formatting_tool.extract import read_deck
    from formatting_tool.extract.master_spec import derive_master_spec

    return derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())


def _findings(deck: Path, rule_id: str):
    from formatting_tool.extract import read_deck
    from formatting_tool.linemetrics import NullLineMetrics
    from formatting_tool.rules import RuleContext, build_default_rules, run_rules

    context = RuleContext(deck=read_deck(deck), spec=_spec())
    found = [
        i for i in run_rules(context, build_default_rules(metrics=NullLineMetrics()))
        if i.rule_id == rule_id
    ]
    for issue in found:
        issue.id = issue.fingerprint()
    return found


def _slide(tmp_path: Path, name: str):
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    return prs, prs.slides.add_slide(prs.slide_layouts[6]), tmp_path / name


def _text(slide, name, x, y, w, h, words="Section"):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt

    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h)
    )
    shape.name = name
    shape.text_frame.text = words
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(9)
    return shape


def _lefts(path: Path) -> list[float]:
    from pptx import Presentation

    return [
        round(s.left / 914400, 2) for s in Presentation(str(path)).slides[0].shapes
    ]


# --------------------------------------------------------------------------- #
# Which of the two moves
# --------------------------------------------------------------------------- #

def test_the_finding_lands_on_the_shape_in_front(tmp_path: Path) -> None:
    """`slide.shapes` is document order, so the later of the pair is drawn on
    top. That is the one that arrived last and the one that moves."""
    prs, slide, path = _slide(tmp_path, "overlap.pptx")
    _text(slide, "Behind", 1.0, 2.0, 3.0, 1.0)
    _text(slide, "In front", 2.5, 2.0, 3.0, 1.0)
    prs.save(str(path))

    found = _findings(path, "space.overlap")

    assert len(found) == 1
    assert found[0].shape == "In front"
    assert "Behind" in found[0].expected


def test_the_shape_in_front_is_nudged_clear(tmp_path: Path) -> None:
    from formatting_tool.apply import apply_fixes

    prs, slide, path = _slide(tmp_path, "overlap.pptx")
    _text(slide, "Behind", 1.0, 2.0, 3.0, 1.0)
    _text(slide, "In front", 2.5, 2.0, 3.0, 1.0)
    prs.save(str(path))

    found = _findings(path, "space.overlap")
    out = tmp_path / "fixed.pptx"
    result = apply_fixes(path, found, out, selected=[i.id for i in found], spec=_spec())

    assert len(result.applied) == 1
    behind, front = _lefts(out)
    assert behind == 1.0                    # the one underneath did not move
    assert front >= 4.0                     # clear of its right edge


def test_a_shape_with_nowhere_to_go_is_left_alone(tmp_path: Path) -> None:
    from formatting_tool.apply import apply_fixes

    prs, slide, path = _slide(tmp_path, "overlap.pptx")
    _text(slide, "Behind", 0.0, 0.0, 13.333, 7.5, "Full bleed")
    _text(slide, "In front", 0.0, 0.0, 13.333, 7.5, "Also full bleed")
    prs.save(str(path))

    found = _findings(path, "space.overlap")
    result = apply_fixes(
        path, found, tmp_path / "fixed.pptx",
        selected=[i.id for i in found], spec=_spec(),
    )

    assert not result.applied
    assert "runs off the slide" in result.skipped[0].detail


# --------------------------------------------------------------------------- #
# A row that has collapsed
# --------------------------------------------------------------------------- #

def _ribbon(tmp_path: Path, width: float = 2.2, step: float = 1.9):
    prs, slide, path = _slide(tmp_path, "ribbon.pptx")
    for i in range(5):
        _text(slide, f"Tab {i}", 1.0 + i * step, 0.4, width, 0.4, f"Section {i}")
    prs.save(str(path))
    return path


def test_a_collapsed_row_is_one_finding_not_four(tmp_path: Path) -> None:
    """Saying the same thing five times helps nobody, and none of the five can
    be fixed on its own."""
    path = _ribbon(tmp_path)

    assert len(_findings(path, "space.series_crowded")) == 1
    assert _findings(path, "space.overlap") == []


def test_the_row_is_spread_across_the_space_it_has(tmp_path: Path) -> None:
    from formatting_tool.apply import apply_fixes

    path = _ribbon(tmp_path)
    found = _findings(path, "space.series_crowded")
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(path, found, out, selected=[i.id for i in found], spec=_spec())

    assert len(result.applied) == 1
    lefts = _lefts(out)
    gaps = [round(b - a, 2) for a, b in zip(lefts, lefts[1:])]
    assert len(set(gaps)) == 1              # evenly spaced
    assert gaps[0] >= 2.2                   # and no longer overlapping


def test_a_row_that_cannot_fit_says_so(tmp_path: Path) -> None:
    """Spreading shapes wider than the space they have still leaves them
    overlapping, and pretending otherwise would report a fix that did not
    fix anything."""
    from formatting_tool.apply import apply_fixes

    path = _ribbon(tmp_path, width=3.2, step=2.0)
    found = _findings(path, "space.series_crowded")

    result = apply_fixes(
        path, found, tmp_path / "fixed.pptx",
        selected=[i.id for i in found], spec=_spec(),
    )

    assert not result.applied
    assert "wider than the row" in result.skipped[0].detail


def test_a_row_with_room_between_its_members_is_not_crowded(
    tmp_path: Path,
) -> None:
    path = _ribbon(tmp_path, width=1.5, step=2.0)

    assert _findings(path, "space.series_crowded") == []
