"""Slides the master has no layout for, handed back rather than wrecked.

THE SLIDE THIS IS FOR. A deck's framework page was four labelled layers with
connector lines between them and a band across the middle. The master offered
nothing of that shape -- its nearest layout is a title, a subtitle, a circle
drawn as artwork and eight identical body regions stacked down the right -- so
PowerPoint's placeholder matching poured four layers of copy into eight slots
that know nothing about them. Every bullet landed on top of the diagram it
belonged to. The layout choice was the best available and the result was
unreadable, which is the case this module exists for: not a wrong pick, but a
master with no right answer in it.

WHAT IT DOES INSTEAD. Nothing. The slide is left exactly as it arrived, on its
own design, and a PowerPoint comment says why so a designer can decide. That
is the honest outcome -- a slide nobody can lay out automatically is a slide
for a person -- and it is strictly better than the alternative, because the
original is a working slide and the restyle is not.

HOW IT IS DECIDED, and why it is not decided on the layout match alone. The
matcher already says when it is guessing, and on a thin master it says so
often; quarantining every slide it doubted would hand back half the deck,
including the slides it doubted and got right. Doubt is therefore the gate and
not the verdict. The verdict is measured: the restyle is applied to a COPY of
the slide while the original is still there beside it, both are measured, and
the copy is kept only if it did not make things materially worse. A slide the
restyle handled is restyled however hesitant the pick was.

WHAT "WORSE" MEANS is the damage a reader sees -- copy written over other
copy, text spilling out of the box holding it, shapes pushed off the page --
in square inches of the page. Not a count of findings: one bullet overlapping
another by a hair and a title written across a diagram are both one finding,
and only the second is a reason to hand the slide back.

Measured through COM rather than from the file, because it has to happen
between two automation calls on a slide PowerPoint is holding -- see
`master_apply._restyle`. Nothing here raises: a measurement that cannot be
taken reads as no damage, so a slide is restyled as it would have been before
this existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

log = logging.getLogger(__name__)

_POINTS_PER_INCH = 72.0

# How much NEW damage a restyle has to do before the slide is handed back, in
# square inches of the page. A 13.33 x 7.5in slide is 100 sq in, so this is
# one percent of it: below that the restyle has shifted something slightly and
# a designer would not call it broken.
WORSE_BY_SQIN = 1.0

# And how damaged the result has to be in absolute terms. Both apply: a slide
# that arrived with copy already overlapping and came out marginally worse is
# not this module's business, and neither is a tidy slide that picked up a
# hairline collision.
WRECKED_SQIN = 2.0

COMMENT_AUTHOR = "Deck check"
COMMENT_INITIALS = "DC"

# Where the comment is anchored, in points from the top left. Out of the way
# of the copy rather than over it: a comment marker sitting on the title is
# the first thing a designer moves before they can read the slide.
_ANCHOR_PT = (12.0, 12.0)


@dataclass(frozen=True)
class Damage:
    """What a reader would see wrong with a slide, in square inches."""

    overlap: float = 0.0        # copy written over other copy
    overflow: float = 0.0       # text drawn outside the box holding it
    offcanvas: float = 0.0      # shape area past the edge of the page

    @property
    def total(self) -> float:
        return self.overlap + self.overflow + self.offcanvas

    def __str__(self) -> str:
        return (
            f"{self.total:.1f} sq in "
            f"({self.overlap:.1f} overlapping, {self.overflow:.1f} overflowing, "
            f"{self.offcanvas:.1f} off the page)"
        )


def wrecked(before: Damage, after: Damage) -> bool:
    """Whether the restyle did enough new damage to hand the slide back.

    Both tests, for the reason the constants give: the restyle has to have
    made it materially worse AND the result has to be bad in its own right.
    A slide that arrived broken is not made this module's business by being
    broken slightly differently afterwards.
    """
    return (
        after.total - before.total >= WORSE_BY_SQIN
        and after.total >= WRECKED_SQIN
    )


def measure(slide: Any, canvas_pt: tuple[float, float]) -> Damage:
    """What is visibly wrong with this slide, through COM. Never raises.

    A slide that will not answer reads as undamaged, which is the safe way
    round: it leaves the restyle exactly as it would have been.
    """
    try:
        boxes = _boxes(slide, canvas_pt)
    except Exception:
        log.debug("could not measure a slide's damage", exc_info=True)
        return Damage()
    if not boxes:
        return Damage()
    # Rounded, because these are points converted to inches and the round trip
    # leaves a sliver: a shape whose text exactly fills it measured 5e-15 sq in
    # of overflow, which is nothing at all and reads in a report as something.
    # A thousandth of a square inch is far below anything a reader could see.
    return Damage(
        overlap=round(_overlap(boxes), 3),
        overflow=round(sum(box.overflow for box in boxes), 3),
        offcanvas=round(sum(box.offcanvas for box in boxes), 3),
    )


@dataclass(frozen=True)
class _Box:
    left: float                 # inches
    top: float
    right: float
    bottom: float
    has_copy: bool
    overflow: float             # sq in of text drawn past the box
    offcanvas: float            # sq in of the box past the page

    @property
    def area(self) -> float:
        return max(0.0, self.right - self.left) * max(0.0, self.bottom - self.top)


def _boxes(slide: Any, canvas_pt: tuple[float, float]) -> list[_Box]:
    """Every top-level shape as a measured box.

    TOP LEVEL ONLY, and deliberately. A group is one drawn object: its parts
    overlap each other constantly and by design, and counting those as
    collisions would report every icon in the deck. What this is looking for
    is one object written over another.
    """
    width = canvas_pt[0] / _POINTS_PER_INCH
    height = canvas_pt[1] / _POINTS_PER_INCH
    boxes: list[_Box] = []
    for shape in _shapes(slide):
        box = _box_of(shape, width, height)
        if box is not None:
            boxes.append(box)
    return boxes


def _box_of(shape: Any, width: float, height: float) -> Optional[_Box]:
    try:
        left = float(shape.Left) / _POINTS_PER_INCH
        top = float(shape.Top) / _POINTS_PER_INCH
        right = left + float(shape.Width) / _POINTS_PER_INCH
        bottom = top + float(shape.Height) / _POINTS_PER_INCH
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None

    # The part of the shape that is off the page. A shape drawn partly past
    # the edge is normal -- a full-bleed band overshoots on purpose -- so what
    # counts is the area that is gone, not the fact of it.
    on_page = _area((max(0.0, left), max(0.0, top),
                     min(width, right), min(height, bottom)))
    full = (right - left) * (bottom - top)

    return _Box(
        left=left, top=top, right=right, bottom=bottom,
        has_copy=_has_copy(shape),
        overflow=_overflow(shape, left, top, right, bottom),
        offcanvas=max(0.0, full - on_page),
    )


def _overflow(
    shape: Any, left: float, top: float, right: float, bottom: float
) -> float:
    """How much of the shape's text is drawn outside the shape, in sq in.

    Asked of PowerPoint rather than worked out, because where text falls is
    the renderer's decision and nothing in the file says it. `BoundHeight` and
    friends are the rectangle it actually drew into, which is the same
    measurement `linemetrics.PowerPointComMetrics` reads for the orphan check.
    """
    try:
        if not int(shape.HasTextFrame):
            return 0.0
        text_range = shape.TextFrame.TextRange
        if not str(text_range.Text).strip():
            return 0.0
        drawn = (
            float(text_range.BoundLeft) / _POINTS_PER_INCH,
            float(text_range.BoundTop) / _POINTS_PER_INCH,
            float(text_range.BoundLeft + text_range.BoundWidth) / _POINTS_PER_INCH,
            float(text_range.BoundTop + text_range.BoundHeight) / _POINTS_PER_INCH,
        )
    except Exception:
        return 0.0
    inside = _area((max(drawn[0], left), max(drawn[1], top),
                    min(drawn[2], right), min(drawn[3], bottom)))
    return max(0.0, _area(drawn) - inside)


def _overlap(boxes: Sequence[_Box]) -> float:
    """Total area where one shape's copy is written over another's.

    COPY OVER COPY, not shape over shape. A card sitting on a panel and a
    label on a coloured band are how slides are drawn; two blocks of text in
    the same place are not, and only the second is a defect a designer has to
    undo. So a pair counts only when both sides carry words.

    Counted per pair, so three boxes in one place count three overlaps. That
    over-counts a genuine pile-up, which is the right direction to be wrong
    in: a pile-up is exactly what this is looking for.
    """
    total = 0.0
    carrying = [box for box in boxes if box.has_copy]
    for i, one in enumerate(carrying):
        for other in carrying[i + 1:]:
            total += _area((
                max(one.left, other.left), max(one.top, other.top),
                min(one.right, other.right), min(one.bottom, other.bottom),
            ))
    return total


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _has_copy(shape: Any) -> bool:
    try:
        return bool(int(shape.HasTextFrame)) and bool(
            str(shape.TextFrame.TextRange.Text).strip()
        )
    except Exception:
        return False


def _shapes(slide: Any):
    """A 1-based COM shape collection as an iterator, skipping what will not
    come. Local rather than shared with `master_apply` so this module can be
    measured on a plain stub."""
    try:
        count = int(slide.Shapes.Count)
    except Exception:
        return
    for i in range(1, count + 1):
        try:
            yield slide.Shapes(i)
        except Exception:
            continue


# --------------------------------------------------------------------------- #
# What the designer is told
# --------------------------------------------------------------------------- #

def comment_body(layout: Optional[str], before: Damage, after: Damage) -> str:
    """The note left on the slide, written for whoever opens the deck.

    It says what was tried, what it would have cost, and what is being asked
    of them. A comment saying only "needs attention" makes the reader work out
    what happened; the numbers are there because a designer deciding whether
    to redo a slide by hand wants to know how far off the automatic answer
    was.
    """
    tried = f"the nearest one, {layout!r}," if layout else "the nearest layout"
    return (
        "This slide was left exactly as you sent it.\n\n"
        f"No layout in the master fits it, and putting it on {tried} "
        f"made it materially worse: {after.total:.0f} sq in of the page would "
        f"have been copy written over other copy, text spilling out of its "
        f"box, or shapes pushed off the page, against {before.total:.0f} sq in "
        "before. So the restyle was discarded and the original kept.\n\n"
        "Over to you: either rebuild this slide on one of the master's "
        "layouts by hand, or add a layout to the master that suits it and run "
        "the tool again."
    )


def annotate(slide: Any, body: str) -> bool:
    """Leave the comment on the slide. Never raises.

    Best effort on purpose: the slide has already been kept by the time this
    runs, so a comment that cannot be added costs the note and not the work.
    The report names every quarantined slide either way.
    """
    try:
        slide.Comments.Add(
            _ANCHOR_PT[0], _ANCHOR_PT[1],
            COMMENT_AUTHOR, COMMENT_INITIALS, body,
        )
        return True
    except Exception:
        log.debug("could not add a comment to a quarantined slide", exc_info=True)
        return False
