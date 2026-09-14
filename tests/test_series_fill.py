"""Filling a layout's repeated regions from a slide's repeated content.

A messy deck's content is in loose boxes, and the rebuild only ever offered a
placeholder to a shape that was already one. So a real deck's agenda arrived on
the master's `Agenda` layout with all twenty-four of its slots empty and the
five agenda items sitting at their old inches on top of the layout's
photograph: the right layout, chosen correctly, and it may as well not have
been.

These cover the run detection and the things that went wrong building it, each
of which produced a wrong slide rather than an error.
"""

from __future__ import annotations

from formatting_tool.rebuild.series import find_series, furniture_of, pair_up

EMU = 914400


class Box:
    """The least a shape can be and still be measurable and readable."""

    def __init__(self, left, top, width, height, text=""):
        self.left = int(left * EMU)
        self.top = int(top * EMU)
        self.width = int(width * EMU)
        self.height = int(height * EMU)
        self._text = text

    @property
    def has_text_frame(self):
        return True

    @property
    def text_frame(self):
        return self

    @property
    def text(self):
        return self._text

    def inches(self):
        return (self.left / EMU, self.top / EMU,
                self.width / EMU, self.height / EMU)

    def __repr__(self):
        return f"Box({self._text!r})"


def _agenda_items():
    """The real deck's five agenda cards: one row across the bottom."""
    return [
        Box(0.72, 5.47, 2.15, 0.62, "Our Understanding"),
        Box(3.17, 5.47, 2.17, 0.62, "Approach & Methodology"),
        Box(5.58, 5.47, 2.17, 0.62, "Governance, Security & Compliance"),
        Box(8.04, 5.47, 2.17, 0.62, "Organization & Credentials"),
        Box(10.47, 5.47, 2.17, 0.62, "Financial Proposal"),
    ]


def _agenda_labels():
    """The twelve wide slots of the real master's agenda layout."""
    return [
        Box(column, row, 3.92, 0.63)
        for column in (4.44, 8.68)
        for row in (1.98, 2.78, 3.58, 4.38, 5.18, 5.98)
    ]


def _agenda_numbers():
    """And the twelve narrow ordinal slots beside them."""
    return [
        Box(column, row, 0.48, 0.63)
        for column in (4.43, 8.66)
        for row in (1.98, 2.78, 3.58, 4.38, 5.18, 5.98)
    ]


# --------------------------------------------------------------------------- #
# Finding a run
# --------------------------------------------------------------------------- #

def test_a_row_of_like_sized_boxes_is_a_run() -> None:
    runs = find_series(_agenda_items())

    assert len(runs) == 1
    assert [box.text for box in runs[0].shapes] == [
        "Our Understanding",
        "Approach & Methodology",
        "Governance, Security & Compliance",
        "Organization & Credentials",
        "Financial Proposal",
    ]


def test_a_tall_list_reads_down_and_then_across() -> None:
    """The master's own sample slide for that agenda layout fills the left
    column `01` to `06` and the right `07` to `12`, so a two-column list reads
    down before it reads across. A row of cards reads the other way, and both
    fall out of one rule: the taller arrangement reads down."""
    run = find_series(_agenda_labels(), shortest=2)[0]

    assert (run.rows, run.columns) == (6, 2)
    first_six = [box.inches() for box in run.shapes[:6]]
    assert [round(box[0], 2) for box in first_six] == [4.44] * 6
    assert [round(box[1], 2) for box in first_six] == [
        1.98, 2.78, 3.58, 4.38, 5.18, 5.98
    ]


def test_scattered_boxes_of_one_size_are_not_a_run() -> None:
    """Three like-sized boxes that do not fill a grid are three unrelated
    things drawn the same size, and filling a layout from them would move copy
    somewhere nobody put it."""
    scattered = [
        Box(0.5, 0.5, 2.0, 0.6, "one"),
        Box(5.0, 3.0, 2.0, 0.6, "two"),
        Box(9.5, 6.0, 2.0, 0.6, "three"),
    ]
    assert find_series(scattered) == []


