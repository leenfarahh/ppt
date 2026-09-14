"""Find the repeated runs of content on a slide, and on a layout.

WHY THIS EXISTS. `rebuild.builder` moves a slide's copy into the new layout's
placeholders, and only ever offered a placeholder to a shape that was already
one. A messy deck's content is in loose boxes -- that is what makes it messy --
so on a real deck the master's `Agenda` layout arrived with all twenty-four of
its slots empty and the slide's five agenda items transplanted at their old
inches, across the bottom of a layout that puts a photograph there. The right
layout, correctly chosen, and it may as well not have been.

WHY NOT THE OVERLAP MATCH THAT IS ALREADY THERE. `builder._claim` puts a
placeholder's copy in the slot that sits where it used to sit, which is right
for a slide that was already on a layout of the same shape. It is not an
answer here, and the agenda says why: the slide's agenda is five cards across
the bottom and the layout's agenda is a twelve-item list down the right half,
so of five items one overlaps nothing at all, two want the same slot and the
other two want another. The two arrangements are not the same picture, and
asking which slot a box is nearest cannot map one onto the other.

WHAT DOES MAP THEM IS ORDER. A designer reads the slide's items 1..n and fills
the layout's slots 1..n, and both sides state their order plainly: a series is
a run of like-sized boxes in a regular arrangement, and the order is the order
they are read in. The master's own sample slide for that agenda layout confirms
the reading -- it fills the left column `01` to `06` and the right `07` to `12`,
so a tall two-column list reads down and then across, not across and then
down.

WHAT MAKES IT SAFE. Both sides have to actually BE a series -- like-sized boxes
filling a regular grid, three or more of them on the slide -- and the layout's
series has to be long enough to take the whole of the slide's. Nothing is
filled by halves: either every item of a run gets a slot, or the run is left
exactly as it was. That is the guard the old "will not decide which box goes
where" stance was really protecting, and it is kept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

log = logging.getLogger(__name__)

# How much two boxes may differ and still be the same size. Generous on
# purpose: a real deck's five agenda cards measure 2.15in and 2.17in wide, and
# a run a designer drew by hand is never to the hundredth.
_SIZE_TOLERANCE = 0.12

# How far apart two edges may be and still be called the same row or column.
# A tenth of an inch is under the width of a hairline at reading size, and it
# is what `classify.column_bands` already treats as one band.
_BAND_TOLERANCE = 0.15

# The shortest run worth filling from. Two like-sized boxes side by side are
# routinely not a series -- a caption and a note, a label and its value -- and
# guessing wrong moves copy somewhere a designer did not put it. Three is where
# a run stops being a coincidence, and it is what the arrangements this is for
# actually carry: five agenda items, four cards, three columns.
_SHORTEST_RUN = 3


@dataclass
class Series:
    """A run of like-sized shapes, in the order they are read."""

    shapes: list[Any]
    rows: int
    columns: int

    def __len__(self) -> int:
        return len(self.shapes)

    @property
    def width(self) -> float:
        return _box(self.shapes[0])[2]

    @property
    def height(self) -> float:
        return _box(self.shapes[0])[3]


def find_series(
    shapes: Sequence[Any], shortest: int = _SHORTEST_RUN
) -> list[Series]:
    """Every run of like-sized shapes these form, longest first.

    Size groups them and regularity confirms them: a run has to fill its own
    grid, so three boxes in a row are a series and three scattered boxes of the
    same size are not. A shape belongs to at most one run.
    """
    runs: list[Series] = []
    for group in _by_size(shapes):
        if len(group) < shortest:
            continue
        ordered = _ordered(group)
        if ordered is not None:
            runs.append(ordered)
    runs.sort(key=len, reverse=True)
    return runs


def pair_up(
    slide_runs: Sequence[Series], layout_runs: Sequence[Series]
) -> list[tuple[Series, Series]]:
    """Match each of the slide's runs to a layout run long enough to hold it.

    Longest first, so the slide's main run gets first refusal on the layout's.
    A run with no home is not matched and its shapes are left alone, which is
    the whole of the safety here: a partly filled series is worse than an
    untouched one, because half a deck's agenda in the right place and half in
    the wrong one is harder to fix than none of it.
    """
    free = list(layout_runs)
    pairs: list[tuple[Series, Series]] = []
    for run in sorted(slide_runs, key=len, reverse=True):
        best = _closest(run, free)
        if best is None:
            continue
        free.remove(best)
        pairs.append((run, best))
    return pairs


def companion_of(
    filled: Series, runs: Sequence[Series], taken: Sequence[Any] = ()
) -> Optional[Series]:
    """The run of regions a layout pairs one-for-one with the one just filled.

    A designer who draws a list of twelve labels usually draws twelve
    somethings beside them: an ordinal, a rule, a swatch. On the master this
    was built against, each of the twelve agenda labels has a 0.48in box at the
    same row and the same column, which is where its number goes -- and it is
    exactly where the item's icon belongs when the item had one.

    Paired, not merely equal in length: same grid, and every member sharing a
    row with a member of the filled run. Two unrelated runs that happen to be
    the same length are not a pairing, and moving a slide's icons into one
    would scatter them.
    """
    used = {id(shape) for shape in taken}
    for run in runs:
        if run is filled or len(run) != len(filled):
            continue
        if (run.rows, run.columns) != (filled.rows, filled.columns):
            continue
        if any(id(shape) in used for shape in run.shapes):
            continue
        if all(_same_row(a, b) for a, b in zip(run.shapes, filled.shapes)):
            return run
    return None


def _same_row(a: Any, b: Any) -> bool:
    _la, ta, _wa, ha = _box(a)
    _lb, tb, _wb, hb = _box(b)
    return abs((ta + ha / 2) - (tb + hb / 2)) <= _BAND_TOLERANCE


def belongs_to(run: Series, shapes: Sequence[Any]) -> dict[int, Any]:
    """Which item of the run each of these shapes sits with, by its cell.

    Measured from the centres along the run's own long axis: a row of five
    cards is five columns, and a shape belongs to the card it sits over.
    """
    if not run.shapes:
        return {}
    across = run.columns >= run.rows
    centres = [_centre(shape, across) for shape in run.shapes]

    out: dict[int, Any] = {}
    for shape in shapes:
        point = _centre(shape, across)
        nearest = min(range(len(centres)), key=lambda i: abs(centres[i] - point))
        out[id(shape)] = nearest
    return out


def _centre(shape: Any, across: bool) -> float:
    left, top, width, height = _box(shape)
    return left + width / 2 if across else top + height / 2


def fit_into(shape: Any, target: Any) -> tuple[int, int, int, int]:
    """Where to put `shape` so it sits inside `target`'s box, undistorted.

    Returned in EMU, ready to assign. Scaled down to fit and centred; never
    scaled UP, because an icon drawn at 0.6in is drawn at 0.6in and blowing it
    up to fill a region is a change nobody asked for.
    """
    _sl, _st, s_width, s_height = _box(shape)
    t_left, t_top, t_width, t_height = _box(target)
    if s_width <= 0 or s_height <= 0:
        return (int(t_left * EMU), int(t_top * EMU),
                int(t_width * EMU), int(t_height * EMU))
    scale = min(1.0, t_width / s_width, t_height / s_height)
    width, height = s_width * scale, s_height * scale
    return (
        int((t_left + (t_width - width) / 2) * EMU),
        int((t_top + (t_height - height) / 2) * EMU),
        int(width * EMU),
        int(height * EMU),
    )


EMU = 914400


def furniture_of(run: Series, loose: Sequence[Any]) -> list[Any]:
    """The decoration drawn around this run, which moves out with it.

    A RUN IS NOT ONLY ITS WORDS. The agenda that motivated all of this is five
    labels, and also five icons above them, four rules between them and five
    empty panels below -- one old layout's way of drawing a list. Move the
    labels into the new layout's own list and the drawing stays behind, so the
    slide reads as a tidy agenda on the right with five orphaned icons and four
    rules floating across it. Worse than before, not better, and the copy was
    in the right place.

    So the drawing goes with the words. Three conditions, and each is there to
    stop this from taking something a designer wanted:

    - IT CARRIES NO COPY. Anything with words in it is content and stays,
      whatever it sits near.
    - IT IS NO BIGGER THAN ONE ITEM OF THE RUN. A photograph beside a list is
      not part of the list; a 0.6in icon over a 2.15in label is.
    - IT SITS IN THE RUN'S BLOCK -- within the run's own extent along its long
      axis, and within one item's height of it across. That is the strip a
      reader sees as the run, and it is where its drawing lives.

    Everything it returns is reported by the caller, because this deletes
    things, and a deletion a designer cannot see is not one they can undo.
    """
    if not run.shapes:
        return []
    left, top, right, bottom = _extent(run.shapes)
    reach = max(run.height, 0.1)
    cell = run.width * run.height

    inside = []
    for shape in loose:
        if shape in run.shapes:
            continue
        if _has_words(shape):
            continue
        s_left, s_top, s_width, s_height = _box(shape)
        if s_width * s_height > cell:
            continue
        s_right, s_bottom = s_left + s_width, s_top + s_height
        if s_right < left or s_left > right:
            continue
        if s_bottom < top - reach or s_top > bottom + reach:
            continue
        inside.append(shape)
    return inside


def _extent(shapes: Sequence[Any]) -> tuple[float, float, float, float]:
    boxes = [_box(shape) for shape in shapes]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[0] + b[2] for b in boxes),
        max(b[1] + b[3] for b in boxes),
    )


def _has_words(shape: Any) -> bool:
    try:
        return bool(shape.has_text_frame and shape.text_frame.text.strip())
    except Exception:
        return False


def _closest(run: Series, candidates: Sequence[Series]) -> Optional[Series]:
    """The layout run that can hold this one and is the most like it in shape.

    LONG ENOUGH FIRST, because a run that does not fit is not a candidate at
    all. Then the nearest in shape, which is what tells a layout's label slots
    from the little ordinal slots beside them: on a real agenda layout the
    twelve labels are 3.92in wide and the twelve numbers 0.48in, and the
    slide's 2.15in cards are plainly asking for the first of those.

    MEASURED AS A RATIO, NOT A DIFFERENCE, and that is not a detail. Comparing
    aspect ratios by subtraction put the agenda's five items in the NUMBER
    slots: the labels are 6.2 wide-to-tall against the items' 3.5 and the
    numbers 0.76, so by subtraction the numbers were nearer by four
    hundredths, and five agenda titles went into five half-inch boxes. Twice as
    wide and half as wide are equally unlike, which is what a ratio says and a
    difference cannot.
    """
    fits = [c for c in candidates if len(c) >= len(run)]
    if not fits:
        return None
    return min(fits, key=lambda c: _apart(c, run))


def _apart(a: Series, b: Series) -> float:
    """How unlike two runs' boxes are, as a scale-free distance."""
    from math import log  # noqa: PLC0415 - one call site

    def ratio(x: float, y: float) -> float:
        return abs(log(max(x, 1e-3) / max(y, 1e-3)))

    return ratio(a.width, b.width) + ratio(a.height, b.height)


