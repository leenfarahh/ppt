"""One fixer per rule, for the findings a machine can correct on its own.

A fixer earns its place by being mechanical: the finding names a shape and a
target, and there is exactly one way to reach the target. `space.alignment_grid`
says the left edge should be 11.19in, so the fix is to set it to 11.19in.

Most findings are not like that. `space.overlap` names two boxes and cannot
say which of them should move; `logo.missing` needs a logo file nobody has
handed over; `title.missing` needs copy that has to be written. Those have no
fixer and are reported as needing a designer, which is the honest answer and
a much better one than a plausible guess applied to a client deck.

Every fixer works on the live python-pptx shape and returns a short line
saying what it changed, or None when it found nothing to do. A fixer never
raises: a fix that cannot be made is reported, not fatal.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from ..colorutil import TEXT_CONTRAST_FLOOR, contrast_ratio
from ..models import Issue
from ..rules.space import DECLARED_GRID

log = logging.getLogger(__name__)

EMU_PER_INCH = 914400


class LeaveAlone(Exception):
    """Raised by a fixer that has decided this shape must not be touched.

    Distinct from returning None, which means "already correct". This means
    "correctable in principle, and doing it would be wrong here", and the
    reason travels with it onto the report so the designer knows why the fix
    they ticked did not happen.
    """


# Fixes that move a shape. They share a hazard the text fixers do not have: a
# slide is a composition, and a shape nudged to satisfy one rule lands on its
# neighbour. Every one of these is checked against the shapes around it before
# the move is allowed to stand.
GEOMETRIC = frozenset(
    {
        "space.off_canvas",
        "space.safe_margin",
        "space.alignment_grid",
        "space.repeat_out_of_line",
        "space.overlap",
        "space.row_out_of_line",
        "space.satellite_offset",
        "title.position_inconsistent",
        "logo.geometry",
    }
)

# Geometric fixes measured as a delta from where the shape was when the report
# was written, rather than as a position to move to. Every other fix names an
# absolute target and is therefore idempotent -- run it twice and the second
# run finds the shape already correct. These cannot be: the delta is only true
# of the position it was measured against, so a second fix that has already
# moved the shape makes this one wrong by exactly the amount of the first.
#
# On a real deck `space.repeat_out_of_line` and `space.satellite_offset` both
# fired on one shape, describing the same 0.15in drift. The edge fix ran first
# and corrected it; this one then subtracted the same 0.15in again from the
# corrected position. The alignment guard caught the result, but it caught it
# as "would break an alignment", which tells the designer nothing about what
# actually went wrong.
RELATIVE = frozenset({"space.satellite_offset"})

# Fixes allowed to move a shape's whole alignment set rather than refuse.
#
# The hard constraints only, and that is the whole distinction. A deck cannot
# ship with content outside the frame, so when a column of shapes is outside
# it together, the column moves together -- shapes no finding named included,
# because a column half corrected is worse than one uncorrected.
#
# A grid snap is a preference, and dragging a neighbour to satisfy a
# preference is the failure that got `space.alignment_grid` disabled once
# already: a section label pulled off the table it captioned, and with a set
# move it would take the table with it. So these stay single-shape moves and
# still refuse when they would break an alignment.
COHORT = frozenset({"space.safe_margin", "space.off_canvas"})

# Whitespace that is safe to strip from the end of a paragraph. A vertical tab
# is a soft line break, which is `typography.manual_line_break`'s business and
# a deliberate act often enough that removing it here would be a surprise.
_TRAILING = " \t   "

_INCHES = re.compile(r"(-?\d+(?:\.\d+)?)\s*in")
_EDGE = re.compile(r"\b(left|right|top)\s+(-?\d+(?:\.\d+)?)\s*in")
_MARGIN = re.compile(r"\b(top|right|bottom|left)\s+(-?\d+(?:\.\d+)?)\s*in")
_QUOTED_FONT = re.compile(r"explicit\s+(.+?)\s*$")
# "0.42, 1.19in", and the signed form "offset +3.04, +0.00in". Both numbers and
# the unit together, so a lone measurement elsewhere in the same sentence
# cannot be read as half a point.
_POINT = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*in")
# "centre y 3.00in, as the rest of the row" -> ("y", 3.00). A row lines up on
# its middle, not on an edge, so this is a different thing to read than
# `_EDGE`.
_CENTRE = re.compile(r"centre\s+([xy])\s+([-+]?\d+(?:\.\d+)?)\s*in", re.IGNORECASE)
_N = r"([-+]?\d+(?:\.\d+)?)"
# "clear of 'Tab 0' at 1.00, 0.40, 1.90 x 0.40in"
_RECT = re.compile(
    rf"at\s+{_N}\s*,\s*{_N}\s*,\s*{_N}\s*x\s*{_N}\s*in", re.IGNORECASE
)
# "evenly spaced across 1.00-8.90in, gap 0.38in"
_SPREAD = re.compile(
    rf"across\s+{_N}\s*-\s*{_N}\s*in.*?gap\s+{_N}\s*in", re.IGNORECASE
)
# "theme:accent1 #C00000" -> C00000, and "#DADADA" -> DADADA. Six hex digits
# anchored on the hash, so a delta-E or a shape name in the same sentence
# cannot be mistaken for a colour.
_HEX = re.compile(r"#([0-9A-Fa-f]{6})(?![0-9A-Fa-f])")
# "theme:accent1 #A32020" on a finding's `found` -- the colour is read from a
# slot rather than set on the shape, which changes who can fix it.
_THEME_BOUND = re.compile(r"^theme:\w+\s+#", re.IGNORECASE)
# "graphic #A32020" -- a colour inside an icon's SVG rather than a fill on the
# shape, which changes how it is written and not just what it is set to.
_GRAPHIC = re.compile(r"^graphic\s+#", re.IGNORECASE)
# The rule's mark for "this target is the nearest entry, not the one the
# colour was meant to be". See rules.colors._target.
_FALLBACK = re.compile(r"^nearest\s", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def fix_off_canvas(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Bring a shape that straddles an edge back inside the canvas.

    Nudged rather than resized: the shape's proportions are a design decision
    and its position usually is not, and a shape too large for the canvas is
    left alone because shrinking it would be the bigger change.

    A shape lying *wholly* off the slide is a different thing entirely and is
    never moved. It is invisible where it is, so nobody is looking at a
    defect; it is either parked there on purpose or left over from an edit,
    and dragging it into view does not repair a slide, it adds junk to one. On
    a real deck this pulled two stray diagram fragments on top of an icon.
    """
    width, height = ctx.width_emu, ctx.height_emu
    left, top = shape.left, shape.top
    if left is None or top is None:
        return None

    box_w = shape.width or 0
    box_h = shape.height or 0
    if box_w > width or box_h > height:
        return None

    if left + box_w <= 0 or left >= width or top + box_h <= 0 or top >= height:
        raise LeaveAlone(
            "it sits entirely off the slide, so it is parked or left over; "
            "whether to reposition or delete it is a designer's call"
        )

    new_left = min(max(left, 0), width - box_w)
    new_top = min(max(top, 0), height - box_h)
    if (new_left, new_top) == (left, top):
        return None

    shape.left, shape.top = new_left, new_top
    return (
        f"moved {(new_left - left) / EMU_PER_INCH:+.2f}, "
        f"{(new_top - top) / EMU_PER_INCH:+.2f}in back onto the canvas"
    )


