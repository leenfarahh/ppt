"""Make Same: capture one shape's property, write it onto the others.

Two phases by construction, as in the C#. `capture` reads the reference once
and records only what the requested action needs; `apply_style` writes that
onto one shape and absorbs its own failures, so one locked shape in a set of
twelve costs one shape rather than the batch.

WHERE THIS DEPARTS FROM THE ORIGINAL, AND WHY IT IS THE BETTER HALF OF THE
TRADE. The add-in has two escape hatches it is not proud of: Make Same Format
hands the whole job to PowerPoint's format painter because COM cannot
reproduce a fill faithfully, and Make Same Line Color runs the painter and
then puts back everything the painter overwrote, because there is no COM
surface for a theme-driven or patterned line colour. Both exist because the
COM object model flattens what it reports -- a fill with a `lumMod` on it
comes back as `na`.

A file has no such problem. The fill is an element, the line is an element,
and copying an element copies the theme reference, the tint, the gradient
stops and the transforms with it, exactly. So there is no painter here and no
restore dance: `FILL_COLOR` deep-copies `a:solidFill`/`a:gradFill`/whatever
the reference carries, and `LINE_COLOR` copies only the colour child of
`a:ln` and leaves the weight, the dash and the arrowheads where they were.
That is what the C#'s capture-paint-restore sequence is trying to achieve and
it achieves it in one step.

WHAT IS LOST is that nothing here can read a value PowerPoint computes at
render time. The add-in asks a live application; this asks a file. Every
property Make Same transfers is stated in the file, so the difference does not
bite -- but a caller wanting to know what a shape actually LOOKS like, rather
than what it says, still needs the renderer this tool already has.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from . import text as textframe
from .actions import (
    ArrangeResult,
    MatchAction,
    ReferenceMode,
    supports_slide_reference,
)
from .elements import Box, locks_aspect

log = logging.getLogger(__name__)

# The six spellings of a DrawingML fill, as a choice group. Exactly one of them
# may sit in any parent that takes a fill.
FILL_TAGS = ("noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill")

# What must come before the fill in each parent that carries one. The schema
# fixes the order and lxml will not fix it for us: a `a:solidFill` written
# before `a:prstGeom` gives a file PowerPoint declines to open.
_FILL_AFTER = {
    "spPr": ("xfrm", "prstGeom", "custGeom"),
    "ln": (),
    "rPr": ("ln",),
    "defRPr": ("ln",),
    "tcPr": ("lnL", "lnR", "lnT", "lnB", "lnTlToBr", "lnBlToTr", "cell3D"),
}


def _safe(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def _local(tag: Any) -> str:
    value = str(tag)
    return value.rsplit("}", 1)[-1] if "}" in value else value


def _qn(tag: str):
    from pptx.oxml.ns import qn  # noqa: PLC0415 - lazy, oxml internals

    return qn(tag)


@dataclass
class Style:
    """Everything a capture can hold. C#: MakeSameContext.

    Width, height and rotation are always recorded, as in the C#, because they
    cost one read each and a caller that asked for a fill and then wanted the
    size would otherwise have to capture twice.

    The XML fields hold deep copies taken at capture time. Copies rather than
    references: the reference shape can itself be a member of the set being
    written, and a capture that pointed at its live element would be reading a
    shape that had already been changed.
    """

    width: Optional[int] = None
    height: Optional[int] = None
    rotation: Optional[float] = None

    corner_radius: Optional[float] = None
    has_corner_radius: bool = False

    fill: Any = None                 # a:solidFill / a:gradFill / ...
    has_fill_capture: bool = False   # True even when the reference had no fill

    line_fill: Any = None            # the colour child of a:ln
    has_line_capture: bool = False
    line_width: Optional[str] = None       # a:ln/@w, in EMU as written
    line_cap: Optional[str] = None         # a:ln/@cap
    line_compound: Optional[str] = None    # a:ln/@cmpd
    line_dash: Any = None                  # a:prstDash / a:custDash
    head_end: Any = None                   # a:headEnd
    tail_end: Any = None                   # a:tailEnd

    font_name: Optional[str] = None
    font_size_pt: Optional[float] = None
    font_bold: Optional[bool] = None
    font_italic: Optional[bool] = None
    font_underline: Any = None
    font_fill: Any = None            # the colour child of a:rPr
    has_font: bool = False

    anchor: Optional[str] = None
    anchor_centred: Optional[bool] = None
    insets_in: Optional[tuple] = None
    alignment: Optional[str] = None
    reading_order: Optional[bool] = None

    effects: Any = None              # a:effectLst
    scene_3d: Any = None             # a:scene3d
    shape_3d: Any = None             # a:sp3d
    body_pr: Any = None              # a:bodyPr

    signature: str = ""


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #

def capture(reference: Any, action: MatchAction) -> Style:
    """Read off the reference exactly what `action` needs. C#: CreateMakeSameContext."""
    box = Box.of(reference)
    style = Style(
        width=box.width,
        height=box.height,
        rotation=_safe(lambda: float(reference.rotation or 0.0), 0.0),
    )

    if action is MatchAction.CORNER_RADIUS:
        ok, radius = corner_radius_of(reference)
        style.has_corner_radius = ok
        style.corner_radius = radius

    elif action is MatchAction.FILL_COLOR:
        _capture_fill(reference, style)

    elif action in (MatchAction.LINE_WEIGHT, MatchAction.LINE_STYLE,
                    MatchAction.LINE_COLOR, MatchAction.ARROW_STYLE):
        _capture_line(reference, style)

    elif action in (MatchAction.FONT_NAME, MatchAction.FONT_COLOR,
                    MatchAction.FONT_SIZE, MatchAction.FONT_STYLE):
        _capture_font(reference, style)

    elif action is MatchAction.TEXT_ANCHOR:
        style.anchor = textframe.anchor_of(reference)
        style.anchor_centred = textframe.anchor_centred(reference)

    elif action is MatchAction.TEXT_INSETS:
        style.insets_in = textframe.insets_of(reference)

    elif action is MatchAction.PARAGRAPH_ALIGNMENT:
        style.alignment = textframe.alignment_of(reference)
        style.reading_order = textframe.reading_order_of(reference)

    elif action is MatchAction.FORMAT:
        _capture_format(reference, style)

    return style