def _by_size(shapes: Sequence[Any]) -> list[list[Any]]:
    """Group shapes whose boxes are the same size, within tolerance."""
    groups: list[list[Any]] = []
    for shape in shapes:
        _left, _top, width, height = _box(shape)
        if width <= 0 or height <= 0:
            continue
        for group in groups:
            _l, _t, gw, gh = _box(group[0])
            if (abs(gw - width) <= _SIZE_TOLERANCE
                    and abs(gh - height) <= _SIZE_TOLERANCE):
                group.append(shape)
                break
        else:
            groups.append([shape])
    return groups


def _ordered(group: Sequence[Any]) -> Optional[Series]:
    """These shapes in reading order, or None if they are not a regular run.

    REGULAR MEANS IT FILLS ITS OWN GRID. The rows and columns the boxes sit in
    are counted, and the run is only a run when there is one box per cell:
    five in a row, twelve in two columns of six. Three like-sized boxes
    scattered over a slide count three rows and three columns and so are not a
    run, which is the point -- they are three unrelated things that happen to
    have been drawn the same size.

    A TALL LIST READS DOWN, A WIDE ONE ACROSS. Two columns of six is an agenda
    and reads `01`..`06` then `07`..`12`; three boxes in a row are cards and
    read left to right. The master this was measured against says so in its own
    sample slide, which fills the left column first.
    """
    rows = _bands(_box(shape)[1] for shape in group)
    columns = _bands(_box(shape)[0] for shape in group)
    if len(rows) * len(columns) != len(group):
        return None

    def cell(shape):
        left, top, _w, _h = _box(shape)
        return _band_of(top, rows), _band_of(left, columns)

    down = len(rows) > len(columns)
    ordered = sorted(
        group, key=lambda s: (cell(s)[1], cell(s)[0]) if down else cell(s)
    )
    return Series(shapes=ordered, rows=len(rows), columns=len(columns))


