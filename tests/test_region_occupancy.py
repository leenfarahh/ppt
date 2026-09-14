"""Filling a layout region the slide is already drawing in.

Two guards, both found on one rebuilt slide. Its three wave columns each ended
with a "Timing / Total Headcount" box at the foot of the page; the layout it
moved to offers three content regions near the top. The regions held no copy,
so they were taken as free, and the three boxes were filled into them -- on top
of the wave headings and the lists already there. Nothing was duplicated and
nothing was lost. Three blocks of copy were moved onto three others.

Then, with that fixed, the first wave heading alone was pulled into a region
while the second and third stayed where they were, and a row of three became a
row of one and two.
"""

from __future__ import annotations

from formatting_tool.rebuild.builder import _has_row_peers, _unoccupied

EMU = 914400


class Box:
    def __init__(self, left, top, width, height, text="", placeholder=False):
        self.left = int(left * EMU)
        self.top = int(top * EMU)
        self.width = int(width * EMU)
        self.height = int(height * EMU)
        self._text = text
        self.is_placeholder = placeholder
        self.shape_type = "TEXT_BOX (17)"
        self.name = text or "shape"

    @property
    def has_text_frame(self):
        return True

    @property
    def text_frame(self):
        return self

    @property
    def text(self):
        return self._text


# --------------------------------------------------------------------------- #
# Empty is not the same as free
# --------------------------------------------------------------------------- #

def test_a_region_the_slide_already_draws_in_is_not_free() -> None:
    """The real one: a content region near the top of the layout, with the
    slide's own heading and list already sitting across it."""
    region = Box(0.69, 1.59, 3.86, 4.0, placeholder=True)
    heading = Box(1.15, 1.27, 3.14, 0.50, "Wave 1")
    listing = Box(0.84, 2.17, 3.78, 3.00, "Human Capital (13)")

    assert _unoccupied([region], [heading, listing], []) == []


def test_a_region_with_nothing_in_it_is_free() -> None:
    region = Box(0.69, 1.59, 3.86, 4.0, placeholder=True)
    elsewhere = Box(0.84, 6.40, 3.78, 0.50, "a footnote")

    assert _unoccupied([region], [elsewhere], []) == [region]


def test_the_shapes_on_their_way_in_do_not_count_against_it() -> None:
    """A run moving into a region cannot be the reason the region is
    unavailable, and the box drawn directly over one is the whole case
    `claim_drawn_over` exists for."""
    region = Box(0.69, 1.59, 3.86, 0.5, placeholder=True)
    moving = Box(0.67, 1.60, 3.86, 0.5, "the copy that belongs there")

    assert _unoccupied([region], [moving], [moving]) == [region]


def test_a_clipped_corner_is_not_occupation() -> None:
    """Well above the incidental overlap of a neighbouring box."""
    region = Box(0.69, 1.59, 3.86, 4.0, placeholder=True)
    nudging = Box(4.40, 1.50, 1.00, 0.30, "just touching")

    assert _unoccupied([region], [nudging], []) == [region]


# --------------------------------------------------------------------------- #
# One of a row is not a lone box
# --------------------------------------------------------------------------- #

def test_a_heading_with_company_on_its_line_has_peers() -> None:
    """Measured on the top edge, because a row of headings shares that even
    when their widths differ -- and here they are 3.14, 2.55 and 2.77in, which
    no size-based grouping will ever call a series."""
    row = [
        Box(1.15, 1.27, 3.14, 0.50, "Wave 1"),
        Box(5.76, 1.27, 2.55, 0.50, "Wave 2"),
        Box(9.73, 1.27, 2.77, 0.50, "Wave 3"),
    ]

    assert all(_has_row_peers(shape, row) for shape in row)


def test_a_box_on_its_own_line_has_none() -> None:
    """The subtitle this was built for: one box, nothing beside it."""
    subtitle = Box(0.67, 1.29, 12.28, 0.53, "The methodology uses...")
    others = [Box(0.68, 1.88, 5.90, 0.35, "Employee Profile")]

    assert not _has_row_peers(subtitle, [subtitle] + others)


def test_a_shape_is_not_its_own_peer() -> None:
    alone = Box(0.67, 1.29, 12.28, 0.53, "on its own")

    assert not _has_row_peers(alone, [alone])
