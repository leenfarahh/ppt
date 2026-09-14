"""Copy that is drawn on top of the region it belongs in.

`claim_runs` fills a layout's repeated regions from the slide's repeated
content, and a run is three or more like-sized boxes, because that is where a
run stops being a coincidence. A subtitle is one box. So on a real deck the
layout's subtitle region sat empty, showing its own prompt text, with the
deck's subtitle drawn across the top of it in a loose box: the words on the
slide twice over, once as a prompt and once as content.

One box is not an arrangement, so there is no order to read. What there is
instead is geometry, and here it is unambiguous in a way it never was for the
agenda -- the region is 12.28in wide at 0.69in from the edge and the box is
12.28in wide at 0.67in. The same rectangle, drawn twice.
"""

from __future__ import annotations

from formatting_tool.rebuild.series import drawn_over, pair_over

EMU = 914400


class Box:
    def __init__(self, left, top, width, height, name=""):
        self.left = int(left * EMU)
        self.top = int(top * EMU)
        self.width = int(width * EMU)
        self.height = int(height * EMU)
        self.name = name

    def __repr__(self):
        return f"Box({self.name!r})"


def _subtitle_region():
    return Box(0.69, 1.28, 12.28, 0.24, "the layout's subtitle region")


def _subtitle_box():
    return Box(0.67, 1.29, 12.28, 0.53, "the deck's subtitle")


# --------------------------------------------------------------------------- #
# What counts as drawn over
# --------------------------------------------------------------------------- #

def test_the_same_rectangle_drawn_twice_is_a_match() -> None:
    assert drawn_over(_subtitle_box(), _subtitle_region()) is not None


def test_height_is_not_compared() -> None:
    """A one-line prompt strip on the layout holds three lines of real copy,
    so a box that is twice the region's height is still that region's copy."""
    tall = Box(0.67, 1.29, 12.28, 1.60, "three lines of it")

    assert drawn_over(tall, _subtitle_region()) is not None


def test_a_small_box_inside_a_big_region_is_not_a_match() -> None:
    """The reason width decides this and coverage does not. Ranked by how much
    of the region a box covers, a caption sitting inside a big empty content
    region scores perfectly -- and a slide of fourteen small boxes over one
    region would hand it to whichever happened to win."""
    caption = Box(1.0, 2.0, 2.0, 0.3, "a caption")
    big_region = Box(0.69, 1.28, 12.28, 5.0, "a whole content region")

    assert drawn_over(caption, big_region) is None


def test_a_box_in_the_next_column_is_not_a_match() -> None:
    """Same width, nowhere near it."""
    right_column = Box(7.0, 1.29, 5.90, 0.5, "the right column")
    left_region = Box(0.69, 1.28, 5.90, 0.24, "the left region")

    assert drawn_over(right_column, left_region) is None


def test_a_box_above_the_region_is_not_a_match() -> None:
    """Vertical overlap has to cover the middle of the region, or a heading
    sitting just above one would be swallowed by it."""
    above = Box(0.67, 0.40, 12.28, 0.40, "the title, above it")

    assert drawn_over(above, _subtitle_region()) is None


def test_a_box_of_a_different_width_is_not_a_match() -> None:
    half = Box(0.67, 1.29, 6.0, 0.53, "half the width")

    assert drawn_over(half, _subtitle_region()) is None


def test_a_box_with_no_size_is_not_a_match() -> None:
    assert drawn_over(Box(0, 0, 0, 0), _subtitle_region()) is None
    assert drawn_over(_subtitle_box(), Box(0, 0, 0, 0)) is None


# --------------------------------------------------------------------------- #
# Matching them up
# --------------------------------------------------------------------------- #

def test_the_box_over_a_region_is_paired_with_it() -> None:
    box, region = _subtitle_box(), _subtitle_region()

    assert pair_over([box], [region]) == [(box, region)]


def test_one_region_takes_one_box() -> None:
    """Two boxes over one region is a slide where the guess would be a coin
    toss. The better match takes it and the other is left where it is."""
    region = _subtitle_region()
    exact = Box(0.69, 1.28, 12.28, 0.24, "exactly over it")
    looser = Box(0.60, 1.29, 12.00, 0.60, "nearly over it")

    pairs = pair_over([looser, exact], [region])

    assert pairs == [(exact, region)]


def test_each_box_goes_to_its_own_region() -> None:
    """Two columns, two regions, and neither crosses over."""
    left_region = Box(0.69, 1.28, 5.90, 0.24, "left region")
    right_region = Box(7.06, 1.28, 5.90, 0.24, "right region")
    left_box = Box(0.68, 1.29, 5.90, 0.50, "left copy")
    right_box = Box(7.05, 1.29, 5.90, 0.50, "right copy")

    pairs = dict(pair_over([left_box, right_box], [left_region, right_region]))

    assert pairs[left_box] is left_region
    assert pairs[right_box] is right_region


def test_nothing_over_anything_pairs_nothing() -> None:
    assert pair_over([Box(1, 1, 2, 2)], [Box(8, 5, 4, 1)]) == []