def fix_alignment_grid(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Snap the left edge to the grid line the finding names.

    Registered, after a spell disabled. `space.alignment_grid` used to find
    the edge the most shapes on a slide share, and the busiest one is not
    necessarily the one a given shape belongs to: on a real deck this snapped
    a section label 0.20in away from the table it captioned and onto a grid
    line borrowed from the other half of the slide.

    Two things answer that now. The grid can come from the master rather than
    from the deck being audited, so the candidate lines are a handful the
    designer drew instead of every habit four shapes share. And the applier
    refuses a move that ends an alignment the shape already had, which is
    exactly what the label-and-table failure was: no new overlap, a broken
    relationship. See `applier._aligned`.

    Neither makes "which column" certain, so this refuses to act on an
    INFERRED grid at all, and the move is bounded by the near-miss window the
    rule reports in: at most four position tolerances, 0.20in by default.

    The refusal is not caution for its own sake. Run against an inferred grid
    on a real deck, this snapped a 5.48in section header 0.20in onto a line
    whose entire support was five 1.67in pentagon labels elsewhere on the
    slide -- the original failure, reproduced. A line four shapes happen to
    share is not a column; a line the master draws is.
    """
    if DECLARED_GRID not in (issue.message or ""):
        raise LeaveAlone(
            "the grid line here is inferred from this deck's own habits, not "
            "declared by the master, and which column a shape belongs to is "
            "a design call. Mark the master's presentation space to enable this"
        )
    side, target = _edge(issue.expected)
    if target is None or shape.left is None:
        return None

    # The edge the deck aligns on, which is the right one in a deck that reads
    # right to left. Snapping the left edge of a right-aligned shape moves it
    # by its own width off the column it belongs to.
    width = shape.width or 0
    new_left = int(round(target * EMU_PER_INCH)) - (width if side == "right" else 0)
    if new_left == shape.left:
        return None
    moved = (new_left - shape.left) / EMU_PER_INCH
    shape.left = new_left
    return f"snapped the {side or 'left'} edge {moved:+.2f}in to {target:.2f}in"


def fix_repeat_out_of_line(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Put one member of a series back on the edge the rest of it shares.

    The finding already did the judging: it found the series, established
    which edge the majority agrees on, and satisfied itself that this member
    is near enough for the difference to be drift. All that is left is to move
    it, which is the same act as aligning the set by hand in PowerPoint.
    """
    side, target = _edge(issue.expected)
    if side is None or target is None:
        return None

    current = shape.left if side == "left" else shape.top
    if current is None:
        return None
    wanted = int(round(target * EMU_PER_INCH))
    if wanted == current:
        return None

    moved = (wanted - current) / EMU_PER_INCH
    if side == "left":
        shape.left = wanted
    else:
        shape.top = wanted
    return f"aligned the {side} edge {moved:+.2f}in onto the set at {target:.2f}in"


def fix_satellite_offset(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Put one copy of a repeated pairing back on the offset its cohort shares.

    The finding carries both numbers -- the offset the majority sits at and
    the offset this one sits at -- so the move is their difference and nothing
    here has to decide anything. Applied to the shape's own left and top
    rather than to its centre: the offsets are centre to centre, but the
    satellite is not being resized and its partner is not moving, so the two
    deltas are the same number.

    The partner could itself be moved later in the run, which would reopen the
    gap. It cannot be moved *earlier*: this shares its rank with the other
    series fix, and both run before the canvas and margin clamps, which are
    the only fixes that touch a shape no finding named.
    """
    expected = _point(issue.expected)
    found = _point(issue.found)
    if expected is None or found is None:
        return None
    if shape.left is None or shape.top is None:
        return None

    dx = _emu(found[0] - expected[0])
    dy = _emu(found[1] - expected[1])
    if not dx and not dy:
        return None

    shape.left -= dx
    shape.top -= dy
    return (
        f"moved {-dx / EMU_PER_INCH:+.2f}, {-dy / EMU_PER_INCH:+.2f}in back "
        "onto the offset the rest of the set shares"
    )


def fix_title_position(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Move a drifting title onto the position the rest of the deck's titles hold.

    The rule only reports per slide once a real majority agrees on a position,
    so "which one is right" was settled before this ran; what is left is the
    move, which is the same act as typing the modal left and top into the
    position box.

    Bounded, though. A title several inches from the modal position is not
    drift, it is a different arrangement -- a section divider, a cover -- that
    happens to use a title placeholder, and dragging it up to join the content
    slides would wreck the slide to satisfy a warning.
    """
    return _move_onto(shape, issue, ctx, "the title", "the rest of the deck")


def fix_logo_geometry(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Move a logo back onto the master's position.

    Only the position half of this rule. The other two things it reports are
    not mechanical: a logo below the minimum width has to be scaled, and
    growing it pushes into whatever the designer left around it, which the
    move guard cannot see because the guard compares positions; and which
    corner a logo belongs in is the composition's business, not arithmetic's.
    Both say so rather than failing silently.
    """
    if _point(issue.expected) is None:
        if "wide" in (issue.expected or ""):
            raise LeaveAlone(
                "scaling the logo up to the minimum width changes the space "
                "the designer left around it, which is a composition change "
                "rather than a correction"
            )
        raise LeaveAlone("which corner the logo belongs in is a design call")
    return _move_onto(shape, issue, ctx, "the logo", "the master")


def _move_onto(
    shape: Any,
    issue: Issue,
    ctx: "FixContext",
    what: str,
    whose: str,
) -> Optional[str]:
    """Move a shape onto the "left, topin" position a finding names.

    Shared by the two fixes that have an exact destination rather than a
    delta. The drift ceiling is the one the repeat rules use, and for the same
    reason: past it the shape was put there, not left there.
    """
    target = _point(issue.expected)
    if target is None or shape.left is None or shape.top is None:
        return None

    wanted_left, wanted_top = _emu(target[0]), _emu(target[1])
    if (wanted_left, wanted_top) == (shape.left, shape.top):
        return None

    dx = (wanted_left - shape.left) / EMU_PER_INCH
    dy = (wanted_top - shape.top) / EMU_PER_INCH
    if max(abs(dx), abs(dy)) > ctx.max_drift_in:
        raise LeaveAlone(
            f"it sits {max(abs(dx), abs(dy)):.2f}in away, too far to be drift; "
            "a slide that places its content this differently is doing so on "
            "purpose"
        )

    shape.left, shape.top = wanted_left, wanted_top
    return (
        f"moved {what} {dx:+.2f}, {dy:+.2f}in onto the position {whose} holds"
    )


def fix_overlap(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Nudge the shape that is on top clear of the one under it.

    Two boxes overlap and the geometry cannot say which is in the wrong place,
    which is why this had no fixer for so long. What it can say is which one
    landed on the other: z-order records that, and `space.overlap` now reports
    the finding on the shape in front. So the one that moves is the one that
    arrived last, which is the same call a designer makes without thinking
    about it.

    The shortest way out, along one axis. A diagonal nudge clears the same
    collision by moving further and lands the shape somewhere neither box
    suggested; pushing straight off the nearest edge keeps whatever alignment
    the shape had on the other axis.
    """
    box = _rect(issue.expected)
    if box is None:
        return None
    if shape.left is None or shape.top is None:
        return None

    left, top = shape.left, shape.top
    width, height = shape.width or 0, shape.height or 0
    other_left, other_top = _emu(box[0]), _emu(box[1])
    other_right, other_bottom = other_left + _emu(box[2]), other_top + _emu(box[3])

    if left >= other_right or left + width <= other_left:
        return None
    if top >= other_bottom or top + height <= other_top:
        return None

    clear = _emu(_CLEARANCE_IN)
    across = ((other_right - left + clear, 0), (other_left - (left + width) - clear, 0))
    down = ((0, other_bottom - top + clear), (0, other_top - (top + height) - clear))

    # The shortest way out, but not along an axis the two shapes fully share.
    # Two boxes side by side at the same height overlap their whole height, so
    # "shortest" is downward -- and moving down clears the collision by taking
    # the shape out of the row it belongs to. Full overlap on an axis is not a
    # collision on that axis, it is an alignment, and the move guard would
    # refuse the result anyway.
    ways = []
    if not _fully_overlapped(left, width, other_left, other_right - other_left):
        ways.extend(across)
    if not _fully_overlapped(top, height, other_top, other_bottom - other_top):
        ways.extend(down)
    if not ways:
        ways = list(across) + list(down)
    dx, dy = min(ways, key=lambda way: abs(way[0]) + abs(way[1]))

    new_left, new_top = left + dx, top + dy
    if not (
        0 <= new_left and new_left + width <= ctx.width_emu
        and 0 <= new_top and new_top + height <= ctx.height_emu
    ):
        raise LeaveAlone(
            "the only way clear of the shape under it runs off the slide, so "
            "one of the two has to be resized or its copy shortened"
        )

    shape.left, shape.top = new_left, new_top
    return (
        f"moved {dx / EMU_PER_INCH:+.2f}, {dy / EMU_PER_INCH:+.2f}in clear of "
        "the shape underneath, being the one on top of it"
    )


# A hair of daylight, so a shape pushed clear does not come back next run as
# touching to the nearest EMU.
_CLEARANCE_IN = 0.02


def _fully_overlapped(a: int, a_size: int, b: int, b_size: int) -> bool:
    """Whether one shape's extent on this axis covers the other's entirely.

    Two boxes in a row share their whole height; a caption and the chart above
    it share their whole width. Separating along that axis is not clearing a
    collision, it is leaving the arrangement.
    """
    span = min(a + a_size, b + b_size) - max(a, b)
    return span >= min(a_size, b_size) - _emu(0.01)


def fix_series_crowded(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Spread a collapsed row evenly across the space it already occupies.

    What Distribute Horizontally does, and the only thing that works on a row
    where every pair overlaps: push the second tab clear of the first and it
    lands on the third, which is exactly what the move guard refuses. The set
    has to be spread at once.

    Safe in a way a single move is not, and that is why it may touch shapes no
    finding named: distributing inside the row's existing span never grows the
    row's footprint, so it cannot land on anything outside it. The leftmost
    shape -- the one the finding names -- does not move at all.
    """
    start, span, gap = _spread(issue.expected)
    if start is None or span is None or gap is None:
        return None
    if gap < 0:
        raise LeaveAlone(
            f"the shapes are wider than the row they sit in, so spreading "
            f"them still leaves {abs(gap):.2f}in of overlap; they have to be "
            "narrowed or the row widened"
        )

    row = _row_with(shape, ctx.neighbours)
    if len(row) < 2:
        return None

    cursor = _emu(start)
    moved = 0
    for member in row:
        if member.left != cursor:
            member.left = cursor
            moved += 1
        cursor += (member.width or 0) + _emu(gap)
    return (
        f"spread {len(row)} shapes evenly across {start:.2f}-{span:.2f}in "
        f"with a {gap:.2f}in gap, moving {moved} of them"
    )


def _row_with(shape: Any, neighbours: list) -> list:
    """The shape and the same-sized shapes sharing its row, left to right."""
    width, height = shape.width, shape.height
    centre = (shape.top or 0) + (shape.height or 0) / 2
    slack = _emu(_ROW_SLACK_IN)
    row = [shape] + [
        other for other in neighbours
        if other.width == width and other.height == height
        and abs(((other.top or 0) + (other.height or 0) / 2) - centre) <= slack
    ]
    return sorted(row, key=lambda s: s.left or 0)


# How far a member's centre can sit from the row's and still be in the row.
_ROW_SLACK_IN = 0.2


def fix_row_out_of_line(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Put a shape back on the centre line its row or column shares.

    The same act as `space.repeat_out_of_line`, one axis over. That rule reads
    a shared left or top edge; this one reads a shared centre, because a row
    of shapes of different heights lines up on its middle and not on its top.

    The row already did the judging: three or more members agreeing is the
    intent and this one is the exception, which is why the rule reports
    nothing for a row of two -- there the two disagree and neither is a
    majority. Those are `space.mirror_pair_offset`, and they go to a designer.
    """
    axis, target = _centre_line(issue.expected)
    if axis is None or target is None:
        return None

    side = "left" if axis == "x" else "top"
    extent = shape.width if axis == "x" else shape.height
    current = getattr(shape, side)
    if current is None or extent is None:
        return None

    wanted = int(round(target * EMU_PER_INCH - extent / 2))
    if wanted == current:
        return None
    moved = (wanted - current) / EMU_PER_INCH
    setattr(shape, side, wanted)
    return (
        f"moved {moved:+.2f}in onto the centre {axis} of {target:.2f}in "
        "the rest of the set shares"
    )


def fix_safe_margin(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Move a shape back inside the safe margin, the shortest distance.

    Moved, never resized: the box was drawn at that width on purpose, and a
    shape too big for the usable area cannot be brought inside it by nudging,
    so it is left for a person. Only the edges the finding actually names are
    corrected, so a box crossing the right margin is not also dragged down off
    a top margin it was never breaching.
    """
    margins = _margins(issue.expected)
    if not margins:
        return None

    left, top = shape.left, shape.top
    width, height = shape.width or 0, shape.height or 0
    if left is None or top is None:
        return None

    usable_w = ctx.width_emu - _emu(margins.get("left")) - _emu(margins.get("right"))
    usable_h = ctx.height_emu - _emu(margins.get("top")) - _emu(margins.get("bottom"))
    if width > usable_w or height > usable_h:
        return None

    new_left, new_top = left, top
    if "left" in margins:
        new_left = max(new_left, _emu(margins["left"]))
    if "right" in margins:
        new_left = min(new_left, ctx.width_emu - _emu(margins["right"]) - width)
    if "top" in margins:
        new_top = max(new_top, _emu(margins["top"]))
    if "bottom" in margins:
        new_top = min(new_top, ctx.height_emu - _emu(margins["bottom"]) - height)

    if (new_left, new_top) == (left, top):
        return None
    shape.left, shape.top = new_left, new_top
    return (
        f"moved {(new_left - left) / EMU_PER_INCH:+.2f}, "
        f"{(new_top - top) / EMU_PER_INCH:+.2f}in inside the safe margin"
    )


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #
#
# Both of these snap to the nearest palette entry, which the finding already
# names: the rule measured the distance in CIEDE2000 to decide the colour was
# off-palette at all, so the nearest entry is a number it has in hand rather
# than a guess made here.
#
# What they do NOT do is pick between two entries a colour sits between. The
# rule reports only the nearest, and "nearest" is the whole claim; where the
# brand meaning matters more than the distance -- an accent that should have
# been the other accent -- the recolour is wrong in a way no palette maths can
# see. That is why the finding stays on the report after the fix lands, so a
# designer sees which colours were moved and where to.


def _refuse_theme_bound(issue: Issue) -> None:
    """Stand down on a colour the shape does not own.

    A theme-bound colour carries no RGB: the shape reads a slot, and the slot
    is right. What is wrong is the theme the deck brought with it from the
    file it was built in, and writing a literal colour onto the shape would
    hide that while leaving every other shape reading the same slot untouched
    -- and would survive a later rebuild as a hardcoded exception to a palette
    that had since been corrected.
    """
    if _THEME_BOUND.search(issue.found or ""):
        raise LeaveAlone(
            "this colour is bound to the theme, so the shape is not what is "
            "wrong: the deck carries its own theme. Rebuild onto the master, "
            "which replaces it and corrects every colour bound to it at once"
        )


def _refuse_untargeted(issue: Issue) -> None:
    """Stand down on a finding that names no colour at all to move to.

    Which is now only the empty-palette case: with nothing to measure
    against, `expected` is the bare words "brand palette" and there is no hex
    in it to apply.

    A colour the rule found no INTENDED entry for is no longer refused here.
    The rule falls back to the nearest entry and marks it, and this applies
    it. See `_target` for why that fallback is wanted and `_is_fallback` for
    what the marker buys.
    """
    if _hex_of(issue.expected) is None:
        raise LeaveAlone(
            "the palette names no colour to move this to, so which entry "
            "replaces it is a design call"
        )


def _is_fallback(issue: Issue) -> bool:
    """Whether the target is the nearest entry rather than the intended one.

    The rule prefixes the fallback with `nearest`. It matters in the outcome
    line and nowhere else: a designer scanning forty applied fixes needs the
    two that changed a colour's hue to stand out from the thirty-eight that
    snapped a near-miss into place.
    """
    return bool(_FALLBACK.search(issue.expected or ""))


def _target_phrase(issue: Issue, target: str, plan_note: str = "") -> str:
    """How the outcome line describes the colour it moved to."""
    if plan_note:
        return f"#{target} ({plan_note})"
    if _is_fallback(issue):
        return (
            f"#{target}, the nearest palette entry -- it reads as a different "
            "colour, so check it"
        )
    return f"the palette entry #{target}"


# Defined in colorutil so the plan that CHOOSES a colour and the fixer that
# APPLIES it cannot drift apart on what counts as legible.
_CONTRAST_FLOOR = TEXT_CONTRAST_FLOOR


def _planned_target(issue: Issue, ctx: "FixContext") -> tuple[Optional[str], str]:
    """The colour to move to, and a note for the outcome line.

    The plan is consulted before the finding, because the finding was written
    one shape at a time and the plan is the only thing that saw the whole
    deck. Where there is no plan -- no palette, or a caller assembling issues
    by hand -- the finding's own target stands, which is what the CLI and the
    tests do.

    A plan that declines this colour raises rather than returning None. It is
    a decision, not a missing value, and the reason belongs in front of the
    designer: their three-step legend is why this shape was left alone.
    """
    plan = getattr(ctx, "color_plan", None)
    if plan is None:
        return _hex_of(issue.expected), ""
    choice = plan.choice_for(_hex_of(issue.found))
    if choice is None:
        return _hex_of(issue.expected), ""
    if not choice.applies:
        raise LeaveAlone(choice.reason)
    _PLANNED[id(issue)] = choice
    return choice.target, choice.reason


# The plan's choice for the finding currently being fixed, so the fill fixer
# can tell a colour the plan picked KNOWING it costs legibility from one it
# picked without looking. Keyed by the issue's identity and read once,
# immediately, on the same call: the alternative was widening every fixer's
# signature to carry a value only two of them use.
_PLANNED: dict[int, Any] = {}


def _plan_accepted_the_risk(issue: Issue) -> bool:
    """Whether the plan chose this colour knowing the text would suffer.

    When it did, the fill fixer must not overrule it. The plan searched the
    whole palette for a legible entry and there was none, so vetoing here
    would put the colour back off-palette -- trading a defect a designer is
    told about for one the brand check exists to remove.
    """
    choice = _PLANNED.pop(id(issue), None)
    return bool(choice is not None and getattr(choice, "text_at_risk", False))


def _refuse_illegible(shape: Any, target: str, issue: Issue) -> None:
    """Stand down on a fill that would leave the text on it unreadable.

    Delta-E says whether two colours read as the same colour. It says nothing
    about whether text in one can be read on the other: a mid grey pill and a
    navy pill are far apart in Lab, and dark text is legible on one and
    invisible on the other. Snapping a pale fill to a dark palette entry is
    exactly the move that does this, and it is a defect a client sees before
    they see anything else the tool corrected.

    Only measured against text the shape actually carries and that carries its
    own colour. An inherited or theme-bound run is not something this fix is
    in a position to judge, and guessing at it would decline good fixes.
    """
    if not _has_text(shape):
        return
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if not run.text.strip():
                continue
            ink = _run_hex(run)
            if not ink:
                continue
            after = contrast_ratio(target, ink)
            if after is None or after >= _CONTRAST_FLOOR:
                continue
            before = contrast_ratio(_hex_of(issue.found) or ink, ink)
            # Only refuse what this fix would BREAK. A shape whose text was
            # already unreadable is a finding of its own, and holding the
            # recolour hostage to it would leave the colour wrong as well.
            if before is not None and before < _CONTRAST_FLOOR:
                continue
            raise LeaveAlone(
                f"recolouring the fill to #{target} would leave its text "
                f"(#{ink}) at {after:.1f}:1 contrast, under the {_CONTRAST_FLOOR}:1 "
                f"floor, where it reads {before:.1f}:1 now"
            )


def fix_off_palette_text(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Recolour the runs that carry the off-palette colour, and only those.

    Matched on the colour the finding measured, not on every run in the shape:
    a shape routinely holds one run in the brand navy and one somebody typed
    over in black, and the finding is about the second.
    """
    _refuse_theme_bound(issue)
    _refuse_untargeted(issue)
    target, note = _planned_target(issue, ctx)
    current = _hex_of(issue.found)
    if not target or not current or not _has_text(shape):
        return None

    from pptx.dml.color import RGBColor  # noqa: PLC0415 - lazy heavy dependency

    changed = 0
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if _run_hex(run) != current:
                continue
            run.font.color.rgb = RGBColor.from_string(target)
            changed += 1
    if not changed:
        return None
    return (
        f"recoloured {changed} run(s) from #{current} to "
        f"{_target_phrase(issue, target, note)}"
    )


def fix_off_palette_shape(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Recolour a shape's fill, its outline, or the SVG an icon draws from."""
    _refuse_theme_bound(issue)
    _refuse_untargeted(issue)
    target, note = _planned_target(issue, ctx)
    if not target:
        return None
    if _GRAPHIC.search(issue.found or ""):
        return _recolor_icon(shape, issue, target, note)
    wants_outline = "outline" in (issue.message or "").lower()

    from pptx.dml.color import RGBColor  # noqa: PLC0415 - lazy heavy dependency

    colour = RGBColor.from_string(target)
    if wants_outline:
        # An outline carries no text, so nothing on the shape becomes
        # unreadable by moving it.
        shape.line.color.rgb = colour
        return f"recoloured the outline to {_target_phrase(issue, target, note)}"
    if not _plan_accepted_the_risk(issue):
        _refuse_illegible(shape, target, issue)
    # solid() first: a shape whose fill is inherited or themed has no fore
    # colour to set until it has been made a solid fill of its own.
    shape.fill.solid()
    shape.fill.fore_color.rgb = colour
    return f"recoloured the fill to {_target_phrase(issue, target, note)}"


def _recolor_icon(
    shape: Any, issue: Issue, target: str, note: str = ""
) -> Optional[str]:
    """Rewrite the colour inside an icon's SVG.

    An icon is a `p:pic`, so it has no fill to set: PowerPoint calls this a
    Graphics Fill and the colour lives inside the SVG part the picture points
    at. Setting `shape.fill` on one does nothing at all -- verified against
    desktop PowerPoint, which writes byte-identical XML for it -- which is why
    icons stayed the wrong colour however the finding was worded.

    The SVG part is shared by every copy of the same icon, so recolouring one
    recolours all of them. That is almost always what a designer wants from a
    row of identical icons and is said in the outcome either way, because
    "changed 1 shape" would be a lie about a file where six moved.
    """
    from .. import svgicon  # noqa: PLC0415 - keeps the import off the hot path

    part = svgicon.svg_part(shape)
    if part is None:
        raise LeaveAlone(
            "this icon is not an SVG, so its colour is baked into the image; "
            "replacing it is a designer's call"
        )
    current = _hex_of(issue.found)
    if not current:
        return None
    try:
        markup = part.blob.decode("utf-8", "ignore")
        rewritten, changed = svgicon.recolor(markup, current, target)
        if not changed:
            return None
        part._blob = rewritten.encode("utf-8")
    except Exception as exc:
        raise LeaveAlone(f"the icon's drawing could not be rewritten: {exc}") from exc
    warning = (
        f" -- {note}" if note else
        " -- the nearest entry, which reads as a different colour, so check it"
        if _is_fallback(issue) else ""
    )
    return (
        f"recoloured the icon from #{current} to #{target} "
        f"({changed} statement(s) in its drawing, which every copy of this "
        f"icon shares){warning}"
    )


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #

def fix_whitespace(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Strip trailing spaces and collapse runs of spaces inside a line.

    Works run by run from the end of each paragraph, because the trailing
    space usually sits in the last run and only that run's text should change.
    """
    if not _has_text(shape):
        return None

    trimmed = 0
    collapsed = 0
    for paragraph in shape.text_frame.paragraphs:
        runs = list(paragraph.runs)
        for run in reversed(runs):
            stripped = run.text.rstrip(_TRAILING)
            if stripped != run.text:
                trimmed += len(run.text) - len(stripped)
                run.text = stripped
            if stripped:
                break        # the paragraph now ends in real text
        for run in runs:
            squeezed = re.sub(r"  +", " ", run.text)
            if squeezed != run.text:
                collapsed += 1
                run.text = squeezed

    parts = []
    if trimmed:
        parts.append(f"{trimmed} trailing character(s)")
    if collapsed:
        parts.append(f"repeated spaces in {collapsed} run(s)")
    return "removed " + " and ".join(parts) if parts else None


def fix_manual_line_break(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Replace soft returns with a space so the text wraps to its box.

    A space rather than nothing: the break is almost always sitting between
    two words that would otherwise run together.
    """
    if not _has_text(shape):
        return None

    from pptx.oxml.ns import qn  # noqa: PLC0415 - lazy, python-pptx is optional

    removed = 0
    for paragraph in shape.text_frame.paragraphs:
        for br in list(paragraph._p.findall(qn("a:br"))):
            previous = br.getprevious()
            if previous is not None and previous.tag == qn("a:r"):
                node = previous.find(qn("a:t"))
                if node is not None and node.text and not node.text.endswith(" "):
                    node.text += " "
            paragraph._p.remove(br)
            removed += 1
    return f"removed {removed} soft return(s)" if removed else None


def fix_terminal_punctuation(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Take the full stop off the end of a title.

    Registered where the rest of `typography` is not, because this is not a
    copy edit. A title is a label, not a sentence, and the full stop on the
    end of one is a typing artefact rather than something a writer chose --
    which is why the rule only fires on the title role in the first place.
    Nothing about the wording changes, and putting it back is one keystroke.

    An ellipsis is the exception and is left alone: three dots at the end of a
    title are always deliberate, and stripping one of them would turn a chosen
    mark into a typo.
    """
    if not _has_text(shape):
        return None

    paragraphs = [p for p in shape.text_frame.paragraphs if p.text.strip()]
    if not paragraphs:
        return None

    # The last run with text in it, which is where the stop sits. Whitespace-
    # only runs after it are skipped rather than trusted, because a title
    # ending "Title. " keeps its stop in the run before the space.
    for run in reversed(list(paragraphs[-1].runs)):
        text = run.text.rstrip(_TRAILING)
        if not text:
            continue
        if text.endswith("...") or text.endswith("…"):
            raise LeaveAlone(
                "the title ends in an ellipsis, which is a mark somebody chose"
            )
        if not text.endswith("."):
            return None
        run.text = text[:-1]
        return "removed the full stop from the end of the title"
    return None


def fix_orphan_widow(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Bind the last two words together so the wrap takes both down.

    A non-breaking space, which is what a typesetter does and the only one of
    the obvious options that is not a design decision:

    - Widening the box changes the composition, and re-wraps the whole
      paragraph, so it may strand a different word instead of no word.
    - Editing the copy is the writer's call.
    - "Pull the last word back onto the previous line" is not a separate
      option; it is what this does. A word cannot be moved without changing
      where the line breaks, and the break is what a non-breaking space moves.

    One character, no words changed, and undone by deleting it. What it does
    not do is guarantee the result: binding two words can push both onto the
    last line and leave two words there, or -- on a very narrow box -- push a
    longer stub down. The second pass re-measures the deck that was written,
    with the same renderer, which is where that is caught.

    Only the stranded-word finding. The same rule reports a short last line
    and a title wrapping past its limit, and neither is fixed by binding two
    words: a stub is a stub whichever line it sits on.
    """
    if not _ORPHAN.search(issue.message or ""):
        raise LeaveAlone(
            "this is a short last line or a title wrapping too far, not a "
            "stranded word; binding two words does not answer either"
        )
    if not _has_text(shape):
        return None

    stranded = (issue.found or "").strip()
    paragraph = _paragraph_ending_with(shape, stranded)
    if paragraph is None:
        raise LeaveAlone(
            "the copy has changed since the report was made, so the line this "
            "names is no longer there"
        )
    if not _bind_last_two_words(paragraph):
        raise LeaveAlone(
            "there is only one word to bind, so nothing can be brought down "
            "with it"
        )
    return (
        f"bound {stranded!r} to the word before it with a non-breaking space, "
        "so the two wrap together"
    )


# "Last line is a single stranded word ('everywhere')." -- the one variant of
# this rule a non-breaking space answers.
_ORPHAN = re.compile(r"single stranded word", re.IGNORECASE)

# U+00A0. The whole fix, and the reason it is safe: one character, no words
# changed, removed by deleting it.
_NBSP = "\u00a0"


def _paragraph_ending_with(shape: Any, stranded: str) -> Optional[Any]:
    """The paragraph whose copy ends in the stranded word the finding names."""
    if not stranded:
        return None
    for paragraph in reversed(list(shape.text_frame.paragraphs)):
        if (paragraph.text or "").rstrip().endswith(stranded):
            return paragraph
    return None


def _bind_last_two_words(paragraph: Any) -> bool:
    """Replace the space before the final word with a non-breaking one.

    Runs make this fiddlier than it reads. The space and the word it precedes
    are often in different runs -- a bolded last word is enough to split them
    -- so the space is looked for inside the final run first and in the run
    before it second.
    """
    runs = [run for run in paragraph.runs if run.text]
    if not runs:
        return False

    last = runs[-1]
    stripped = last.text.rstrip()
    cut = stripped.rfind(" ")
    if cut > 0:
        last.text = stripped[:cut] + _NBSP + stripped[cut + 1:]
        return True

    # The final word is a run of its own; the space is the tail of the one
    # before it.
    for run in reversed(runs[:-1]):
        if run.text.endswith(" "):
            run.text = run.text[:-1] + _NBSP
            return True
        if run.text.strip():
            return False        # two words with no space between them
    return False


def fix_rtl_not_set(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Mark the Arabic paragraphs right-to-left.

    One attribute, `a:pPr/@rtl`, and it changes no words, no typeface and no
    size. What it changes is where everything that is not a letter goes: the
    full stop at the end of the sentence, a bracketed aside, a number, a Latin
    product name inside an Arabic line. Without it the letters still shape and
    join -- that is the font's job -- so the slide looks almost right, which
    is exactly why this ships.

    Only the paragraphs that are actually Arabic. A bilingual shape holding an
    English heading over an Arabic body has one of each, and turning the
    heading round would be the same defect pointed the other way.
    """
    from ..script import is_rtl  # noqa: PLC0415 - keeps the import off the hot path

    if not _has_text(shape):
        return None

    from pptx.oxml.ns import qn  # noqa: PLC0415 - lazy, python-pptx is optional

    changed = 0
    for paragraph in shape.text_frame.paragraphs:
        if not is_rtl(paragraph.text or ""):
            continue
        properties = paragraph._p.get_or_add_pPr()
        if properties.get("rtl") in ("1", "true"):
            continue
        properties.set("rtl", "1")
        changed += 1
    if not changed:
        return None
    return (
        f"marked {changed} Arabic paragraph(s) right-to-left, so their "
        "punctuation and numbers land where they belong"
    )


def fix_arabic_font(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Set the Arabic runs in the brand's Arabic face.

    Applied only when the brand declares ONE Arabic face, which is the common
    case and the only one where the answer is not a choice. With two or more
    it is the same design call `font.family.unapproved` leaves to a person:
    which of the approved faces this copy should be in.

    Every run holding Arabic, not every run in the shape. A shape carrying an
    English label and an Arabic caption needs one of them changed, and
    rewriting the English into an Arabic face would be a defect this rule
    would then report from the other side.
    """
    from ..script import has_arabic  # noqa: PLC0415

    approved = [name.strip() for name in (issue.expected or "").split(",")]
    approved = [name for name in approved if name]
    if len(approved) != 1:
        raise LeaveAlone(
            "the brand declares more than one Arabic face, and which of them "
            "this copy should be in is a design call"
        )
    if not _has_text(shape):
        return None

    wanted = approved[0]
    changed = 0
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if has_arabic(run.text) and run.font.name != wanted:
                run.font.name = wanted
                changed += 1
    return f"set {changed} Arabic run(s) in {wanted!r}" if changed else None


def fix_theme_font_drift(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Clear a run-level typeface so the run inherits from the layout again.

    Only the typeface named in the finding is cleared. A run set in some other
    face is a different defect, and silently rewriting it here would be a
    change nobody asked for.
    """
    wanted = _font_name(issue.found)
    if not wanted or not _has_text(shape):
        return None

    cleared = 0
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            name = run.font.name
            if name and name.casefold() == wanted.casefold():
                run.font.name = None
                cleared += 1
    return (
        f"cleared the hardcoded {wanted!r} from {cleared} run(s)"
        if cleared
        else None
    )


# --------------------------------------------------------------------------- #
# The AI layer's proposed corrections
# --------------------------------------------------------------------------- #
#
# A rule finding carries a measured target and its fixer reads the number back
# out. An AI finding carries prose, which is why none of them were ever
# fixable: "the caption should sit closer to the chart" has nothing to parse.
#
# `Issue.fix` is where the model puts a target a machine can act on, and this
# is what acts on it. The whole safety of it is that a proposal is not an
# instruction. Every value is checked against the master before the file is
# touched -- a colour has to be a palette entry, a typeface has to be
# approved, a size has to sit in the role's range -- so the worst a bad
# proposal can do is cost itself. Without a master to check against, nothing
# here runs at all, which is the honest answer rather than a hopeful one.

def fix_ai_action(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Carry out the action the AI layer proposed, once it checks out."""
    action = issue.fix
    if action is None or not action.valid:
        return None
    if ctx.brand is None:
        raise LeaveAlone(
            "there is no brand reference to check this proposal against, so "
            "it cannot be applied; re-run with the master or a guidelines file"
        )
    if action.op in _REMOVES and (issue.confidence or 0.0) < _SURE_ENOUGH:
        raise LeaveAlone(
            f"removing something needs more certainty than "
            f"{issue.confidence or 0.0:.2f}; the finding stays on the report "
            "and the shape stays on the slide"
        )
    handler = _AI_OPS.get(action.op)
    if handler is None:                     # pragma: no cover - FIX_OPS guards it
        raise LeaveAlone(f"no handler is written for {action.op!r}")
    return handler(shape, action, ctx)


# Ops that take something off the slide. Removal is the one act a designer
# cannot check by looking at the result -- everything else leaves evidence and
# this leaves a gap -- so it is the one that asks the model how sure it was.
_REMOVES = frozenset({"remove_note"})

# Not the 0.5 that separates a judgement call from a defect. Leaving a note in
# costs a designer ten seconds; taking a caption out of a client deck is a
# defect nobody sees until the client does, and the two are not worth the same.
_SURE_ENOUGH = 0.8


def _ai_recolor_fill(shape: Any, action: Any, ctx: "FixContext") -> Optional[str]:
    return _ai_recolor(shape, action, ctx, "fill")


def _ai_recolor_line(shape: Any, action: Any, ctx: "FixContext") -> Optional[str]:
    return _ai_recolor(shape, action, ctx, "outline")


def _ai_recolor(shape: Any, action: Any, ctx: "FixContext", kind: str):
    label = _palette_entry(action.hex, ctx)
    from pptx.dml.color import RGBColor  # noqa: PLC0415 - lazy heavy dependency

    colour = RGBColor.from_string(action.hex)
    if kind == "outline":
        shape.line.color.rgb = colour
        return f"recoloured the outline to {label} #{action.hex}"
    shape.fill.solid()
    shape.fill.fore_color.rgb = colour
    return f"recoloured the fill to {label} #{action.hex}"


def _ai_recolor_text(shape: Any, action: Any, ctx: "FixContext") -> Optional[str]:
    label = _palette_entry(action.hex, ctx)
    if not _has_text(shape):
        return None
    from pptx.dml.color import RGBColor  # noqa: PLC0415 - lazy heavy dependency

    colour = RGBColor.from_string(action.hex)
    changed = 0
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if not run.text.strip():
                continue
            run.font.color.rgb = colour
            changed += 1
    if not changed:
        return None
    return f"recoloured {changed} run(s) to {label} #{action.hex}"


def _ai_set_font(shape: Any, action: Any, ctx: "FixContext") -> Optional[str]:
    wanted = (action.font or "").strip()
    approved = {name.casefold(): name for name in ctx.brand.allowed_fonts}
    if wanted.casefold() not in approved:
        raise LeaveAlone(
            f"{wanted!r} is not one of the approved typefaces "
            f"({', '.join(sorted(ctx.brand.allowed_fonts)) or 'none declared'})"
        )
    if not _has_text(shape):
        return None
    name = approved[wanted.casefold()]
    changed = 0
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if run.text.strip() and run.font.name != name:
                run.font.name = name
                changed += 1
    return f"set {changed} run(s) in {name}" if changed else None


def _ai_set_font_size(shape: Any, action: Any, ctx: "FixContext") -> Optional[str]:
    size = action.size_pt
    if not size or size <= 0:
        return None
    low, high = ctx.brand.size_range(_role_of(shape))
    if (low is not None and size < low) or (high is not None and size > high):
        raise LeaveAlone(
            f"{size:g}pt is outside the {low if low is not None else '?'}-"
            f"{high if high is not None else '?'}pt range the brand system "
            "sets for this role"
        )
    if not _has_text(shape):
        return None
    from pptx.util import Pt  # noqa: PLC0415 - lazy heavy dependency

    changed = 0
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            if run.text.strip():
                run.font.size = Pt(size)
                changed += 1
    return f"set {changed} run(s) to {size:g}pt" if changed else None


def _ai_disable_autofit(shape: Any, action: Any, ctx: "FixContext") -> Optional[str]:
    """Stop a box shrinking its own type to hide that the copy is too long.

    The shrink is not a size somebody chose, it is PowerPoint papering over an
    overflow, and it is why a deck can read at six different sizes while every
    box claims the same one. Turning it off makes the overflow visible, which
    is the point: how much copy fits is a decision, and it goes back to a
    person rather than being hidden by the software.
    """
    if not _has_text(shape):
        return None
    from pptx.enum.text import MSO_AUTO_SIZE  # noqa: PLC0415 - lazy

    frame = shape.text_frame
    if frame.auto_size == MSO_AUTO_SIZE.NONE:
        return None
    frame.auto_size = MSO_AUTO_SIZE.NONE
    return "turned off shrink-to-fit, so the type keeps the size it is set at"


def _ai_delete_empty_paragraphs(
    shape: Any, action: Any, ctx: "FixContext"
) -> Optional[str]:
    """Drop empty paragraphs from the end of a text frame.

    Distinct from `typography.whitespace`, which works inside a paragraph and
    cannot see these: a trailing empty paragraph carries no runs to strip. It
    is what stops a vertically centred box from looking centred, and the AI
    layer is where it turns up, because the rendered slide is where it shows.
    """
    if not _has_text(shape):
        return None
    paragraphs = list(shape.text_frame.paragraphs)
    removed = 0
    for paragraph in reversed(paragraphs[1:]):      # never the last one left
        if paragraph.text.strip():
            break
        paragraph._p.getparent().remove(paragraph._p)
        removed += 1
    return f"removed {removed} empty paragraph(s) from the end" if removed else None


def _ai_move(shape: Any, action: Any, ctx: "FixContext") -> Optional[str]:
    """Put a shape where the model said, once the frame agrees.

    Geometry from the model was refused outright at first, on the grounds that
    it is told not to measure off a rendered image. That was the wrong line to
    draw. The payload carries every shape's box in inches, the slide size and
    the safe margins, all of them exact; reasoning from those to a position is
    arithmetic on numbers it was given, not an impression of a picture, and it
    is what `basis: "geometry"` has always meant.

    What makes it safe is not trusting the number but checking it: the target
    has to land inside the safe margins, and the move then goes through the
    same overlap and alignment guards a rule's move does. A slide is a
    composition whoever proposed the move.
    """
    left, top = action.left_in, action.top_in
    if left is None or top is None:
        return None
    width = (shape.width or 0) / EMU_PER_INCH
    height = (shape.height or 0) / EMU_PER_INCH
    _within_the_frame(left, top, width, height, ctx)

    wanted = (_emu(left), _emu(top))
    if wanted == (shape.left, shape.top):
        return None
    dx = (wanted[0] - (shape.left or 0)) / EMU_PER_INCH
    dy = (wanted[1] - (shape.top or 0)) / EMU_PER_INCH
    # No drift ceiling here, unlike the rule fixes. That ceiling exists to say
    # "a shape this far from its cohort is part of a different arrangement",
    # which is a statement about a delta measured off other shapes. This is an
    # explicit target that has already been checked against the frame, and how
    # far the shape happens to be from it is evidence of nothing. The distance
    # is stated so a designer reading the tick list can see it.
    shape.left, shape.top = wanted
    return f"moved {dx:+.2f}, {dy:+.2f}in to {left:.2f}, {top:.2f}in"


def _ai_resize(shape: Any, action: Any, ctx: "FixContext") -> Optional[str]:
    """Resize a shape to the box the model named, once the frame agrees.

    Never below a size that could hide content, and never outside the safe
    margins at its current position -- a box grown past the frame trades one
    finding for another.
    """
    width, height = action.width_in, action.height_in
    if width is None or height is None:
        return None
    if width < _MIN_SIDE_IN or height < _MIN_SIDE_IN:
        raise LeaveAlone(
            f"{width:.2f} x {height:.2f}in is too small to hold anything; a "
            "box that size hides its content rather than fitting it"
        )
    left = (shape.left or 0) / EMU_PER_INCH
    top = (shape.top or 0) / EMU_PER_INCH
    _within_the_frame(left, top, width, height, ctx)

    wanted = (_emu(width), _emu(height))
    if wanted == (shape.width, shape.height):
        return None
    was_w = (shape.width or 0) / EMU_PER_INCH
    was_h = (shape.height or 0) / EMU_PER_INCH
    shape.width, shape.height = wanted
    return (
        f"resized from {was_w:.2f} x {was_h:.2f} to {width:.2f} x {height:.2f}in"
    )


# A box smaller than this in either direction cannot be holding anything a
# reader is meant to see, so a proposal to make one is a mistake rather than a
# correction.
_MIN_SIDE_IN = 0.1


def _within_the_frame(
    left: float, top: float, width: float, height: float, ctx: "FixContext"
) -> None:
    """Refuse a box that would sit outside the canvas or the safe margins.

    The margins are in the payload the model was given, so a target outside
    them is not a reading it could defend -- and applying one would satisfy
    whatever it was aiming at by breaking the rule the deck is measured by.
    """
    canvas_w = ctx.width_emu / EMU_PER_INCH
    canvas_h = ctx.height_emu / EMU_PER_INCH
    margins = ctx.margins or {}
    inside_left = margins.get("left", 0.0)
    inside_top = margins.get("top", 0.0)
    inside_right = canvas_w - margins.get("right", 0.0)
    inside_bottom = canvas_h - margins.get("bottom", 0.0)

    if left < inside_left - _EDGE_SLACK_IN or top < inside_top - _EDGE_SLACK_IN:
        raise LeaveAlone(
            f"{left:.2f}, {top:.2f}in is outside the safe margins the payload "
            "gave it"
        )
    if (
        left + width > inside_right + _EDGE_SLACK_IN
        or top + height > inside_bottom + _EDGE_SLACK_IN
    ):
        raise LeaveAlone(
            f"a {width:.2f} x {height:.2f}in box at {left:.2f}, {top:.2f}in "
            "runs past the safe margins the payload gave it"
        )


# A hair, so a target computed to the margin itself is not refused over the
# rounding that put it a thousandth of an inch outside.
_EDGE_SLACK_IN = 0.01


def _ai_remove_note(shape: Any, action: Any, ctx: "FixContext") -> Optional[str]:
    """Take a production note off the slide.

    The only op here that removes something, and removal is the one act a
    designer cannot check by looking at the result: everything else leaves
    evidence on the slide, and this leaves a gap. So it is the most guarded.

    A placeholder is never removed. It is part of the layout's structure, it
    comes back empty on the next rebuild, and a title box holding a note is a
    title box with a note typed into it -- the copy is the problem, not the
    shape.

    Nothing else is removed either unless the model was sure. Below
    `_SURE_ENOUGH` the finding stays on the report for a designer to read and
    the shape stays on the slide, because leaving a note in costs ten seconds
    and taking a caption out of a client deck is a defect nobody sees until
    the client does.

    What was removed is put back into the outcome verbatim. A report that says
    "removed a shape" is not enough to check; one that quotes what it said is.
    """
    try:
        if shape.is_placeholder:
            raise LeaveAlone(
                "this is a layout placeholder, so the note is copy typed into "
                "the deck's structure rather than a shape somebody added; "
                "clear the text instead of deleting the box"
            )
    except LeaveAlone:
        raise
    except Exception:
        pass

    text = _text_of(shape)
    if not text.strip():
        raise LeaveAlone(
            "there is no text here to have been a note; a shape this names "
            "and cannot read is not one to delete"
        )

    element = getattr(shape, "_element", None)
    parent = element.getparent() if element is not None else None
    if parent is None:
        return None
    parent.remove(element)
    return f"removed the production note {_shorten(text)}"


def _text_of(shape: Any) -> str:
    try:
        return shape.text_frame.text if shape.has_text_frame else ""
    except Exception:
        return ""


def _shorten(text: str, limit: int = 90) -> str:
    """The note as it read, quoted, and cut only when it is very long."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return repr(flat)
    return repr(flat[:limit] + "...")


_AI_OPS: dict[str, Callable[[Any, Any, "FixContext"], Optional[str]]] = {
    "recolor_fill": _ai_recolor_fill,
    "recolor_line": _ai_recolor_line,
    "recolor_text": _ai_recolor_text,
    "set_font": _ai_set_font,
    "set_font_size": _ai_set_font_size,
    "disable_autofit": _ai_disable_autofit,
    "delete_empty_paragraphs": _ai_delete_empty_paragraphs,
    "move": _ai_move,
    "resize": _ai_resize,
    "remove_note": _ai_remove_note,
}


def _palette_entry(value: Optional[str], ctx: "FixContext") -> str:
    """The palette label for a proposed colour, or a refusal.

    Exact membership, not nearest. "Nearest" is how a colour the model liked
    the look of gets written into a client deck wearing a brand label.
    """
    wanted = (value or "").strip().lstrip("#").upper()
    if len(wanted) != 6:
        raise LeaveAlone(f"{value!r} is not a six-digit colour")
    for label, entry in ctx.brand.palette.items():
        if str(entry).strip().lstrip("#").upper() == wanted:
            return label
    raise LeaveAlone(
        f"#{wanted} is not in the brand palette, so it is a colour the model "
        "chose rather than one the master declares"
    )


def _role_of(shape: Any) -> str:
    """Best guess at a shape's type role, from its placeholder or its name."""
    try:
        if shape.is_placeholder:
            token = str(shape.placeholder_format.type or "")
            if "TITLE" in token and "SUB" not in token:
                return "title"
            if "SUBTITLE" in token:
                return "subtitle"
            if "BODY" in token or "OBJECT" in token:
                return "body"
    except Exception:
        pass
    name = str(getattr(shape, "name", "") or "").casefold()
    for role in ("subtitle", "title", "footer", "body"):
        if name.startswith(role):
            return role
    return "body"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

Fixer = Callable[[Any, Issue, "FixContext"], Optional[str]]

FIXERS: dict[str, Fixer] = {
    "space.alignment_grid": fix_alignment_grid,
    "color.text.off_palette": fix_off_palette_text,
    "color.shape.off_palette": fix_off_palette_shape,
    "space.off_canvas": fix_off_canvas,
    "space.safe_margin": fix_safe_margin,
    "space.repeat_out_of_line": fix_repeat_out_of_line,
    "space.row_out_of_line": fix_row_out_of_line,
    "space.overlap": fix_overlap,
    "space.series_crowded": fix_series_crowded,
    "space.satellite_offset": fix_satellite_offset,
    "title.position_inconsistent": fix_title_position,
    "logo.geometry": fix_logo_geometry,
    "typography.whitespace": fix_whitespace,
    "typography.manual_line_break": fix_manual_line_break,
    "typography.terminal_punctuation": fix_terminal_punctuation,
    "typography.orphan_widow": fix_orphan_widow,
    "font.family.theme_drift": fix_theme_font_drift,
    "font.family.arabic": fix_arabic_font,
    "typography.rtl_not_set": fix_rtl_not_set,
}

# The order fixes run in, low first. Two fixes can touch one shape, and then
# the second decides: snapping a box to a grid line after clamping it back
# onto the canvas pushes it straight off again, which is exactly what applying
# them in report order did. So preferences run first and hard constraints run
# last -- a shape must be on the canvas, and would merely prefer to be on the
# grid.
FIX_ORDER: dict[str, int] = {
    "typography.whitespace": 10,
    "typography.manual_line_break": 10,
    "typography.terminal_punctuation": 10,
    "typography.orphan_widow": 10,
    "font.family.theme_drift": 10,
    "font.family.arabic": 10,
    "typography.rtl_not_set": 10,
    # The series fixes, together: each one puts a shape back where the rest of
    # its set already is, so they cannot fight each other, and both want to
    # run before the clamps below decide anything about the same shape.
    "space.series_crowded": 15,
    "space.repeat_out_of_line": 20,
    "space.row_out_of_line": 20,
    "space.overlap": 30,
    "space.satellite_offset": 20,
    "title.position_inconsistent": 20,
    "logo.geometry": 20,
    "space.off_canvas": 90,
    # Last of all, and strictly stronger: the safe margin sits inside the
    # canvas, so a shape moved within it satisfies the canvas too.
    "space.safe_margin": 95,
}
DEFAULT_ORDER = 50


def fix_order(issue: Issue) -> int:
    return FIX_ORDER.get(issue.rule_id or "", DEFAULT_ORDER)

# Findings a machine should not attempt, and why. Kept explicit so that
# `apply --list` can say "needs a designer, because ..." rather than leaving a
# finding unexplained, which reads like an oversight.
NEEDS_A_PERSON: dict[str, str] = {
    "space.mirror_pair_offset": (
        "names two shapes that disagree about a height, and nothing in the "
        "geometry says which of them moved; moving both to the midpoint would "
        "level the pair and put both of them off the arrangement they belong to"
    ),
    "logo.missing": "needs the approved logo file, which the tool does not have",
    "logo.unapproved_asset": "needs the approved logo file to swap in",
    "title.missing": "needs copy that has to be written",
    "title.detached_textbox": "moving copy between shapes changes the design",
    "subtitle.structure": "needs copy that has to be written",
    "size.autofit_shrink": "the fix is to edit the copy, not to resize the box",
    "layout.not_in_master": "use `rebuild`, which recreates the slide on the layout",
    "layout.header_footer_missing": "the master has to be edited, not the deck",
    "layout.band_missing": "the master has to be edited, not the deck",
    "color.theme_mismatch": (
        "the deck carries its own theme, so no shape can be corrected into "
        "the right colour; use `rebuild`, which replaces the theme"
    ),
    "color.inconsistent_variants": (
        "which of the near-identical colours in play is the intended one is a "
        "design call"
    ),
    "font.family.unapproved": "which approved face replaces it is a design call",
    "font.family.mixed_in_shape": "which of the faces in play is correct is a design call",
    "size.role.out_of_range": "resizing type changes how much copy fits",
    "size.role.inconsistent": "resizing type changes how much copy fits",
    "space.text_overflow": "the fix is to edit the copy or resize the box",
}


def is_geometric(issue: Issue) -> bool:
    """Whether applying this finding moves or resizes a shape.

    A rule says so by its id. An AI finding says so by the op it proposed, and
    both answers lead to the same guards: the overlap and alignment checks
    exist because a slide is a composition, which is true whoever asked for
    the move.
    """
    if issue.source.value == "rule":
        return (issue.rule_id or "") in GEOMETRIC
    return bool(issue.fix and issue.fix.valid and issue.fix.geometric)


def fixer_for(issue: Issue) -> Optional[Fixer]:
    """The fixer for a finding, or None when it needs a person.

    An AI finding has one only when it carries a `fix`: the model named an
    action from a closed set and a target that can be checked against the
    master. Most do not, and most should not -- what the AI layer is for is
    the judgement the rules cannot make, and a judgement has no op.
    """
    if issue.source.value != "rule":
        return fix_ai_action if (issue.fix and issue.fix.valid) else None
    if not issue.rule_id:
        return None
    return FIXERS.get(issue.rule_id)


def why_not_fixable(issue: Issue) -> str:
    if issue.source.value != "rule":
        return (
            "an AI observation with no mechanical action behind it, which is "
            "what most of them are"
        )
    return NEEDS_A_PERSON.get(
        issue.rule_id or "", "no fixer is written for this rule"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _has_text(shape: Any) -> bool:
    try:
        return bool(shape.has_text_frame)
    except Exception:
        return False


def _inches(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = _INCHES.search(value)
    return float(match.group(1)) if match else None


def _emu(inches: Optional[float]) -> int:
    return int(round((inches or 0.0) * EMU_PER_INCH))


def _edge(expected: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    """"left 6.48in, as the rest of the set" -> ("left", 6.48)."""
    if not expected:
        return None, None
    match = _EDGE.search(expected)
    return (match.group(1), float(match.group(2))) if match else (None, None)


def _point(text: Optional[str]) -> Optional[tuple[float, float]]:
    """"0.42, 1.19in" -> (0.42, 1.19), and the signed offset form with it.

    None when the text names no point at all, which is how the logo fixer
    tells a position finding from the width and corner findings the same rule
    also reports.
    """
    if not text:
        return None
    match = _POINT.search(text)
    return (float(match.group(1)), float(match.group(2))) if match else None


def _rect(expected: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    """"clear of 'X' at 1.20, 3.40, 2.00 x 0.60in" -> the four numbers.

    The partner's box travels on the finding so a fix knows what it is
    clearing without going looking for a shape by a name that is not unique.
    """
    if not expected:
        return None
    match = _RECT.search(expected)
    if match is None:
        return None
    return tuple(float(match.group(i)) for i in range(1, 5))       # type: ignore


def _spread(
    expected: Optional[str],
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """"evenly spaced across 1.00-8.90in, gap 0.38in" -> (1.00, 8.90, 0.38)."""
    if not expected:
        return None, None, None
    match = _SPREAD.search(expected)
    if match is None:
        return None, None, None
    return float(match.group(1)), float(match.group(2)), float(match.group(3))


def _centre_line(expected: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    """"centre y 3.00in, as the rest of the row" -> ("y", 3.00)."""
    if not expected:
        return None, None
    match = _CENTRE.search(expected)
    if match is None:
        return None, None
    return match.group(1).lower(), float(match.group(2))


def _margins(expected: Optional[str]) -> dict[str, float]:
    """"top 0.4in, right 0.19in, ... safe margin" -> {"top": 0.4, ...}.

    Only the edges the finding names are returned, so an unspecified edge is
    never invented and never corrected against.
    """
    if not expected:
        return {}
    return {side: float(value) for side, value in _MARGIN.findall(expected)}


def _hex_of(text: Optional[str]) -> Optional[str]:
    """The six hex digits in a finding's expected/found, upper case."""
    if not text:
        return None
    found = _HEX.search(text)
    return found.group(1).upper() if found else None


def _run_hex(run: Any) -> Optional[str]:
    """A run's own colour as six hex digits, or None when it inherits one.

    None for a theme-bound colour too: it is correct by construction, and the
    rule does not report those.
    """
    try:
        colour = run.font.color
        if colour is None or colour.type is None or colour.rgb is None:
            return None
        return str(colour.rgb).upper()
    except Exception:
        return None


def _font_name(found: Optional[str]) -> Optional[str]:
    """"explicit Arial" -> "Arial"."""
    if not found:
        return None
    match = _QUOTED_FONT.search(found.strip())
    return match.group(1) if match else None
