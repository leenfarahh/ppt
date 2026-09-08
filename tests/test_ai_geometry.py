"""Geometry the AI layer proposes, and the frame it is checked against.

Refused outright at first, on the grounds that the model is told not to
measure off a rendered image. That was the wrong line to draw. The payload
carries every shape's box in inches, the slide size and the safe margins, all
of them exact; reasoning from those to a position is arithmetic on numbers it
was given, which is what `basis: "geometry"` has always meant.

What makes it safe is the checking, not the trusting: a target has to land
inside the safe margins, and the move then goes through the same overlap and
alignment guards a rule's move does. A slide is a composition whoever proposed
the move.

The other half is the shape id. The payload now carries it and the proposal
must return it, because names repeat within a slide -- sixteen shapes called
"Pentagon 7" is a real deck -- and a fix matched on a name lands on whichever
came first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool.apply import apply_fixes
from formatting_tool.models import (
    GEOMETRIC_OPS,
    BrandGuidelines,
    Category,
    FixAction,
    Issue,
    Severity,
    Source,
)


def _spec():
    from formatting_tool.extract import read_deck
    from formatting_tool.extract.master_spec import derive_master_spec

    return derive_master_spec(read_deck("test_master1.pptx"), BrandGuidelines())


def _ai(op: str, shape: str, shape_id: int, **fix) -> Issue:
    issue = Issue(
        category=Category.SPACE,
        severity=Severity.WARNING,
        message=f"the model's wording about {op}",
        source=Source.AI,
        slide=1,
        shape=shape,
        shape_id=shape_id,
        deck="messy.pptx",
        confidence=0.8,
        fix=FixAction(op=op, shape=shape, shape_id=shape_id, **fix),
    )
    issue.id = issue.fingerprint()
    return issue


def _deck(tmp_path: Path, boxes):
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    made = {}
    for name, (x, y, w, h) in boxes:
        shape = slide.shapes.add_textbox(
            Inches(x), Inches(y), Inches(w), Inches(h)
        )
        shape.name = name
        made[name] = shape
    path = tmp_path / "messy.pptx"
    prs.save(str(path))
    return path, {name: s.shape_id for name, s in made.items()}


def _box_of(path: Path, name: str):
    from pptx import Presentation

    for shape in Presentation(str(path)).slides[0].shapes:
        if shape.name == name:
            return tuple(
                round(v / 914400, 2)
                for v in (shape.left, shape.top, shape.width, shape.height)
            )
    raise AssertionError(f"no shape named {name}")


# --------------------------------------------------------------------------- #
# The ops exist and are geometric
# --------------------------------------------------------------------------- #

def test_move_and_resize_are_the_geometric_ops() -> None:
    assert GEOMETRIC_OPS == {"move", "resize"}


def test_a_move_inside_the_margins_is_applied(tmp_path: Path) -> None:
    deck, ids = _deck(tmp_path, [("Stray", (0.1, 1.0, 3.0, 0.6))])
    issue = _ai("move", "Stray", ids["Stray"], left_in=1.0, top_in=1.2)
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id], spec=_spec())

    assert len(result.applied) == 1
    assert _box_of(out, "Stray")[:2] == (1.0, 1.2)


def test_a_resize_with_room_for_it_is_applied(tmp_path: Path) -> None:
    deck, ids = _deck(tmp_path, [("Panel", (6.0, 3.0, 3.0, 0.6))])
    issue = _ai("resize", "Panel", ids["Panel"], width_in=4.0, height_in=1.0)
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id], spec=_spec())

    assert len(result.applied) == 1
    assert _box_of(out, "Panel")[2:] == (4.0, 1.0)


# --------------------------------------------------------------------------- #
# And what the frame refuses
# --------------------------------------------------------------------------- #

def test_a_target_outside_the_safe_margins_is_refused(tmp_path: Path) -> None:
    """The margins are in the payload the model was given, so a target outside
    them is not a reading it could defend -- and applying one would satisfy
    whatever it was aiming at by breaking the rule the deck is measured by."""
    deck, ids = _deck(tmp_path, [("Stray", (2.0, 2.0, 3.0, 0.6))])
    issue = _ai("move", "Stray", ids["Stray"], left_in=0.0, top_in=0.1)

    result = apply_fixes(
        deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id], spec=_spec()
    )

    assert not result.applied
    assert "outside the safe margins" in result.skipped[0].detail


def test_a_box_grown_past_the_frame_is_refused(tmp_path: Path) -> None:
    deck, ids = _deck(tmp_path, [("Panel", (6.0, 3.0, 3.0, 0.6))])
    issue = _ai("resize", "Panel", ids["Panel"], width_in=9.0, height_in=1.0)

    result = apply_fixes(
        deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id], spec=_spec()
    )

    assert not result.applied
    assert "runs past the safe margins" in result.skipped[0].detail


def test_a_box_too_small_to_hold_anything_is_refused(tmp_path: Path) -> None:
    deck, ids = _deck(tmp_path, [("Panel", (6.0, 3.0, 3.0, 0.6))])
    issue = _ai("resize", "Panel", ids["Panel"], width_in=0.01, height_in=0.01)

    result = apply_fixes(
        deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id], spec=_spec()
    )

    assert not result.applied
    assert "too small to hold anything" in result.skipped[0].detail


def test_without_a_master_no_geometry_is_applied(tmp_path: Path) -> None:
    """There is no frame to check against, so there is nothing that makes the
    proposal safe."""
    deck, ids = _deck(tmp_path, [("Stray", (0.1, 1.0, 3.0, 0.6))])
    issue = _ai("move", "Stray", ids["Stray"], left_in=1.0, top_in=1.2)

    result = apply_fixes(deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id])

    assert not result.applied
    assert "no brand reference" in result.skipped[0].detail


def test_a_proposed_move_still_goes_through_the_overlap_guard(
    tmp_path: Path,
) -> None:
    """A slide is a composition whoever proposed the move."""
    deck, ids = _deck(
        tmp_path,
        [("Stray", (1.0, 1.0, 3.0, 0.6)), ("Occupied", (6.0, 3.0, 3.0, 0.6))],
    )
    issue = _ai("move", "Stray", ids["Stray"], left_in=6.0, top_in=3.0)

    result = apply_fixes(
        deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id], spec=_spec()
    )

    assert not result.applied
    assert "further over" in result.skipped[0].detail


def test_the_shape_is_found_by_id_when_two_share_a_name(tmp_path: Path) -> None:
    """The reason the id is in the payload at all. Matched on the name, this
    lands on whichever came first."""
    deck, ids = _deck(
        tmp_path,
        # Not aligned with each other, so the alignment guard is not what is
        # being tested here.
        [("Pentagon 7", (1.0, 1.0, 2.0, 0.6)), ("Pentagon 7 ", (7.3, 5.0, 2.0, 0.6))],
    )
    second = ids["Pentagon 7 "]
    issue = _ai("move", "Pentagon 7 ", second, left_in=4.0, top_in=5.0)
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id], spec=_spec())

    assert len(result.applied) == 1
    assert _box_of(out, "Pentagon 7")[:2] == (1.0, 1.0)     # the first, untouched
    assert _box_of(out, "Pentagon 7 ")[:2] == (4.0, 5.0)
