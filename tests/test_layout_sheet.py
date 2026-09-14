"""Rendering the master's layouts so the model can look at them.

The sheet is an improvement to the question, not a part of the answer: every
failure along the way has to leave the caller asking what it asked before, from
the written inventory alone. These check that it does, and that what it
produces is keyed back to the layouts it shows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool.ai.layoutsheet import LayoutSheet, _compose, build_sheet
from formatting_tool.models import Geometry, LayoutProfile, ShapeProfile
from formatting_tool.render import NullRenderer


def _layout(name, index):
    return LayoutProfile(name=name, index=index, shapes=[
        ShapeProfile(
            shape_id=index, name="Title", shape_type="PLACEHOLDER (14)",
            geometry=Geometry(left_in=0.7, top_in=0.7, width_in=11.0, height_in=0.6),
            placeholder_type="TITLE (1)",
        ),
    ])


def test_no_layouts_is_no_sheet() -> None:
    assert build_sheet(Path("master.pptx"), []) is None


def test_no_renderer_is_no_sheet_and_no_exception(tmp_path: Path) -> None:
    """The case most machines are in. Without a renderer there is nothing to
    show, and the caller has to be left asking the question it asked before
    rather than handed an error."""
    master = tmp_path / "master.pptx"
    master.write_bytes(b"not really a deck")

    assert build_sheet(master, [_layout("Title with Content 01", 0)],
                       directory=tmp_path, renderer=NullRenderer()) is None


def test_a_master_that_will_not_open_is_no_sheet(tmp_path: Path) -> None:
    master = tmp_path / "master.pptx"
    master.write_bytes(b"not really a deck")

    assert build_sheet(master, [_layout("Title with Content 01", 0)],
                       directory=tmp_path) is None


def test_every_tile_is_numbered_in_the_order_it_was_given(tmp_path: Path) -> None:
    """The numbers are how the picture and the written list are put together,
    so they have to be the master's own order and nothing else."""
    Image = pytest.importorskip("PIL.Image")

    tiles = []
    for name in ("Title Slide", "Agenda", "Section Divider"):
        path = tmp_path / f"{name}.png"
        Image.new("RGB", (64, 36), (200, 200, 200)).save(path)
        tiles.append((name, path))

    sheet = _compose(tmp_path / "sheet.png", tiles)

    assert sheet is not None
    assert sheet.path.exists()
    assert sheet.numbers == {"Title Slide": 1, "Agenda": 2, "Section Divider": 3}
    assert sheet.number_of("Agenda") == 2
    assert sheet.number_of("not a layout") is None


def test_a_tile_that_will_not_open_does_not_lose_the_sheet(tmp_path: Path) -> None:
    """Thirteen tiles beats none. One unreadable render is a gap in the sheet,
    not a reason to stop showing the model the master."""
    Image = pytest.importorskip("PIL.Image")

    good = tmp_path / "good.png"
    Image.new("RGB", (64, 36), (200, 200, 200)).save(good)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png")

    sheet = _compose(tmp_path / "sheet.png", [("Good", good), ("Broken", broken)])

    assert sheet is not None
    assert sheet.numbers["Good"] == 1


def test_cleanup_is_safe_to_call_on_a_sheet_that_owns_nothing() -> None:
    LayoutSheet(path=Path("nowhere.png")).cleanup()
