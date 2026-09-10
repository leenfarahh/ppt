"""Repeated things on a slide, and the units they belong to.

A consulting deck is built out of repetition: five process steps, four status
columns, eleven numbered badges. Two rules already turn on finding those sets
-- `space.repeat_out_of_line` measures a member against the arrangement the
rest of the set agrees on, and `space.series_type_fit` needs the set before it
can change anything about the type in it -- so the grouping lives here rather
than inside either of them.

Two questions, and they are different:

- `series_of` finds the repeated SHAPES: four boxes of identical size and
  kind. That is enough to say one of them is out of line.
- `units_of` finds the repeated CARDS those shapes sit in: the heading, the
  labels, the copy and the bar that travel together as one column, four times
  across. That is what a change to type has to act on, because a card whose
  status line shrank and whose heading did not is two defects where there was
  one.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from ..models import Geometry, ShapeProfile, SlideProfile


def series_of(slide: SlideProfile, minimum: int) -> Iterable[list[ShapeProfile]]:
    """Groups of shapes that are the same thing repeated.

    Grouped on rounded size and shape type together. Size alone would put a
    row of icons and a row of same-sized text boxes in one series; type alone
    would group every rectangle on a busy slide, whatever its dimensions.

    Only top-level shapes. A grouped diagram is placed as one object, and its
    parts are positioned relative to each other by whoever drew it, so holding
    them to a shared edge would report the drawing rather than a defect.
    """
    buckets: dict[tuple, list[ShapeProfile]] = defaultdict(list)
    for shape in slide.shapes:
        if shape.is_group or not shape.geometry.width_in:
            continue
        key = (
            round(shape.geometry.width_in, 2),
            round(shape.geometry.height_in, 2),
            shape.shape_type,
        )
        buckets[key].append(shape)

    for members in buckets.values():
        if len(members) >= minimum:
            yield members


# How much of a shape's width has to fall inside one band before it reads as
# belonging to that band. Generous, because a card heading is routinely drawn
# a little wider or narrower than the copy box under it and nobody notices.
_BAND_COVERAGE = 0.8

# And how far it may reach into any OTHER band. Small on purpose: this is the
# test that keeps a full-width deck title, which sits across all four bands,
# out of every one of them.
_BAND_STRAY = 0.1

# Tolerance on "the same place in its own card", in inches. Cards are placed
# by hand and a tenth of an inch of drift between one and the next is normal;
# half an inch is a different element.
_SAME_PLACE_IN = 0.15


def units_of(
    slide: SlideProfile,
    series: list[ShapeProfile],
    majority: float,
) -> list[list[ShapeProfile]]:
    """The repeated card each member of `series` sits in, one list per card.

    The series gives the columns: each member's left and right edge mark out
    a band, and the bands are the cards. What goes in a card is then decided
    twice over, and it takes both tests to be right about it.

    First, horizontally: a shape belongs to a band when nearly all of its
    width is inside that band and it barely reaches into any other. A card
    heading drawn a little wider than the copy below it still belongs; a
    deck title spanning the slide reaches into every band and belongs to
    none, which is the answer that keeps it out.

    Second, and this is the test that does the real work: an element only
    counts if it REPEATS. Its counterpart has to appear in most of the other
    cards at the same place within them -- same offset from the card's left
    edge, same height down the slide, same size. Four headings at the top of
    four columns pass. A footnote sitting under the second column alone does
    not, and neither does anything else that happens to be parked over a card
    without being part of it.

    Cards come back in the series' own order, empty ones included, so a
    caller can see that a band contributed nothing.
    """
    bands = [
        (shape.geometry.left_in, shape.geometry.right_in) for shape in series
    ]
    if len(bands) < 2:
        return [list(series)]

    members: list[list[ShapeProfile]] = [[] for _ in bands]
    keys: list[list[tuple]] = [[] for _ in bands]
    for shape in slide.shapes:
        if shape.is_group or shape.geometry.width_in <= 0:
            continue
        band = _band_of(bands, shape.geometry)
        if band is None:
            continue
        members[band].append(shape)
        keys[band].append(_place_key(shape, bands[band][0]))

    # An element is part of the card only if most of the cards have one.
    counted: dict[tuple, set[int]] = defaultdict(set)
    for index, band_keys in enumerate(keys):
        for key in band_keys:
            counted[key].add(index)
    needed = max(2, round(majority * len(bands)))
    repeated = {key for key, seen in counted.items() if len(seen) >= needed}

    return [
        [
            shape for shape, key in zip(band_members, band_keys)
            if key in repeated
        ]
        for band_members, band_keys in zip(members, keys)
    ]


def _band_of(bands: list[tuple[float, float]], box: Geometry) -> Optional[int]:
    """Which band a box belongs to, or None when it belongs to none."""
    fractions = [
        max(0.0, min(box.right_in, right) - max(box.left_in, left)) / box.width_in
        for left, right in bands
    ]
    best = max(range(len(fractions)), key=lambda i: fractions[i])
    if fractions[best] < _BAND_COVERAGE:
        return None
    if any(f > _BAND_STRAY for i, f in enumerate(fractions) if i != best):
        return None
    return best


def _place_key(shape: ShapeProfile, band_left: float) -> tuple:
    """Where a shape sits in its own card, to the tolerance cards are placed at.

    Rounded to a fixed grid rather than clustered. Two shapes either side of
    a boundary come out as different elements and the card they are in
    contributes nothing, which costs a finding; clustering to fix that would
    let a card's heading and its copy merge into one element on a tight
    layout, which would cost the right answer.
    """
    box = shape.geometry
    step = _SAME_PLACE_IN
    return (
        round((box.left_in - band_left) / step),
        round(box.top_in / step),
        round(box.width_in / step),
        round(box.height_in / step),
        shape.shape_type,
    )


def half_point(size: float) -> float:
    """A point size down to the nearest half point.

    Down rather than nearest, so the arithmetic that said this size fits is
    not undone by the rounding. Half points because that is the granularity
    PowerPoint's own size box offers, and a deck full of 9.37pt type reads as
    a machine having been at it.

    Here rather than in either caller because the rule decides whether a set
    can be scaled and the fixer writes the sizes, and two copies of the
    rounding would let them disagree about what the plan meant.
    """
    return int(size * 2) / 2