def capture_slide(slide_size: tuple[int, int]) -> Style:
    """The page as a reference. C#: CreateSlideReferenceContext.

    A slide has a width and a height and nothing else, which is precisely why
    `SLIDE_REFERENCE_ACTIONS` is the three size actions and no more.
    """
    width, height = slide_size
    return Style(width=int(width), height=int(height), rotation=0.0)


def _capture_fill(reference: Any, style: Style) -> None:
    parents = _fill_parents(reference)
    style.has_fill_capture = bool(parents)
    if not parents:
        return
    style.fill = _copy_of(_fill_child(parents[0]))


def _capture_line(reference: Any, style: Style) -> None:
    line = _line_of(reference)
    if line is None:
        return
    style.has_line_capture = True
    style.line_fill = _copy_of(_fill_child(line))
    style.line_width = line.get("w")
    style.line_cap = line.get("cap")
    style.line_compound = line.get("cmpd")
    style.line_dash = _copy_of(
        _first_child(line, ("prstDash", "custDash"))
    )
    style.head_end = _copy_of(_first_child(line, ("headEnd",)))
    style.tail_end = _copy_of(_first_child(line, ("tailEnd",)))


def _capture_font(reference: Any, style: Style) -> None:
    """The first character's font. C#: CaptureFontStyle / GetFirstFontName.

    Every one of the four font actions reads the same sample, so they share a
    capture: asking for the size and asking for the whole style should never
    disagree about which character they looked at.
    """
    run = textframe.first_run(reference)
    if run is None:
        return
    font = _safe(lambda: run.font)
    if font is None:
        return

    style.has_font = True
    style.font_name = _safe(lambda: font.name)
    size = _safe(lambda: font.size)
    style.font_size_pt = None if size is None else round(size.pt, 2)
    style.font_bold = _safe(lambda: font.bold)
    style.font_italic = _safe(lambda: font.italic)
    style.font_underline = _safe(lambda: font.underline)

    properties = _safe(lambda: run._r.find(_qn("a:rPr")))
    if properties is not None:
        style.font_fill = _copy_of(_fill_child(properties))


