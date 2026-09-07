"""Which master layout a slide goes on, when a model has looked at it.

The structural matcher counts the content regions a slide uses against the
regions a layout offers. On a tidy deck that is right; on a messy one the count
is of loose text boxes, because that is what makes a deck messy. Measured on a
real deck against a five-layout master, it read a table of contents as a
four-region comparison layout and a two-column slide as the same, where the
model looking at the render got both right.

So a model's reading is allowed to outrank the structural fit. It is not
allowed to outrank two files agreeing on a layout name, and it cannot name a
layout the master does not have.
"""

from __future__ import annotations

from formatting_tool.ai.schema import layout_choices_from_response
from formatting_tool.models import (
    DeckProfile,
    Geometry,
    LayoutChoice,
    LayoutProfile,
    ShapeProfile,
    SlideProfile,
)
from formatting_tool.rebuild.matcher import choose_layout

FLOOR = 0.5


def _ph(name, token, left, top, width, height):
    return ShapeProfile(
        shape_id=abs(hash(name)) % 9999,
        name=name,
        shape_type="PLACEHOLDER (14)",
        geometry=Geometry(left_in=left, top_in=top, width_in=width, height_in=height),
        placeholder_type=token,
    )


def _layouts():
    return [
        LayoutProfile(name="Cover", index=0, shapes=[
            _ph("Title", "CENTER_TITLE (3)", 1.6, 1.2, 10.0, 2.6),
            _ph("Subtitle", "SUBTITLE (4)", 1.6, 3.9, 10.0, 1.8),
        ]),
        LayoutProfile(name="title_content", index=1, shapes=[
            _ph("Title", "TITLE (1)", 0.9, 0.4, 11.5, 1.4),
            _ph("Body", "OBJECT (7)", 0.9, 2.0, 11.5, 4.8),
        ]),
        LayoutProfile(name="title_comparison", index=2, shapes=[
            _ph("Title", "TITLE (1)", 0.9, 0.4, 11.5, 1.4),
            _ph("A", "BODY (2)", 0.9, 1.8, 5.6, 0.9),
            _ph("B", "OBJECT (7)", 0.9, 2.7, 5.6, 4.0),
            _ph("C", "BODY (2)", 6.8, 1.8, 5.6, 0.9),
            _ph("D", "OBJECT (7)", 6.8, 2.7, 5.6, 4.0),
        ]),
    ]


def _slide(layout_name=None, boxes=4):
    """A messy slide: content in loose text boxes, which is what defeats the
    structural count."""
    shapes = [
        ShapeProfile(
            shape_id=100 + n, name=f"TextBox {n}", shape_type="TEXT_BOX (17)",
            geometry=Geometry(left_in=0.9, top_in=2.0 + n * 0.8,
                              width_in=5.0, height_in=0.6),
            text="copy",
        )
        for n in range(boxes)
    ]
    return SlideProfile(number=1, layout_name=layout_name, shapes=shapes)


def _deck(slide):
    return DeckProfile(path="messy.pptx", width_in=13.333, height_in=7.5,
                       slides=[slide])


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #

def test_a_model_pick_outranks_the_structural_fit() -> None:
    """The case the render exists for: four loose boxes read structurally as a
    four-region comparison, and the picture says it is one column."""
    slide = _slide()
    structural = choose_layout(slide, _layouts(), FLOOR, deck=_deck(slide))

    seen = LayoutChoice(slide=1, layout="title_content", confidence=0.95,
                        why="a title and one column of copy")
    picked = choose_layout(slide, _layouts(), FLOOR, deck=_deck(slide), seen=seen)

    assert structural.name != "title_content"       # what it would have chosen
    assert picked.name == "title_content"
    assert "read off the rendered slide" in picked.basis
    assert picked.confident


def test_a_layout_name_both_files_share_still_wins() -> None:
    """A designer naming a layout the same in both files is that designer's own
    statement, and it outranks any reading of a picture."""
    slide = _slide(layout_name="title_comparison")
    seen = LayoutChoice(slide=1, layout="Cover", confidence=1.0, why="a cover")

    picked = choose_layout(slide, _layouts(), FLOOR, deck=_deck(slide), seen=seen)

    assert picked.name == "title_comparison"
    assert "matches the master" in picked.basis


def test_a_hesitant_pick_is_left_to_the_structure() -> None:
    """A low confidence is the model saying the master offers nothing that
    fits. That is worth reporting, not worth acting on."""
    slide = _slide()
    seen = LayoutChoice(slide=1, layout="Cover", confidence=0.2,
                        why="nothing here is a cover, but it is the closest")

    picked = choose_layout(slide, _layouts(), FLOOR, deck=_deck(slide), seen=seen)

    # On the basis, not on the name: the fallback is free to arrive at the same
    # layout by its own reasoning, and asserting on the name would pass or fail
    # on that coincidence rather than on whether the pick was used.
    assert "read off the rendered slide" not in picked.basis


def test_a_pick_naming_no_real_layout_is_ignored() -> None:
    """Belt as well as braces: the parser drops these, and the matcher would
    too. A pick the master cannot honour is the model having invented a
    layout, and acting on it would put the slide somewhere nobody chose."""
    slide = _slide()
    seen = LayoutChoice(slide=1, layout="Section Divider", confidence=1.0, why="x")

    picked = choose_layout(slide, _layouts(), FLOOR, deck=_deck(slide), seen=seen)

    assert picked.name in {layout.name for layout in _layouts()}
    assert "read off the rendered slide" not in picked.basis


# --------------------------------------------------------------------------- #
# Parsing the model's answer
# --------------------------------------------------------------------------- #

def test_an_invented_layout_name_is_dropped_not_corrected() -> None:
    """Not snapped to the nearest real name: a pick the master cannot honour is
    not a near miss, and correcting it would invent a decision."""
    parsed = layout_choices_from_response(
        {"layout_choices": [
            {"slide": 1, "layout": "title_contents", "confidence": 0.9, "why": "x"},
            {"slide": 2, "layout": "Cover", "confidence": 0.8, "why": "a cover"},
        ]},
        {"Cover", "title_content"},
    )

    assert [(c.slide, c.layout) for c in parsed] == [(2, "Cover")]


def test_a_pick_with_no_slide_number_is_dropped() -> None:
    parsed = layout_choices_from_response(
        {"layout_choices": [{"slide": None, "layout": "Cover",
                             "confidence": 1.0, "why": "x"}]},
        {"Cover"},
    )

    assert parsed == []


def test_no_layout_choices_at_all_is_fine() -> None:
    """The field is optional in practice: an older response, or a batch with no
    render attached, and the structural matcher decides alone."""
    assert layout_choices_from_response({}, {"Cover"}) == []
