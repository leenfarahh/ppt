"""What happens to a run's icons, and to a title the layout already writes.

Two defects a designer saw on one rebuilt agenda slide. The icons drawn above
the five agenda items were deleted along with the rules and blank panels around
them -- correct for the drawing, wrong for the icons, which are content someone
chose. And the slide said "Agenda" twice: once in the layout's own display
type, and once in the deck's title placeholder, which the layout had no title
region to take.
"""

from __future__ import annotations

from formatting_tool.rebuild.builder import (
    _is_imagery,
    echoes_layout,
    layout_writes,
)
from formatting_tool.rebuild.series import (
    belongs_to,
    companion_of,
    find_series,
    fit_into,
)

EMU = 914400


class Box:
    def __init__(self, left, top, width, height, text="", kind="TEXT_BOX (17)"):
        self.left = int(left * EMU)
        self.top = int(top * EMU)
        self.width = int(width * EMU)
        self.height = int(height * EMU)
        self._text = text
        self.shape_type = kind

    @property
    def has_text_frame(self):
        return True

    @property
    def text_frame(self):
        return self

    @property
    def text(self):
        return self._text

    @property
    def is_placeholder(self):
        return False

    def inches(self):
        return (self.left / EMU, self.top / EMU,
                self.width / EMU, self.height / EMU)


def _items():
    return [
        Box(0.72, 5.47, 2.15, 0.62, "Our Understanding"),
        Box(3.17, 5.47, 2.17, 0.62, "Approach & Methodology"),
        Box(5.58, 5.47, 2.17, 0.62, "Governance"),
        Box(8.04, 5.47, 2.17, 0.62, "Organization"),
        Box(10.47, 5.47, 2.17, 0.62, "Financial Proposal"),
    ]


def _icons():
    """One over each item, as the deck drew them."""
    return [
        Box(x, 4.58, 0.66, 0.66, kind="PICTURE (13)")
        for x in (1.49, 3.86, 6.26, 8.79, 11.20)
    ]


def _labels():
    return [
        Box(column, row, 3.92, 0.63)
        for column in (4.44, 8.68)
        for row in (1.98, 2.78, 3.58, 4.38, 5.18, 5.98)
    ]


def _ordinals():
    return [
        Box(column, row, 0.48, 0.63)
        for column in (4.43, 8.66)
        for row in (1.98, 2.78, 3.58, 4.38, 5.18, 5.98)
    ]


# --------------------------------------------------------------------------- #
# Where an item's icon goes when the item moves
# --------------------------------------------------------------------------- #

def test_the_layouts_paired_regions_are_found() -> None:
    """A designer who draws twelve labels usually draws twelve somethings
    beside them. Here it is the ordinal each agenda item is numbered with, and
    it is exactly where that item's icon belongs once the item has moved."""
    labels, ordinals = _labels(), _ordinals()
    runs = find_series(labels + ordinals, shortest=2)
    filled = next(run for run in runs if round(run.width, 2) == 3.92)

    companion = companion_of(filled, runs, taken=filled.shapes)

    assert companion is not None
    assert round(companion.width, 2) == 0.48
    # Paired row for row, not merely equal in length.
    for slot, beside in zip(filled.shapes, companion.shapes):
        assert slot.top == beside.top


def test_a_run_of_the_same_length_that_does_not_pair_is_not_a_companion() -> None:
    """Two unrelated runs that happen to be the same length are not a pairing,
    and moving a slide's icons into one would scatter them."""
    labels = _labels()
    elsewhere = [Box(0.5, 0.5 + n * 0.2, 0.48, 0.15) for n in range(12)]
    runs = find_series(labels + elsewhere, shortest=2)
    filled = next(run for run in runs if round(run.width, 2) == 3.92)

    assert companion_of(filled, runs, taken=filled.shapes) is None


def test_each_icon_is_matched_to_the_item_it_sits_over() -> None:
    items, icons = _items(), _icons()
    run = find_series(items)[0]

    owner = belongs_to(run, icons)

    assert [owner[id(icon)] for icon in icons] == [0, 1, 2, 3, 4]


def test_an_icon_is_fitted_into_its_region_without_being_stretched() -> None:
    """Scaled down to fit and centred, never scaled up: an icon drawn at 0.66in
    is drawn at 0.66in, and blowing it up to fill a region is a change nobody
    asked for."""
    icon = Box(1.49, 4.58, 0.66, 0.66, kind="PICTURE (13)")
    slot = Box(4.43, 1.98, 0.48, 0.63)

    left, top, width, height = fit_into(icon, slot)

    assert width == height                       # still square
    assert round(width / EMU, 2) == 0.48         # fitted to the narrow side
    # Centred in the slot, to within the rounding that going through EMU costs.
    assert round(left / EMU, 2) == 4.43
    assert round(top / EMU, 2) == round(1.98 + (0.63 - 0.48) / 2, 2)


def test_a_small_icon_is_not_blown_up() -> None:
    icon = Box(0.0, 0.0, 0.2, 0.2, kind="PICTURE (13)")
    slot = Box(4.43, 1.98, 0.48, 0.63)

    _left, _top, width, height = fit_into(icon, slot)

    assert (round(width / EMU, 2), round(height / EMU, 2)) == (0.2, 0.2)


# --------------------------------------------------------------------------- #
# Which shapes follow a run, and which are its old drawing
# --------------------------------------------------------------------------- #

def test_a_picture_follows_and_a_rule_does_not() -> None:
    """An icon beside an agenda item is content someone chose. The rule under
    it is the old layout's way of separating two rows, and the new layout draws
    its own."""
    assert _is_imagery(Box(0, 0, 1, 1, kind="PICTURE (13)"))
    assert _is_imagery(Box(0, 0, 1, 1, kind="GROUP (6)"))
    assert not _is_imagery(Box(0, 0, 1, 1, kind="LINE (9)"))
    assert not _is_imagery(Box(0, 0, 1, 1, kind="AUTO_SHAPE (1)"))


# --------------------------------------------------------------------------- #
# A title the layout already writes
# --------------------------------------------------------------------------- #

class _Layout:
    def __init__(self, shapes):
        self.shapes = shapes


def test_the_words_a_layout_writes_itself_are_collected() -> None:
    """Its own shapes, not its placeholders: a placeholder is a space for
    someone else's copy, and what is wanted is what the layout already has."""
    layout = _Layout([Box(4.47, 0.50, 3.55, 1.02, "AGENDA", kind="AUTO_SHAPE (1)")])

    assert layout_writes(layout) == {"agenda"}


def test_a_title_the_layout_already_writes_is_an_echo() -> None:
    """The master's agenda layout has no title region at all -- it draws the
    word itself, as artwork. So the deck's title found no home, was
    transplanted at its old inches, and the slide said "Agenda" twice."""
    written = {"agenda"}

    assert echoes_layout(Box(0, 0.71, 11.92, 0.56, "Agenda"), written)
    assert echoes_layout(Box(0, 0.71, 11.92, 0.56, " AGENDA. "), written)


def test_a_heading_that_says_more_is_not_an_echo() -> None:
    """Matched on the whole of the text, not on a part of it. A heading that
    repeats the layout's word and then says more is a heading."""
    written = {"agenda"}

    assert not echoes_layout(Box(0, 0, 5, 1, "Agenda for the quarter"), written)
    assert not echoes_layout(Box(0, 0, 5, 1, ""), written)


def test_nothing_is_an_echo_when_the_layout_writes_nothing() -> None:
    assert not echoes_layout(Box(0, 0, 5, 1, "Agenda"), set())