def _capture_format(reference: Any, style: Style) -> None:
    """Everything the format painter would carry, minus the geometry.

    THE PAINTER DOES NOT CHANGE WHAT A SHAPE IS. Painting a rounded rectangle
    onto an ellipse leaves an ellipse wearing the rectangle's fill, and the
    same is true of its size and position. So `a:xfrm`, `a:prstGeom` and
    `a:custGeom` are excluded here by name -- everything else in `a:spPr`
    travels, which is fill, line, effects and the two 3-D groups.
    """
    _capture_fill(reference, style)
    _capture_line(reference, style)
    spPr = _sp_pr(reference)
    if spPr is not None:
        style.effects = _copy_of(_first_child(spPr, ("effectLst", "effectDag")))
        style.scene_3d = _copy_of(_first_child(spPr, ("scene3d",)))
        style.shape_3d = _copy_of(_first_child(spPr, ("sp3d",)))

    _capture_font(reference, style)
    style.anchor = textframe.anchor_of(reference)
    style.anchor_centred = textframe.anchor_centred(reference)
    style.insets_in = textframe.insets_of(reference)
    style.alignment = textframe.alignment_of(reference)
    style.reading_order = textframe.reading_order_of(reference)


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #

def apply_style(
    shape: Any, action: MatchAction, style: Style, result: ArrangeResult
) -> None:
    """Write one captured property onto one shape. C#: ApplyMakeSame.

    Raises nothing the caller has to catch for correctness -- `make_same`
    catches anyway, because a COM-shaped API that promised never to raise is
    worth keeping promised -- and records what it did on `result`.
    """
    if action in (MatchAction.WIDTH, MatchAction.HEIGHT, MatchAction.SIZE):
        _apply_size(shape, action, style, result)

    elif action is MatchAction.ROTATION:
        if style.rotation is not None and _safe(lambda: shape.rotation) != style.rotation:
            shape.rotation = style.rotation
            result.record(f"set the rotation to {style.rotation:g} degrees")

    elif action is MatchAction.CORNER_RADIUS:
        if not style.has_corner_radius or not set_corner_radius(
                shape, style.corner_radius):
            result.skipped += 1
        else:
            result.record(f"set the corner radius to {style.corner_radius:.3f}")

    elif action is MatchAction.FILL_COLOR:
        if not style.has_fill_capture:
            result.skipped += 1
        elif _apply_fill(shape, style.fill):
            result.record("matched the fill")

    elif action is MatchAction.LINE_WEIGHT:
        if _apply_line(shape, style, colour=False, weight=True):
            result.record("matched the line weight")

    elif action is MatchAction.LINE_STYLE:
        if _apply_line(shape, style, colour=False, dash=True):
            result.record("matched the line style")

    elif action is MatchAction.LINE_COLOR:
        if _apply_line(shape, style, colour=True):
            result.record("matched the line colour")

    elif action is MatchAction.ARROW_STYLE:
        if _apply_line(shape, style, colour=False, arrows=True):
            result.record("matched the arrowheads")

    elif action in (MatchAction.FONT_NAME, MatchAction.FONT_COLOR,
                    MatchAction.FONT_SIZE, MatchAction.FONT_STYLE):
        _apply_font(shape, action, style, result)

    elif action is MatchAction.TEXT_ANCHOR:
        outcome = textframe.align_text(
            shape, anchor=style.anchor, centred=style.anchor_centred)
        _absorb(outcome, result)

    elif action is MatchAction.TEXT_INSETS:
        outcome = textframe.align_text(shape, insets_in=style.insets_in)
        _absorb(outcome, result)

    elif action is MatchAction.PARAGRAPH_ALIGNMENT:
        outcome = textframe.align_text(
            shape, alignment=style.alignment, rtl=style.reading_order)
        _absorb(outcome, result)

    elif action is MatchAction.FORMAT:
        _apply_format(shape, style, result)

    else:
        raise ValueError(f"unsupported match action: {action}")


def _apply_size(
    shape: Any, action: MatchAction, style: Style, result: ArrangeResult
) -> None:
    if locks_aspect(shape):
        result.aspect_locked += 1

    box = Box.of(shape)
    if action in (MatchAction.WIDTH, MatchAction.SIZE) and style.width is not None:
        if box.width != style.width:
            shape.width = style.width
            result.record(f"set the width to {style.width / 914400:.2f}in")
    if action in (MatchAction.HEIGHT, MatchAction.SIZE) and style.height is not None:
        if box.height != style.height:
            shape.height = style.height
            result.record(f"set the height to {style.height / 914400:.2f}in")


