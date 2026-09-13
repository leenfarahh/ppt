"""Which placeholder a slide's copy lands in, on the layout it moves to.

An agenda arrived scrambled: eleven numbered items, and the rebuilt slide read
02, 03, 04 ... 11, 01, with item 01 alone at the bottom of the second column
and a two-digit number wrapped to "0 / 3" in a slot sized for something else.

The cause was the pool being taken in order. A layout's placeholders sit in the
order the designer's XML happens to carry, which has nothing to do with the
order a reader sees, and claiming "the first free one of the same family" hands
the first item whichever slot that happens to be. On an agenda of twenty-two
body placeholders it was off by one, all the way down.

Matched on the box instead: the item at the top left of the old slide goes in
the placeholder at the top left of the new one.
"""

from __future__ import annotations

from formatting_tool.rebuild.builder import _claim

EMU = 914400
CANVAS = (int(13.333 * EMU), int(7.5 * EMU))


class _Format:
    def __init__(self, kind: str) -> None:
        self.type = kind


class _Shape:
    """Enough of a python-pptx shape for the claim to measure it."""

    def __init__(self, name: str, kind: str, left: float, top: float,
                 width: float = 2.0, height: float = 0.4) -> None:
        self.name = name
        self.placeholder_format = _Format(kind)
        self.left = int(left * EMU)
        self.top = int(top * EMU)
        self.width = int(width * EMU)
        self.height = int(height * EMU)

    def __repr__(self) -> str:                      # for a readable failure
        return f"<{self.name}>"


def _agenda_pool() -> list[_Shape]:
    """A layout's slots, listed in an order no reader would recognise.

    Which is the normal case: a designer draws the second column first, or
    duplicates a row and PowerPoint appends it, and the XML order is whatever
    that left behind.
    """
    return [
        _Shape("slot right 1", "BODY (2)", left=8.66, top=1.98),
        _Shape("slot left 2", "BODY (2)", left=4.43, top=2.78),
        _Shape("slot left 1", "BODY (2)", left=4.43, top=1.98),
        _Shape("slot right 2", "BODY (2)", left=8.66, top=2.78),
    ]


def test_an_item_lands_in_the_slot_it_sits_on() -> None:
    """The whole bug in one assertion: first-in-the-list would hand the
    top-left item the right-hand slot."""
    pool = _agenda_pool()
    item = _Shape("01 Executive Summary", "BODY (2)", left=4.43, top=1.98)

    claimed = _claim(pool, item, CANVAS)

    assert claimed.name == "slot left 1"
    assert claimed not in pool             # and it is no longer free


def test_a_whole_agenda_keeps_its_reading_order() -> None:
    pool = _agenda_pool()
    items = [
        _Shape("01", "BODY (2)", left=4.43, top=1.98),
        _Shape("07", "BODY (2)", left=8.66, top=1.98),
        _Shape("02", "BODY (2)", left=4.43, top=2.78),
        _Shape("08", "BODY (2)", left=8.66, top=2.78),
    ]

    landed = {item.name: _claim(pool, item, CANVAS).name for item in items}

    assert landed == {
        "01": "slot left 1",
        "07": "slot right 1",
        "02": "slot left 2",
        "08": "slot right 2",
    }
    assert pool == []                      # every slot used exactly once


def test_a_shape_that_overlaps_nothing_takes_the_nearest_slot() -> None:
    """A messy deck puts an item where the new layout puts nothing. The nearer
    of the free slots is the better guess, and guessing is what is left."""
    pool = _agenda_pool()
    stray = _Shape("stray", "BODY (2)", left=4.30, top=6.90)

    claimed = _claim(pool, stray, CANVAS)

    assert claimed.name == "slot left 2"   # the lower of the two left slots


def test_a_family_the_layout_does_not_have_claims_nothing() -> None:
    """Then the shape is transplanted as it is, rather than forced into a slot
    meant for something else -- extra content is never silently lost."""
    pool = [_Shape("body", "BODY (2)", left=1.0, top=1.0)]

    assert _claim(pool, _Shape("pic", "PICTURE (18)", left=1.0, top=1.0), CANVAS) is None
    assert len(pool) == 1


def test_a_shape_with_no_geometry_still_claims_one() -> None:
    """A hand-built deck can carry a placeholder that answers nothing about
    where it is. It should still find a home; it just cannot be placed well."""
    pool = [_Shape("body", "BODY (2)", left=1.0, top=1.0)]
    nowhere = _Shape("body", "BODY (2)", left=0.0, top=0.0)
    nowhere.left = None

    assert _claim(pool, nowhere, CANVAS) is not None
