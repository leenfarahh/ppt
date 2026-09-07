"""Keep a photograph's frame and its cropped-to shape across a layout swap.

The bug, from a real deck: a portrait cropped to a circle came out square and
hard against the left margin, while the decorative arcs drawn around it stayed
exactly where they were. Nothing had gone wrong with the copy. The circle and
the position were never on that slide. They were on the OLD layout's picture
placeholder, and the slide's `p:pic` carried only the image, the crop and
`p:ph idx="N"`. Point that at another layout and both are simply gone:
geometry falls back to the implicit `rect`, and the frame to whatever the new
layout puts at that idx -- the origin, when it puts nothing there. The arcs
came through untouched because they are ordinary autoshapes carrying their own
`a:xfrm`, which is the whole difference.

So the inherited values are written onto the shape while the old layout is
still reachable, and the `p:ph` is then removed. Removing it matters as much as
baking the values in: left in place it is a standing invitation to inherit
again, from a layout that knows nothing about this shape.

This lives in a module of its own because both rebuild routes need it and
neither can reach the other: `builder` imports `master_apply`, so
`master_apply` cannot import `builder`.

WHY IT IS DONE ON THE FILE FOR THE POWERPOINT ROUTE, and not through COM.
Automation can read the designed geometry -- ask about the layout placeholder
and COM answers `msoShapeOval` where it answers `msoShapeNotPrimitive` for the
slide's own shape -- and it can write a preset back through `AutoShapeType`.
What it cannot do is carry a `a:custGeom` mask, which COM has no vocabulary
for: it reports `msoShapeNotPrimitive` for a drawn shape and for an inherited
one alike, so the two are indistinguishable and a custom mask is silently
lost. Baking the geometry into the file first is the only form of this fix
that does not depend on how the circle happens to be authored.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def freeze_file(path: Path) -> int:
    """Freeze every slide in the file at `path`, in place. Best effort.

    Returns the number of shapes changed, and 0 both when there was nothing to
    do and when the file could not be handled -- the caller carries on either
    way, because the fallbacks that remain are the old behaviour rather than a
    broken file.

    The caller must hand this a disposable copy. It rewrites what it is given.

    Saving nothing when nothing was frozen is deliberate, not an optimisation.
    A deck with no picture placeholders reaches PowerPoint byte for byte as
    the designer saved it, which is the property `master_apply` is built
    around and worth keeping for every deck that does not need this.
    """
    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    try:
        presentation = Presentation(str(path))
        frozen = [name for slide in presentation.slides for name in freeze_slide(slide)]
        if not frozen:
            return 0
        presentation.save(str(path))
    except Exception:
        log.warning(
            "could not freeze the picture geometry in %s; a photograph cropped "
            "to a shape by its layout may come out square",
            path.name, exc_info=True,
        )
        return 0
    log.info("froze the inherited frame of %d picture(s): %s", len(frozen),
             ", ".join(frozen))
    return len(frozen)


def _name_of(shape: Any) -> str:
    try:
        return str(shape.name)
    except Exception:
        return "unnamed shape"


def _is_placeholder(shape: Any) -> bool:
    try:
        return bool(shape.is_placeholder)
    except Exception:
        return False



def freeze_slide(src_slide: Any) -> list[str]:
    """Make a picture placeholder's inherited look explicit, then un-placeholder it.

    The bug, from a real deck: a portrait cropped to a circle came out square
    and hard against the left margin, while the decorative arcs drawn around
    it stayed exactly where they were. Nothing had gone wrong with the copy.
    The circle and the position were never on that slide. They were on the OLD
    layout's picture placeholder, and the slide's `p:pic` carried only the
    image, the crop and `p:ph idx="N"`. Point that at another layout and both
    are simply gone: geometry falls back to the implicit `rect`, and the frame
    to whatever the new layout puts at that idx -- the origin, when it puts
    nothing there. The arcs came through untouched because they are ordinary
    autoshapes carrying their own `a:xfrm`, which is the whole difference.

    So the inherited values are written onto the shape while the old layout is
    still reachable, and the `p:ph` is then removed. Removing it matters as
    much as baking the values in: left in place it is a standing invitation to
    inherit again, from a layout that knows nothing about this shape.

    Only placeholders holding an image are touched, never one holding words. A
    text placeholder taking the new layout's type and position is the entire
    point of the rebuild, and freezing one would reproduce exactly the drift
    this exists to remove.

    Returns the names of the shapes it changed. Mutates `src_slide`, which is
    a throwaway in-memory read of the source file and is never written back.
    """
    frozen: list[str] = []
    for shape in list(src_slide.shapes):
        if not _is_placeholder(shape) or not _carries_picture(shape):
            continue
        if _freeze(shape, _layout_placeholder(shape, src_slide)):
            frozen.append(_name_of(shape))
    return frozen


def _carries_picture(shape: Any) -> bool:
    """A shape whose content is an image rather than words.

    Both ways of authoring one count: `p:pic`, and the autoshape with a
    picture fill that PowerPoint writes when a photo is dropped onto a shape.

    Any text disqualifies it. A titled panel that happens to have a photo fill
    is a text placeholder, and its words moving into the new layout's type is
    what has to happen to it.
    """
    element = getattr(shape, "_element", None)
    if element is None:
        return False
    if _text_of(shape).strip():
        return False
    return element.find(f".//{_A_NS}blip") is not None


def _text_of(shape: Any) -> str:
    try:
        return str(shape.text_frame.text) if shape.has_text_frame else ""
    except Exception:
        return ""


def _freeze(shape: Any, layout_ph: Optional[Any]) -> bool:
    """Bake the frame, then the look, then drop the `p:ph`."""
    spPr = getattr(shape._element, "spPr", None)
    if spPr is None:
        return False
    _bake_frame(spPr, _frame_of(shape))
    if layout_ph is not None:
        _bake_look(spPr, getattr(layout_ph._element, "spPr", None))
    return _strip_ph(shape._element)


def _frame_of(shape: Any) -> Optional[tuple[int, int, int, int]]:
    """Where the shape actually sits, resolving what it inherits.

    python-pptx reports the effective value for a placeholder -- its own where
    it has one, otherwise the layout's, otherwise the master's -- and that
    chain is the whole reason this works, because a picture placeholder in a
    designed template carries none of the four itself.
    """
    try:
        frame = (shape.left, shape.top, shape.width, shape.height)
    except Exception:
        # `LayoutPlaceholder._base_placeholder` looks its type up in a table
        # and raises KeyError on one the table does not list, which a
        # hand-built template can carry.
        return None
    if any(value is None for value in frame):
        return None
    return (int(frame[0]), int(frame[1]), int(frame[2]), int(frame[3]))


def _bake_frame(spPr: Any, frame: Optional[tuple[int, int, int, int]]) -> None:
    """Write an explicit `a:xfrm`, unless the shape already had one."""
    if frame is None or spPr.xfrm is not None:
        return
    xfrm = spPr.get_or_add_xfrm()
    xfrm.x, xfrm.y, xfrm.cx, xfrm.cy = frame


def _bake_look(spPr: Any, source: Optional[Any]) -> None:
    """Take the geometry, outline and shadow the layout was supplying.

    Geometry is one or the other: `a:custGeom` and `a:prstGeom` are a schema
    choice, so copying both writes a file PowerPoint refuses. Custom wins
    where a layout has one, because a shape that needed a drawn outline was
    never going to be describable as a preset.

    Anything the slide states for itself is left alone throughout. It was
    overriding the layout before the move and it still should after.
    """
    if source is None:
        return
    if spPr.prstGeom is None and spPr.custGeom is None:
        for name in ("custGeom", "prstGeom"):
            geometry = source.find(f"{_A_NS}{name}")
            if geometry is not None:
                getattr(spPr, f"_insert_{name}")(copy.deepcopy(geometry))
                break
    for name in ("ln", "effectLst"):
        if spPr.find(f"{_A_NS}{name}") is not None:
            continue
        inherited = source.find(f"{_A_NS}{name}")
        if inherited is not None:
            getattr(spPr, f"_insert_{name}")(copy.deepcopy(inherited))


def _layout_placeholder(shape: Any, src_slide: Any) -> Optional[Any]:
    """The source layout's placeholder this shape has been inheriting from."""
    try:
        return src_slide.slide_layout.placeholders.get(idx=shape._element.ph_idx)
    except Exception:
        return None


def _strip_ph(element: Any) -> bool:
    """Turn the shape into an ordinary floating one.

    Not cosmetic. While the `p:ph` is there the shape is a placeholder on a
    slide whose layout has changed, so PowerPoint resolves anything it does
    not state against the wrong layout, and `_claim` offers it a target that
    an image cannot be copied into -- which is its own silent loss, because
    `_copy_text` finds no text frame on either side and returns having done
    nothing, while the shape is reported as filled.
    """
    ph = element.ph
    if ph is None:
        return False
    parent = ph.getparent()
    if parent is None:
        return False
    parent.remove(ph)
    return True