def _apply_font(
    shape: Any, action: MatchAction, style: Style, result: ArrangeResult
) -> None:
    """Write a font across every run of every paragraph of every cell.

    THE READ SAMPLED ONE CHARACTER AND THE WRITE VISITS EVERYTHING, which is
    the asymmetry `arrange.text` exists to keep. It is also what makes this
    idempotent: read one value, write it everywhere, and the next read of the
    same sample returns the value that was written.
    """
    if not style.has_font:
        result.skipped += 1
        return

    from pptx.util import Pt  # noqa: PLC0415 - lazy, optional dep

    changed = 0
    for paragraph in textframe.paragraphs(shape):
        for run in _safe(lambda p=paragraph: list(p.runs), []) or []:
            font = _safe(lambda r=run: r.font)
            if font is None:
                continue
            if action in (MatchAction.FONT_NAME, MatchAction.FONT_STYLE):
                if style.font_name:
                    font.name = style.font_name
            if action in (MatchAction.FONT_SIZE, MatchAction.FONT_STYLE):
                if style.font_size_pt and style.font_size_pt > 0:
                    font.size = Pt(style.font_size_pt)
            if action in (MatchAction.FONT_COLOR, MatchAction.FONT_STYLE):
                properties = _safe(lambda r=run: r._r.get_or_add_rPr())
                if properties is not None and style.font_fill is not None:
                    _set_fill(properties, style.font_fill)
            if action is MatchAction.FONT_STYLE:
                font.bold = style.font_bold
                font.italic = style.font_italic
                if style.font_underline is not None:
                    font.underline = style.font_underline
            changed += 1

    if not changed:
        result.skipped += 1
        return
    what = {
        MatchAction.FONT_NAME: f"set {changed} run(s) in {style.font_name}",
        MatchAction.FONT_SIZE: f"set {changed} run(s) to {style.font_size_pt:g}pt"
        if style.font_size_pt else f"set {changed} run(s)",
        MatchAction.FONT_COLOR: f"matched the text colour on {changed} run(s)",
        MatchAction.FONT_STYLE: f"matched the whole font style on {changed} run(s)",
    }[action]
    result.record(what)


def _apply_format(shape: Any, style: Style, result: ArrangeResult) -> None:
    """The file-side format painter: everything except size, position and shape."""
    touched = False
    if style.has_fill_capture and _apply_fill(shape, style.fill):
        touched = True
    if style.has_line_capture and _apply_line(
            shape, style, colour=True, weight=True, dash=True, arrows=True):
        touched = True

    spPr = _sp_pr(shape)
    if spPr is not None:
        for captured, tags in (
            (style.effects, ("effectLst", "effectDag")),
            (style.scene_3d, ("scene3d",)),
            (style.shape_3d, ("sp3d",)),
        ):
            if captured is None:
                continue
            _replace_child(spPr, tags, _copy_of(captured))
            touched = True

    outcome = textframe.align_text(
        shape,
        anchor=style.anchor,
        centred=style.anchor_centred,
        insets_in=style.insets_in,
        alignment=style.alignment,
        rtl=style.reading_order,
    )
    if outcome.changed:
        touched = True
    if style.has_font:
        # Into a throwaway result: one "matched the whole format" is the
        # sentence a caller wants, not that plus "matched the whole font style
        # on 6 run(s)" describing part of the same act.
        font_outcome = ArrangeResult()
        _apply_font(shape, MatchAction.FONT_STYLE, style, font_outcome)
        touched = touched or bool(font_outcome.changed)

    if touched:
        result.record("matched the whole format")
    else:
        result.skipped += 1


def _absorb(outcome: ArrangeResult, result: ArrangeResult) -> None:
    for line in outcome.changes:
        result.record(line)
    result.skipped += outcome.skipped


# --------------------------------------------------------------------------- #
# The batch
# --------------------------------------------------------------------------- #

def make_same(
    shapes: Sequence[Any],
    action: MatchAction,
    *,
    mode: ReferenceMode = ReferenceMode.FIRST_SHAPE,
    slide_size: Optional[tuple[int, int]] = None,
) -> ArrangeResult:
    """Make a set of shapes match the first of them. C#: MakeSame.

    In slide mode the page is the reference and every shape given is written;
    in first-shape mode shape #1 is read and shapes 2..N are written, and the
    reference itself is never touched.

    A shape that will not take the property is counted and logged, and the
    batch carries on. That is the C#'s behaviour and the right one for a deck:
    one grouped shape that refuses a fill should not cost the other eleven
    cards their fill.
    """
    result = ArrangeResult()
    shapes = [shape for shape in (shapes or ()) if shape is not None]

    if mode is ReferenceMode.SLIDE:
        if not supports_slide_reference(action):
            result.message = (
                "a slide reference can only make shapes the same width, "
                "height or size"
            )
            return result
        if not slide_size:
            result.message = "matching the slide needs the page size"
            return result
        if not shapes:
            result.message = "needs at least one shape"
            return result
        style = capture_slide(slide_size)
        targets = shapes
    else:
        if len(shapes) < 2:
            result.message = "needs at least two shapes: one to read, one to write"
            return result
        style = capture(shapes[0], action)
        targets = shapes[1:]

    if action is MatchAction.CORNER_RADIUS and not style.has_corner_radius:
        result.message = "the reference shape has no adjustable rounded corners"
        return result

    for shape in targets:
        try:
            apply_style(shape, action, style, result)
        except Exception:
            result.skipped += 1
            log.debug("skipped %r during make same", shape, exc_info=True)

    result.success = True
    result.message = _message(action, result)
    return result


