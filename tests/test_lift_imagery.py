"""Graphics that land on the new layout's artwork.

A deck's two-track slide carried an icon above each column, sitting on the edge
of the arc its OLD layout drew. The master's arc is bigger, so on the rebuilt
slide both icons sat well inside it: dark line art on a dark disk, still there
and impossible to see. The geometry was right and the layout choice was right.
The slide was simply drawn against a different arc.

What makes this decidable is not that a shape is on the artwork -- that layout
puts its own title and body regions inside the same arc on purpose -- but that
these graphics belong to something. Each is centred over a column of copy, and
the place it belongs is directly above that copy.
"""

from __future__ import annotations

from formatting_tool.rebuild.builder import lift_imagery

EMU = 914400
CANVAS = (13.333, 7.5)


class Box:
    def __init__(self, left, top, width, height, text="", kind="TEXT_BOX (17)",
                 placeholder=False):
        self.left = int(left * EMU)
        self.top = int(top * EMU)
        self.width = int(width * EMU)
        self.height = int(height * EMU)
        self._text = text
        self.shape_type = kind
        self.is_placeholder = placeholder
        self.name = text or kind

    @property
    def has_text_frame(self):
        return True

    @property
    def text_frame(self):
        return self

    @property
    def text(self):
        return self._text

    def top_in(self):
        return round(self.top / EMU, 2)


class Slide:
    def __init__(self, shapes, layout_shapes):
        self.shapes = shapes
        self.slide_layout = type("L", (), {"shapes": layout_shapes})()


def _arc():
    """The master's arc: a band across the top 44% of the page, and the
    photograph inside it, which is drawn far off the top edge."""
    return [
        Box(0.0, 0.0, 13.33, 3.30, kind="FREEFORM (5)"),
        Box(5.01, -5.01, 3.30, 13.33, kind="PICTURE (13)"),
        Box(0.71, 7.02, 11.92, 0.0, kind="LINE (9)"),
    ]


def _two_track():
    """Two icons on the arc, each an outline ring with a glyph inside it, over
    two columns of copy."""
    icons = [
        Box(3.27, 2.10, 0.67, 0.67, kind="GROUP (6)"),
        Box(3.40, 2.23, 0.42, 0.41, kind="PICTURE (13)"),
        Box(9.40, 2.10, 0.67, 0.67, kind="GROUP (6)"),
        Box(9.56, 2.27, 0.34, 0.34, kind="PICTURE (13)"),
    ]
    copy = [
        Box(0.71, 4.12, 5.79, 0.37, "Strategic Foundations Track"),
        Box(0.71, 4.70, 5.79, 2.09, "Standardized presentation templates"),
        Box(6.84, 4.12, 5.79, 0.37, "Tactical Live Delivery Track"),
        Box(6.84, 4.70, 5.79, 2.09, "Board deck transformation"),
    ]
    return icons + copy, icons


def test_an_icon_on_the_artwork_is_lifted_onto_the_copy_it_labels() -> None:
    shapes, icons = _two_track()
    slide = Slide(shapes, _arc())

    moved = lift_imagery(slide, CANVAS)

    assert len(moved) == 4                       # both rings and both glyphs
    # Clear of the arc, and just above the heading each one sits over.
    for icon in icons:
        assert icon.top_in() >= 3.30
    assert max(icon.top / EMU + icon.height / EMU for icon in icons) <= 4.12


def test_a_ring_and_its_glyph_move_together() -> None:
    """One icon is often several shapes. Lifting one and not the other would
    take the icon apart."""
    shapes, icons = _two_track()
    before = [icon.top for icon in icons]
    slide = Slide(shapes, _arc())

    lift_imagery(slide, CANVAS)

    ring, glyph = icons[0], icons[1]
    assert ring.top - before[0] == glyph.top - before[1]


def test_a_graphic_with_nothing_under_it_is_left_alone() -> None:
    """Nothing to say where it should go instead."""
    icon = Box(3.27, 2.10, 0.67, 0.67, kind="GROUP (6)")
    slide = Slide([icon], _arc())

    assert lift_imagery(slide, CANVAS) == []
    assert icon.top_in() == 2.10


def test_a_graphic_that_is_not_over_its_copy_is_left_alone() -> None:
    """Centred over the column is what says the two belong together."""
    icon = Box(0.2, 2.10, 0.67, 0.67, kind="GROUP (6)")
    copy = Box(6.84, 4.12, 5.79, 0.37, "somewhere else entirely")
    slide = Slide([icon, copy], _arc())

    assert lift_imagery(slide, CANVAS) == []


def test_a_photograph_is_never_lifted() -> None:
    """Moving a photograph is not this function's business. An icon is under a
    percent of the page; a photograph is a third of it."""
    photo = Box(1.0, 1.0, 6.0, 3.0, kind="PICTURE (13)")
    copy = Box(0.71, 4.70, 5.79, 0.37, "a caption under it")
    slide = Slide([photo, copy], _arc())

    assert lift_imagery(slide, CANVAS) == []
    assert photo.top_in() == 1.0


def test_thin_furniture_is_not_artwork_worth_moving_off() -> None:
    """A hairline rule and a small logo are what a plain layout carries, and
    an icon resting on one is not sitting on the layout."""
    plain = [
        Box(0.71, 7.02, 11.92, 0.0, kind="LINE (9)"),
        Box(11.96, 0.70, 0.67, 0.56, kind="PICTURE (13)"),
    ]
    icon = Box(11.9, 0.6, 0.67, 0.67, kind="GROUP (6)")
    copy = Box(8.0, 4.12, 5.0, 0.37, "a heading below it")
    slide = Slide([icon, copy], plain)

    assert lift_imagery(slide, CANVAS) == []


def test_a_graphic_already_clear_of_its_copy_is_left_alone() -> None:
    """Only ever lifted upward onto the copy, never dragged down onto it."""
    icon = Box(3.27, 3.40, 0.67, 0.67, kind="GROUP (6)")
    copy = Box(0.71, 3.50, 5.79, 0.37, "a heading it already overlaps")
    slide = Slide([icon, copy], _arc())

    assert lift_imagery(slide, CANVAS) == []


def test_a_canvas_of_nothing_is_not_an_error() -> None:
    shapes, _icons = _two_track()
    assert lift_imagery(Slide(shapes, _arc()), (0.0, 0.0)) == []