def _bands(values) -> list[float]:
    """The distinct positions in this list, within tolerance, in order."""
    bands: list[float] = []
    for value in sorted(values):
        if not bands or value - bands[-1] > _BAND_TOLERANCE:
            bands.append(value)
    return bands


def _band_of(value: float, bands: Sequence[float]) -> int:
    for index, band in enumerate(bands):
        if abs(value - band) <= _BAND_TOLERANCE:
            return index
    return len(bands)


def _box(shape: Any) -> tuple[float, float, float, float]:
    """A shape's box in inches, or zeroes when it will not say."""
    try:
        return (
            (shape.left or 0) / 914400,
            (shape.top or 0) / 914400,
            (shape.width or 0) / 914400,
            (shape.height or 0) / 914400,
        )
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)


# How much of a region a box has to sit over before it IS that region's copy,
# rather than something that happens to be near it.
#
# Measured on the slide this was written for: the layout's subtitle region is
# 12.28in wide and 0.24in tall, and the loose box carrying the subtitle is
# 12.28in wide and 0.53in tall, one hundredth of an inch away. They are the
# same rectangle drawn twice.
_SAME_WIDTH = 0.75          # the narrower over the wider
_SIDE_BY_SIDE = 0.8         # of the narrower box, horizontally
_DOWN_THE_MIDDLE = 0.5      # of the REGION's height, vertically