def _message(action: MatchAction, result: ArrangeResult) -> Optional[str]:
    """C#: BuildMakeSameMessage. Aspect warnings outrank skip counts.

    Worded for a file rather than for a live application. The add-in warns
    that the other dimension "may have changed", because over COM PowerPoint
    changes it as you write. Nothing changes it here, and that is the warning:
    the lock the designer set has been written straight through.
    """
    if result.aspect_locked > 0:
        which = {
            MatchAction.WIDTH: "height",
            MatchAction.HEIGHT: "width",
            MatchAction.SIZE: "proportions",
        }.get(action)
        if which:
            return (
                f"{result.aspect_locked} shape(s) have their aspect ratio "
                f"locked; writing the file bypasses the lock, so check their "
                f"{which}"
            )
    if result.skipped > 0:
        return (
            "1 shape could not be updated." if result.skipped == 1
            else f"{result.skipped} shapes could not be updated."
        )
    return None


# --------------------------------------------------------------------------- #
# Corner radius
# --------------------------------------------------------------------------- #

def corner_radius_of(shape: Any) -> tuple[bool, float]:
    """C#: TryGetCornerRadius. Adjustment handle 1, 0-based here."""
    adjustments = _safe(lambda: shape.adjustments)
    if adjustments is None or _safe(lambda: len(adjustments), 0) < 1:
        return False, 0.0
    value = _safe(lambda: float(adjustments[0]))
    return (True, value) if value is not None else (False, 0.0)


