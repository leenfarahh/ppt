"""Where a shape's box sits: align, distribute, and the primitives under both.

THE GEOMETRY IS THE ADD-IN'S, AND IT IS THE HALF THAT PORTS EXACTLY. Section 7
of the pipeline document is arithmetic over four numbers -- left, top, width,
height -- and those four numbers are in the file as readily as they are on a
COM `Shape`. Nothing in this module needs PowerPoint to be running.

Three things are worth knowing before reading the table below.

ONLY LEFT AND TOP ARE EVER WRITTEN. Aligning a set does not resize it and does
not rotate it, which is why a row of differently sized cards can be aligned on
its bottom edge and keep its cards. The C# is explicit about this and the
reason is the same here: a fix that quietly resized a shape to make an edge
line up would be trading a visible defect for an invisible one.

THE ANCHOR NEVER MOVES. In first-shape mode the caller has already decided
which shape is right -- a rule found the grid line, or the majority of a
series -- and the whole set is brought to it. Slide mode has no shape to hold
still, so every shape given is moved onto the page box.

THE BOX IS THE UNROTATED ONE. `shape.left` in a file, like `Shape.Left` over
COM, is the left edge of the shape before its rotation is applied, so a
rotated shape aligns on the box PowerPoint stores rather than on the corners a
reader sees. That is the add-in's behaviour, it is PowerPoint's own object
model, and matching it matters more than being cleverer than it: a tool that
aligned rotated shapes on their visual bounds would disagree with what the
designer gets by pressing the same button in the ribbon.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .actions import AlignAction, ArrangeResult, DistributeAction, ReferenceMode

log = logging.getLogger(__name__)

EMU_PER_INCH = 914400

# How many shapes distribution needs before it means anything. Two shapes have
# one gap and it is already even, so the pane greys the button out below three
# and so does this.
_DISTRIBUTE_NEEDS = 3


def emu(inches: float) -> int:
    return int(round(float(inches) * EMU_PER_INCH))


def inches(value: Optional[int]) -> float:
    return (value or 0) / EMU_PER_INCH


@dataclass(frozen=True)
class Box:
    """A shape's rectangle in EMU, and the six derived numbers alignment uses.

    Read once, up front, for the anchor. The C# reads the reference's bounds
    before it touches anything for the same reason: a reference re-read inside
    the loop would be read after an earlier shape in the set had already been
    moved, and on a set that includes the anchor twice that is a set that
    walks across the slide.
    """

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def centre_x(self) -> float:
        return self.left + self.width / 2

    @property
    def centre_y(self) -> float:
        return self.top + self.height / 2

    @classmethod
    def of(cls, shape: Any) -> "Box":
        return cls(
            int(shape.left or 0),
            int(shape.top or 0),
            int(shape.width or 0),
            int(shape.height or 0),
        )

    @classmethod
    def page(cls, width_emu: int, height_emu: int) -> "Box":
        """The slide itself, as the reference for `ReferenceMode.SLIDE`."""
        return cls(0, 0, int(width_emu), int(height_emu))


def bounds_of(shapes: Sequence[Any]) -> Optional[Box]:
    """The rectangle a set of shapes occupies between them."""
    boxes = [Box.of(shape) for shape in shapes if shape is not None]
    if not boxes:
        return None
    left = min(box.left for box in boxes)
    top = min(box.top for box in boxes)
    return Box(
        left,
        top,
        max(box.right for box in boxes) - left,
        max(box.bottom for box in boxes) - top,
    )


# Each alignment as the pair of rules it applies, one per axis. None means
# "leave this axis alone", which is what makes `LEFT` a horizontal move and
# nothing else.
#
# This table IS the C#'s two switch statements, both of them: the first-shape
# arithmetic and the `MsoAlignCmd` pairs the slide mode issues. They agreed
# with each other in the original only by being written twice and kept in
# step by hand; here they cannot disagree, because slide mode is the same
# arithmetic against the page box.
_PLAN: dict[AlignAction, tuple[Optional[str], Optional[str]]] = {
    AlignAction.LEFT: ("left", None),
    AlignAction.RIGHT: ("right", None),
    AlignAction.TOP: (None, "top"),
    AlignAction.BOTTOM: (None, "bottom"),
    AlignAction.TOP_LEFT: ("left", "top"),
    AlignAction.TOP_RIGHT: ("right", "top"),
    AlignAction.BOTTOM_LEFT: ("left", "bottom"),
    AlignAction.BOTTOM_RIGHT: ("right", "bottom"),
    AlignAction.CENTER: ("centre", None),
    AlignAction.MIDDLE: (None, "middle"),
    AlignAction.CENTER_MIDDLE: ("centre", "middle"),
    # The dock family: an edge on one axis, a centre on the other.
    AlignAction.POSITION_LEFT: ("left", "middle"),
    AlignAction.POSITION_RIGHT: ("right", "middle"),
    AlignAction.POSITION_TOP: ("centre", "top"),
    AlignAction.POSITION_BOTTOM: ("centre", "bottom"),
}


def align(
    shapes: Sequence[Any],
    action: AlignAction,
    *,
    mode: ReferenceMode = ReferenceMode.FIRST_SHAPE,
    slide_size: Optional[tuple[int, int]] = None,
) -> ArrangeResult:
    """Align a set of shapes. C#: Align / ApplyFirstShapeAlignment.

    `shapes` is ordered and the order is load-bearing: in first-shape mode the
    first of them is the anchor and is not moved. In slide mode `slide_size`
    is the page in EMU and every shape given is moved.

    Never raises. A set too small, an unknown action or a shape that will not
    take a position comes back as a result with a message on it, because this
    is called from fixers and a fixer that raises costs the report the other
    twenty-four findings.
    """
    result = ArrangeResult()
    shapes = [shape for shape in (shapes or ()) if shape is not None]

    plan = _PLAN.get(action)
    if plan is None:
        result.message = f"unknown alignment: {action}"
        return result

    if mode is ReferenceMode.SLIDE:
        if not slide_size:
            result.message = "aligning to the slide needs the page size"
            return result
        if not shapes:
            result.message = "needs at least one shape to align"
            return result
        reference = Box.page(*slide_size)
        movers = shapes
    else:
        if len(shapes) < 2:
            result.message = "needs at least two shapes: one to hold, one to move"
            return result
        reference = Box.of(shapes[0])
        movers = shapes[1:]

    horizontal, vertical = plan
    for shape in movers:
        try:
            moved = _place(shape, reference, horizontal, vertical)
        except Exception:
            result.skipped += 1
            log.debug("could not align %r", shape, exc_info=True)
            continue
        if moved is not None:
            result.record(moved)

    result.success = True
    return result


def _place(
    shape: Any, reference: Box, horizontal: Optional[str], vertical: Optional[str]
) -> Optional[str]:
    """Apply one alignment to one shape, and say what it did."""
    box = Box.of(shape)
    left = _axis(horizontal, reference.left, reference.right, reference.centre_x,
                 box.width)
    top = _axis(vertical, reference.top, reference.bottom, reference.centre_y,
                box.height)

    dx = dy = 0
    if left is not None and left != box.left:
        shape.left = left
        dx = left - box.left
    if top is not None and top != box.top:
        shape.top = top
        dy = top - box.top
    if not dx and not dy:
        return None
    return f"moved {dx / EMU_PER_INCH:+.2f}, {dy / EMU_PER_INCH:+.2f}in"


def _axis(
    rule: Optional[str], near: int, far: int, centre: float, extent: int
) -> Optional[int]:
    """The new leading coordinate on one axis, or None to leave it alone."""
    if rule is None:
        return None
    if rule == "left" or rule == "top":
        return near
    if rule == "right" or rule == "bottom":
        return far - extent
    return int(round(centre - extent / 2))


def distribute(
    shapes: Sequence[Any],
    action: DistributeAction,
    *,
    span_in: Optional[tuple[float, float]] = None,
    gap_in: Optional[float] = None,
    allow_overlap: bool = False,
) -> ArrangeResult:
    """Space a set evenly. C#: Distribute.

    EQUAL GAPS, HOLDING THE OUTERMOST TWO, which is what PowerPoint does and
    what `space.series_uneven` already measures against -- the rule reports a
    span and a gap, and this is the act that satisfies it. Distributing on
    centres instead would be a different answer for any set whose shapes are
    not all one size, and the rule would then go on reporting the row this had
    just "fixed".

    THE REFERENCE SELECTOR IS IGNORED, as it is in the add-in. PowerPoint
    distributes across the bounds of the selected objects whatever the pane's
    radio says, and the C# passes `msoFalse` unconditionally with a comment
    saying so. `span_in` exists for the one caller that genuinely knows
    better: a finding that names the span to spread across -- a band, a column
    -- rather than letting the shapes' own bounds decide.

    `gap_in` is the second such escape, and it exists for a narrower reason:
    a finding that has already been shown to a designer NAMES a gap, rounded
    for reading. Deriving the gap again here would lay the row out on a number
    the report never mentioned -- 0.125in against a finding that says 0.13in --
    and the fix would then describe itself with a figure it had not applied.
    So a caller holding a published number passes it, and everybody else lets
    the span decide.

    A set wider than the space it has to sit in comes back refused rather than
    distributed, because spreading it evenly would leave the overlap it
    started with and report success for it. The caller is told by how much.
    """
    result = ArrangeResult()
    shapes = [shape for shape in (shapes or ()) if shape is not None]
    if len(shapes) < _DISTRIBUTE_NEEDS:
        result.message = f"needs at least {_DISTRIBUTE_NEEDS} shapes to distribute"
        return result

    horizontal = action is DistributeAction.HORIZONTAL
    side = "left" if horizontal else "top"
    extent = "width" if horizontal else "height"

    ordered = sorted(shapes, key=lambda s: getattr(s, side) or 0)
    sizes = [int(getattr(shape, extent) or 0) for shape in ordered]

    if span_in is not None:
        start, end = emu(span_in[0]), emu(span_in[1])
    else:
        start = int(getattr(ordered[0], side) or 0)
        last = ordered[-1]
        end = int(getattr(last, side) or 0) + int(getattr(last, extent) or 0)

    if gap_in is not None:
        gap = float(emu(gap_in))
    else:
        gap = ((end - start) - sum(sizes)) / (len(ordered) - 1)
    if gap < 0 and not allow_overlap:
        result.message = (
            f"the shapes are wider than the {inches(end - start):.2f}in they "
            f"have to sit in, so spreading them still leaves "
            f"{abs(inches(int(gap))):.2f}in of overlap"
        )
        return result

    cursor = float(start)
    for shape, size in zip(ordered, sizes):
        wanted = int(round(cursor))
        current = int(getattr(shape, side) or 0)
        if wanted != current:
            try:
                setattr(shape, side, wanted)
            except Exception:
                result.skipped += 1
                log.debug("could not distribute %r", shape, exc_info=True)
                cursor += size + gap
                continue
            result.record(f"moved {inches(wanted - current):+.2f}in")
        cursor += size + gap

    result.success = True
    result.message = (
        f"spread {len(ordered)} shapes across "
        f"{inches(start):.2f}-{inches(end):.2f}in with a "
        f"{inches(int(round(gap))):.2f}in gap"
    )
    return result


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
#
# The three moves the existing fixers make, each of them one line, and here
# rather than in `apply.fixers` so that "put this edge at 11.19in" has one
# definition. The fixers still decide WHETHER to move and by how much they are
# willing to; what "the right edge" means is this module's business, and it is
# the same meaning `align` uses.

def align_edge(shape: Any, side: str, target_in: float) -> Optional[float]:
    """Put one edge of a shape at an absolute position. Inches moved, or None.

    "right" and "bottom" subtract the shape's own extent, which is the step a
    fixer that read the target off a finding and assigned it to `shape.left`
    would skip -- and skipping it moves a right-aligned shape by its own width
    off the column it belongs to. An Arabic deck is a deck of right-aligned
    shapes.
    """
    side = (side or "left").lower()
    if side in ("left", "right"):
        current, extent = shape.left, shape.width or 0
    elif side in ("top", "bottom"):
        current, extent = shape.top, shape.height or 0
    else:
        return None
    if current is None:
        return None

    wanted = emu(target_in) - (extent if side in ("right", "bottom") else 0)
    if wanted == current:
        return None
    if side in ("left", "right"):
        shape.left = wanted
    else:
        shape.top = wanted
    return inches(wanted - current)


def align_centre(shape: Any, axis: str, target_in: float) -> Optional[float]:
    """Put a shape's centre on an absolute line. Inches moved, or None.

    A row of shapes of different heights lines up on its middle and not on its
    top, which is why this exists beside `align_edge` rather than inside it.
    """
    axis = (axis or "x").lower()
    side = "left" if axis == "x" else "top"
    extent = shape.width if axis == "x" else shape.height
    current = getattr(shape, side, None)
    if current is None or extent is None:
        return None

    wanted = int(round(emu(target_in) - extent / 2))
    if wanted == current:
        return None
    setattr(shape, side, wanted)
    return inches(wanted - current)


def move_to(shape: Any, left_in: float, top_in: float) -> Optional[tuple[float, float]]:
    """Put a shape at an absolute position. The move in inches, or None."""
    if shape.left is None or shape.top is None:
        return None
    wanted_left, wanted_top = emu(left_in), emu(top_in)
    if (wanted_left, wanted_top) == (shape.left, shape.top):
        return None
    moved = (inches(wanted_left - shape.left), inches(wanted_top - shape.top))
    shape.left, shape.top = wanted_left, wanted_top
    return moved


def locks_aspect(shape: Any) -> bool:
    """Whether the shape declares `noChangeAspect`.

    REPORTED, NOT OBEYED -- the add-in's choice, and the honest one to keep,
    but the reason differs here and the difference is worth stating. Over COM
    the lock is live: PowerPoint adjusts the other dimension as you set one,
    so a Make Same Width on a locked shape changes its height behind the
    caller's back and the warning is a warning about something that happened.

    In a file the lock does nothing at all. It constrains the resize handles
    in the PowerPoint window and no more, so writing both dimensions writes
    exactly what was asked and the shape a designer locked comes back
    distorted with no complaint from anybody. That is the more dangerous of
    the two, which is why the count is carried on the result and callers that
    care are expected to look at it rather than at the message.
    """
    element = getattr(shape, "_element", None)
    if element is None:
        return False
    node = _lock_node(element)
    return node is not None and node.get("noChangeAspect") in ("1", "true")


def _lock_node(element: Any):
    """`a:spLocks` / `a:picLocks` / `a:graphicFrameLocks` on this shape alone.

    Walked by hand rather than with `.//`, because a group's descendants carry
    locks of their own and a search would answer for one of them.
    """
    for child in element:
        if not _local(child.tag).startswith("nv"):
            continue
        for grandchild in child:
            if not _local(grandchild.tag).startswith("cNv"):
                continue
            for node in grandchild:
                if _local(node.tag).endswith("Locks"):
                    return node
    return None


def _local(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1] if "}" in text else text