def drawn_over(shape: Any, region: Any) -> Optional[float]:
    """How strongly this box reads as copy drawn on top of that region.

    None when it does not, and a score to rank by when it does.

    WIDTH IS WHAT DECIDES IT, and the alternative is worth saying because it
    looked right first. Ranking by how much of the region a box covers lets a
    small caption sitting inside a big empty content region score 1.0, and a
    slide of fourteen small boxes over one region would hand it to whichever
    happened to win. A box that IS a region's copy has the region's WIDTH --
    that is what it was typed into -- and it may be any height at all, because
    a one-line prompt strip on the layout holds three lines of real copy.

    So: near enough the same width, sitting over it horizontally, and covering
    the middle of it vertically. Height is deliberately not compared.
    """
    s_left, s_top, s_width, s_height = _box(shape)
    r_left, r_top, r_width, r_height = _box(region)
    if s_width <= 0 or s_height <= 0 or r_width <= 0 or r_height <= 0:
        return None

    if min(s_width, r_width) / max(s_width, r_width) < _SAME_WIDTH:
        return None

    across = min(s_left + s_width, r_left + r_width) - max(s_left, r_left)
    if across <= 0 or across / min(s_width, r_width) < _SIDE_BY_SIDE:
        return None

    down = min(s_top + s_height, r_top + r_height) - max(s_top, r_top)
    if down <= 0 or down / r_height < _DOWN_THE_MIDDLE:
        return None

    return (across / max(s_width, r_width)) * (down / r_height)


def pair_over(
    loose: Sequence[Any], regions: Sequence[Any]
) -> list[tuple[Any, Any]]:
    """Match boxes to the regions they are drawn over, best first.

    One region to one box: two boxes over one region is a slide where the
    guess would be a coin toss, so the better match takes it and the other is
    left exactly where it is.
    """
    scored = []
    for shape in loose:
        for region in regions:
            score = drawn_over(shape, region)
            if score is not None:
                scored.append((score, shape, region))
    scored.sort(key=lambda row: row[0], reverse=True)

    taken_shapes: set[int] = set()
    taken_regions: set[int] = set()
    pairs: list[tuple[Any, Any]] = []
    for _score, shape, region in scored:
        if id(shape) in taken_shapes or id(region) in taken_regions:
            continue
        taken_shapes.add(id(shape))
        taken_regions.add(id(region))
        pairs.append((shape, region))
    return pairs