def set_corner_radius(shape: Any, radius: Optional[float]) -> bool:
    """C#: TrySetCornerRadius, without the three COM spellings.

    python-pptx exposes the adjustment as an ordinary item assignment, so the
    original's `SetItem` / `Invoke` / `setattr` ladder -- which exists only
    because a parameterised COM property has no Python syntax -- collapses to
    one line here.
    """
    if radius is None:
        return False
    adjustments = _safe(lambda: shape.adjustments)
    if adjustments is None or _safe(lambda: len(adjustments), 0) < 1:
        return False
    try:
        adjustments[0] = float(radius)
        return True
    except Exception:
        log.debug("could not set the corner radius on %r", shape, exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# XML plumbing
# --------------------------------------------------------------------------- #

def _sp_pr(shape: Any):
    """`a:spPr` on a shape, picture or connector. None for a graphic frame."""
    element = getattr(shape, "_element", None)
    if element is None:
        return None
    return _safe(lambda: element.spPr)


def _fill_parents(shape: Any) -> list:
    """Every element that carries a fill for this shape.

    One for an ordinary shape. One per cell for a table, because a table's
    colour is on its cells and a graphic frame has no `a:spPr` to write at
    all -- the same reason `arrange.text.frame_targets` exists.
    """
    table = textframe.table_of(shape)
    if table is not None:
        parents = []
        for cell in textframe._cells(table):
            properties = _safe(lambda c=cell: c._tc.get_or_add_tcPr())
            if properties is not None:
                parents.append(properties)
        return parents
    spPr = _sp_pr(shape)
    return [spPr] if spPr is not None else []


def _line_of(shape: Any):
    """`a:ln`, made if the shape has none."""
    spPr = _sp_pr(shape)
    if spPr is None:
        return None
    return _safe(lambda: spPr.get_or_add_ln())


def _fill_child(parent: Any):
    return _first_child(parent, FILL_TAGS)


def _first_child(parent: Any, tags: Sequence[str]):
    if parent is None:
        return None
    for child in parent:
        if _local(child.tag) in tags:
            return child
    return None


def _copy_of(element: Any):
    return None if element is None else copy.deepcopy(element)


def _set_fill(parent: Any, fill: Any) -> bool:
    """Put a copied fill element into `parent`, in the position the schema wants."""
    if parent is None or fill is None:
        return False
    return _replace_child(parent, FILL_TAGS, _copy_of(fill))


def _replace_child(parent: Any, tags: Sequence[str], element: Any) -> bool:
    """Swap whichever of `tags` is present for `element`, or insert it in order."""
    if parent is None or element is None:
        return False
    existing = _first_child(parent, tags)
    if existing is not None:
        parent.replace(existing, element)
        return True

    after = _preceding(parent, tags)
    index = 0
    for position, child in enumerate(parent):
        if _local(child.tag) in after:
            index = position + 1
    parent.insert(index, element)
    return True


_SP_PR_HEAD = _FILL_AFTER["spPr"]
_LINE_HEAD = ("round", "bevel", "miter")


def _preceding(parent: Any, tags: Sequence[str]) -> tuple:
    """What must stay in front of `tags` in this parent.

    THE SCHEMA FIXES THE ORDER AND LXML WILL NOT FIX IT FOR US. A `a:ln`
    written before the fill, or an `a:effectLst` written before the `a:ln`,
    gives a file PowerPoint refuses to open -- and refuses without saying
    which element it objected to. Only the insertions this module makes are
    listed; anything else lands at the front, which is where a caller
    inserting something new would have to think about it anyway.
    """
    first = tags[0]
    if first in FILL_TAGS:
        return _FILL_AFTER.get(_local(parent.tag), ())
    if first in ("prstDash", "custDash"):
        return FILL_TAGS
    if first == "headEnd":
        return FILL_TAGS + ("prstDash", "custDash") + _LINE_HEAD
    if first == "tailEnd":
        return FILL_TAGS + ("prstDash", "custDash") + _LINE_HEAD + ("headEnd",)
    if first in ("effectLst", "effectDag"):
        return _SP_PR_HEAD + FILL_TAGS + ("ln",)
    if first == "scene3d":
        return _SP_PR_HEAD + FILL_TAGS + ("ln", "effectLst", "effectDag")
    if first == "sp3d":
        return _SP_PR_HEAD + FILL_TAGS + (
            "ln", "effectLst", "effectDag", "scene3d")
    return ()


def _apply_fill(shape: Any, fill: Any) -> bool:
    """Copy a captured fill onto every fill-bearing part of a shape."""
    changed = False
    for parent in _fill_parents(shape):
        if fill is None:
            existing = _fill_child(parent)
            if existing is not None:
                parent.remove(existing)
                changed = True
            continue
        if _set_fill(parent, fill):
            changed = True
    return changed


def _apply_line(
    shape: Any,
    style: Style,
    *,
    colour: bool = False,
    weight: bool = False,
    dash: bool = False,
    arrows: bool = False,
) -> bool:
    """Copy the requested parts of a captured line onto a shape.

    ONE PART AT A TIME IS THE POINT. The add-in cannot do this -- its only
    faithful route for a line colour is the format painter, which copies the
    weight and the dash and the arrowheads too, so it snapshots those first
    and puts them back afterwards. Here the colour is one child of `a:ln` and
    the weight is one attribute, so "the colour and nothing else" is simply
    the colour and nothing else.
    """
    if not style.has_line_capture:
        return False
    line = _line_of(shape)
    if line is None:
        return False

    changed = False
    if colour:
        if style.line_fill is not None:
            if _set_fill(line, style.line_fill):
                changed = True
        else:
            existing = _fill_child(line)
            if existing is not None:
                line.remove(existing)
                changed = True
    if weight:
        if style.line_width is None:
            if line.get("w") is not None:
                del line.attrib["w"]
                changed = True
        elif line.get("w") != style.line_width:
            line.set("w", style.line_width)
            changed = True
    if dash:
        for name, value in (("cap", style.line_cap), ("cmpd", style.line_compound)):
            if value is None:
                if line.get(name) is not None:
                    del line.attrib[name]
                    changed = True
            elif line.get(name) != value:
                line.set(name, value)
                changed = True
        if style.line_dash is not None:
            _replace_child(line, ("prstDash", "custDash"), _copy_of(style.line_dash))
            changed = True
    if arrows:
        for captured, tags in ((style.head_end, ("headEnd",)),
                               (style.tail_end, ("tailEnd",))):
            if captured is None:
                continue
            _replace_child(line, tags, _copy_of(captured))
            changed = True
    return changed