def test_two_boxes_are_not_enough_on_the_slide_side() -> None:
    """A caption and a note are routinely the same size and routinely not a
    series. Three is where a run stops being a coincidence -- and the layout
    side is still allowed two, because two columns of regions is a real
    arrangement and it is the slide's run that has to clear the bar."""
    pair = [Box(0.7, 5.0, 2.0, 0.6, "one"), Box(3.2, 5.0, 2.0, 0.6, "two")]

    assert find_series(pair) == []
    assert len(find_series(pair, shortest=2)) == 1


# --------------------------------------------------------------------------- #
# Choosing which regions to fill
# --------------------------------------------------------------------------- #

def test_the_labels_are_chosen_over_the_ordinal_slots() -> None:
    """The bug this is really about. Comparing aspect ratios by SUBTRACTION
    made the 0.48in ordinal slots look nearer to the slide's 2.15in cards than
    the 3.92in labels -- by four hundredths -- and five agenda titles went into
    five half-inch boxes. Twice as wide and half as wide are equally unlike,
    which is what a ratio says and a difference cannot."""
    slots = _agenda_labels() + _agenda_numbers()
    pairs = pair_up(find_series(_agenda_items()), find_series(slots, shortest=2))

    assert len(pairs) == 1
    _run, chosen = pairs[0]
    assert round(chosen.width, 2) == 3.92


def test_a_run_with_nowhere_long_enough_is_left_alone() -> None:
    """Nothing is filled by halves. Half a deck's agenda in the right place and
    half in the wrong one is harder to fix than none of it."""
    too_short = [Box(4.4, 2.0, 3.9, 0.6), Box(4.4, 2.8, 3.9, 0.6)]

    assert pair_up(find_series(_agenda_items()),
                   find_series(too_short, shortest=2)) == []


# --------------------------------------------------------------------------- #
# What goes with a run when it moves
# --------------------------------------------------------------------------- #

def _agenda_drawing():
    """What the old layout drew around those five items: an icon over each, a
    rule between each pair, and an empty panel under each."""
    icons = [Box(x, 4.58, 0.66, 0.66) for x in (1.49, 3.86, 6.26, 8.79, 11.20)]
    rules = [Box(x, 4.38, 0.0, 2.25) for x in (3.02, 5.45, 7.88, 10.31)]
    panels = [Box(x, 6.05, 2.15, 0.55) for x in (0.72, 3.17, 5.58, 8.04, 10.47)]
    return icons + rules + panels


def test_the_drawing_around_a_run_goes_with_it() -> None:
    """Move the labels into the new layout's list and leave the drawing behind,
    and the slide reads as a tidy agenda with five orphaned icons and four
    rules floating across it -- worse than before, with the copy in the right
    place."""
    items = _agenda_items()
    run = find_series(items)[0]
    drawing = _agenda_drawing()

    went = furniture_of(run, items + drawing)

    assert {id(shape) for shape in went} == {id(shape) for shape in drawing}


def test_anything_carrying_copy_stays() -> None:
    """Words are content and stay, whatever they sit near."""
    items = _agenda_items()
    run = find_series(items)[0]
    caption = Box(0.72, 6.05, 2.15, 0.55, "a footnote worth keeping")

    assert furniture_of(run, items + [caption]) == []


def test_something_bigger_than_an_item_stays() -> None:
    """A photograph beside a list is not part of the list."""
    items = _agenda_items()
    run = find_series(items)[0]
    photo = Box(0.0, 4.5, 6.5, 3.0)

    assert furniture_of(run, items + [photo]) == []


def test_something_outside_the_block_stays() -> None:
    """A logo up in the corner is not the run's drawing."""
    items = _agenda_items()
    run = find_series(items)[0]
    logo = Box(11.9, 0.7, 0.67, 0.56)

    assert furniture_of(run, items + [logo]) == []
