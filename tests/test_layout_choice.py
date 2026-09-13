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
    ParagraphProfile,
    RunProfile,
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


def test_a_shared_layout_name_no_longer_decides_on_its_own() -> None:
    """The name used to settle this outright, and it is now the weakest of the
    three signals -- below a reading of the render, and below the measurements.

    Evidence beats labels. A name survives everything: a deck rebuilt onto one
    master and handed another carries the old names, a template renamed around
    its slides carries names describing what a layout used to be, and an author
    duplicating "Title with Content 03" to make something else keeps the name.
    In each case the name is a label and the structure is the evidence, and
    this was trusting the label.
    """
    slide = _slide(layout_name="title_comparison")
    seen = LayoutChoice(slide=1, layout="Cover", confidence=1.0, why="a cover")

    picked = choose_layout(slide, _layouts(), FLOOR, deck=_deck(slide), seen=seen)

    assert picked.name == "Cover"                  # what was looked at
    assert "read off the rendered slide" in picked.basis


def _twins():
    """Two layouts a master really does carry: the same regions in the same
    places, drawn apart for something the file does not record -- a darker
    background, a rule, a different set of furniture."""
    return [
        LayoutProfile(name="Title with Content 01", index=0, shapes=[
            _ph("Title", "TITLE (1)", 0.9, 0.4, 11.5, 1.4),
            _ph("Body", "BODY (2)", 0.9, 2.0, 11.5, 4.8),
        ]),
        LayoutProfile(name="Title with Content 02", index=1, shapes=[
            _ph("Title", "TITLE (1)", 0.9, 0.4, 11.5, 1.4),
            _ph("Body", "BODY (2)", 0.9, 2.0, 11.5, 4.8),
        ]),
    ]


def _body_slide(layout_name=None, number=3):
    """A body slide with real copy in it, so it classifies as content rather
    than falling to the heuristics for a slide with nothing on it."""
    shapes = [
        ShapeProfile(
            shape_id=200 + n, name=f"TextBox {n}", shape_type="TEXT_BOX (17)",
            geometry=Geometry(left_in=0.9, top_in=2.0 + n * 0.9,
                              width_in=11.0, height_in=0.8),
            text="body copy " * 12,
            paragraphs=[ParagraphProfile(
                text="body copy " * 12,
                runs=[RunProfile(text="body copy " * 12)],
            )],
        )
        for n in range(3)
    ]
    return SlideProfile(number=number, layout_name=layout_name, shapes=shapes)


def test_the_name_settles_a_tie_the_measurements_cannot() -> None:
    """Where the name still counts, and it is not a small case: a master with
    seven near-identical content layouts is normal, and the structure cannot
    separate them at all. Without the name the tie goes to whichever sits
    earlier in the master, which is how every content slide in a real deck
    ended up on the same layout."""
    slide = _body_slide(layout_name="Title with Content 02")

    picked = choose_layout(slide, _twins(), FLOOR, deck=_deck(slide))

    assert picked.name == "Title with Content 02"
    assert "names" in picked.basis


def test_a_tie_with_no_name_to_settle_it_still_picks_one() -> None:
    picked = choose_layout(
        _body_slide(), _twins(), FLOOR, deck=_deck(_body_slide())
    )

    assert picked.name == "Title with Content 01"       # the earlier one


def test_a_name_that_does_not_fit_is_reported_and_not_followed() -> None:
    """The disagreement is worth a designer's eye either way, so it is said out
    loud rather than resolved silently."""
    # Slide 1 reads as a cover whatever its boxes say, and it names a content
    # layout. The classification decided; the name is reported, not followed.
    slide = _slide(layout_name="title_content")

    picked = choose_layout(slide, _layouts(), FLOOR, deck=_deck(slide))

    assert picked.name == "Cover"
    assert "title_content" in picked.basis and "fits less well" in picked.basis


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


# --------------------------------------------------------------------------- #
# Where the content sits, when counting cannot tell two layouts apart
# --------------------------------------------------------------------------- #

def _image_layouts():
    """A master's two image layouts, alike but mirrored -- which is how a real
    one is drawn: `Content with Image 01` puts its picture left and `02` puts
    it right, and they differ in nothing else."""
    return [
        LayoutProfile(name="Content with Image 01", index=0, shapes=[
            _ph("Title", "TITLE (1)", 6.9, 0.6, 5.6, 1.2),
            _ph("Body", "BODY (2)", 6.9, 2.0, 5.6, 4.6),
            _ph("Picture", "PICTURE (18)", 0.0, 0.0, 6.5, 7.5),
        ]),
        LayoutProfile(name="Content with Image 02", index=1, shapes=[
            _ph("Title", "TITLE (1)", 0.8, 0.6, 5.6, 1.2),
            _ph("Body", "BODY (2)", 0.8, 2.0, 5.6, 4.6),
            _ph("Picture", "PICTURE (18)", 6.8, 0.0, 6.5, 7.5),
        ]),
    ]


def _picture(name, left, width=6.5):
    shape = ShapeProfile(
        shape_id=abs(hash(name)) % 9999,
        name=name,
        shape_type="PICTURE (13)",
        geometry=Geometry(left_in=left, top_in=0.0, width_in=width, height_in=7.5),
    )
    shape.is_picture = True
    return shape


def _slide_with_image_on(side: str) -> SlideProfile:
    """A slide the count cannot place: a photograph down one half and a column
    of loose text boxes down the other, which is what a messy deck looks like."""
    picture_left = 0.0 if side == "left" else 6.8
    copy_left = 6.9 if side == "left" else 0.8
    shapes = [_picture("Photo", picture_left)]
    for index in range(6):
        box = ShapeProfile(
            shape_id=100 + index,
            name=f"TextBox {index}",
            shape_type="TEXT_BOX (17)",
            geometry=Geometry(
                left_in=copy_left, top_in=1.0 + index * 0.9,
                width_in=5.6, height_in=0.8,
            ),
            text="copy",
        )
        box.paragraphs = [ParagraphProfile(text="copy")]
        shapes.append(box)
    return SlideProfile(number=1, layout_name="Title Only", shapes=shapes)


def _best(slide, layouts):
    from formatting_tool.rebuild.matcher import structure_score

    return max(
        ((structure_score(slide, layout), layout.name) for layout in layouts),
    )[1]


def test_the_layout_with_the_image_on_the_same_side_wins() -> None:
    """Counting regions cannot separate these two: both offer a title, a body
    and a picture, and the slide asks for the same three however many loose
    boxes its copy is in. Before there was anything else to go on, the tie went
    to whichever layout came first in the master -- so every slide in a deck
    got the same one, image on the left or not."""
    layouts = _image_layouts()

    assert _best(_slide_with_image_on("left"), layouts) == "Content with Image 01"
    assert _best(_slide_with_image_on("right"), layouts) == "Content with Image 02"


def test_a_layout_that_cannot_hold_the_content_still_loses() -> None:
    """Position is the tie-break, not the decision. A layout with nowhere to
    put the picture is wrong wherever its regions sit."""
    from formatting_tool.rebuild.matcher import structure_score

    slide = _slide_with_image_on("left")
    text_only = LayoutProfile(name="Title with Content", index=2, shapes=[
        _ph("Title", "TITLE (1)", 0.9, 0.4, 11.5, 1.4),
        _ph("Body", "BODY (2)", 0.9, 2.0, 11.5, 4.8),
    ])

    assert structure_score(slide, _image_layouts()[0]) > structure_score(slide, text_only)
