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
import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

from lxml import etree

log = logging.getLogger(__name__)

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R_LINK = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/"
    "relationships}link"
)
_R_EMBED = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/"
    "relationships}embed"
)
_IMAGE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
)


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
        # Same pass and same save. Both exist for one reason -- what a slide
        # inherits from the layout it is about to stop pointing at -- and a
        # deck needing neither still reaches PowerPoint byte for byte.
        carried = inherit_artwork(presentation)
        if not frozen and not carried:
            return 0
        presentation.save(str(path))
    except Exception:
        log.warning(
            "could not freeze the picture geometry in %s; a photograph cropped "
            "to a shape by its layout may come out square",
            path.name, exc_info=True,
        )
        return 0
    if frozen:
        log.info("froze the inherited frame of %d picture(s): %s", len(frozen),
                 ", ".join(frozen))
    if carried:
        log.info(
            "carried %d picture(s) off the old layouts onto the slides that "
            "inherit them, so the rebuild does not leave them behind: %s",
            len(carried), ", ".join(carried),
        )
    return len(frozen) + len(carried)


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


# --------------------------------------------------------------------------- #
# Artwork the slide inherits from its layout
# --------------------------------------------------------------------------- #
#
# What a designed slide shows is often not on the slide. It is on the layout,
# and the slide inherits it: a section divider carries a full-bleed photograph,
# a pair of connector lines and the grey panels behind its copy while its own
# `p:cSld` holds nothing but a title. Point that slide at another layout and
# all of it is gone -- not dropped, never there, so nothing reports it and no
# shape was lost.
#
# Measured on a deck built for it:
#
#     before   layout carries  Rounded Rectangle, Connector, Oval, Picture
#     after    slide shows     nothing of them
#
# So the artwork is copied onto the slide while the old layout is still
# reachable, which is the same move `freeze_slide` makes for a placeholder's
# frame and for the same reason.
#
# EVERY non-placeholder shape, not only pictures. The first version of this
# carried pictures alone and a real deck came back with its photographs intact
# and its connector lines and panel fills missing, which is the same defect
# wearing a different tag name. A placeholder is excluded because it is a slot
# rather than artwork, and taking the new layout's position and type for those
# is the entire point of a rebuild.
#
# WHAT IS NOT CARRIED, and why there has to be a rule at all. The old layouts
# hold two kinds of shape and only one should travel. A section photograph and
# the panels drawn for that slide belong to the slide; the old brand's logo and
# its header band belong to the design being replaced, and carrying those would
# stamp the previous client's mark on every slide of a rebranded deck -- worse
# than the missing artwork, and harder to spot.
#
# They are told apart by repetition rather than by size, which is the signal
# the file actually carries: furniture is on many layouts because it is on
# every slide, and a section's own artwork is on the one layout drawn for it.

def inherit_artwork(presentation: Any) -> list[str]:
    """Copy each slide's inherited layout artwork onto the slide itself.

    Returns a line per shape copied. Mutates the presentation.
    """
    furniture = _repeated_shapes(presentation)
    carried: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        artwork = _artwork_of(slide.slide_layout, furniture)
        # Kept in the layout's own order, and inserted at the front of the
        # z-order where a layout draws: under the slide's own shapes, which is
        # where these were. A panel arriving on top of the copy it used to sit
        # behind is a different defect from the one being fixed.
        for offset, shape in enumerate(artwork):
            if _copy_onto(slide, shape, _Z_FRONT + offset):
                carried.append(f"slide {index}: {_name_of(shape)}")
    return carried


# `p:spTree` opens with `p:nvGrpSpPr` and `p:grpSpPr`; shapes follow.
_Z_FRONT = 2


def _repeated_shapes(presentation: Any) -> set[str]:
    """The old brand's furniture, which does not travel.

    Counted over SLIDES, not over layouts, and that is the whole correction. A
    layout count says furniture is what several layouts share -- true of a
    deck with many layouts, and useless for the common one where every slide
    sits on the same layout. On a real 12-slide deck that meant its
    background picture, its logo, its slide-number box and two footer text
    boxes were each on exactly one layout, counted as content, and carried
    onto all twelve slides. The master applied underneath them and the output
    looked like the deck that came in.

    What a slide inherits is what a slide inherits: chrome reaches nearly
    every slide because that is what chrome is for, and a section's own
    artwork reaches the handful of slides in that section. So a shape most of
    the deck inherits is the brand's, and one a few slides inherit is theirs.

    The layout count is kept as well. A logo on nine layouts is furniture even
    when the slides using them are spread unevenly, and that reading costs
    nothing to keep. So is anything on a slide master, which is drawn once for
    the whole deck and is therefore the brand's by construction.

    The limit worth knowing: chrome sitting on a layout only one slide uses
    reaches one slide, and nothing here can tell that from that slide's own
    artwork. It errs toward carrying, which is the safer way round -- one
    stray shape on one slide is a smaller wrong than a section image
    stripped out of every slide that needed it.
    """
    by_layout: dict[str, set[int]] = {}
    on_master: set[str] = set()
    for master in _safe(lambda: list(presentation.slide_masters)) or []:
        # Whatever sits on a slide master is the brand's by construction: it
        # is drawn once for the whole deck. No counting needed.
        for shape in _loose_shapes(master):
            key = _content_key(shape)
            if key:
                on_master.add(key)
        for layout in _safe(lambda: list(master.slide_layouts)) or []:
            for shape in _loose_shapes(layout):
                key = _content_key(shape)
                if key:
                    by_layout.setdefault(key, set()).add(id(layout))

    slides = _safe(lambda: list(presentation.slides)) or []
    reach: dict[str, int] = {}
    for slide in slides:
        layout = _safe(lambda s=slide: s.slide_layout)
        if layout is None:
            continue
        for shape in _loose_shapes(layout):
            key = _content_key(shape)
            if key:
                reach[key] = reach.get(key, 0) + 1

    ceiling = max(1, int(len(slides) * _CHROME_SHARE))
    return {
        key
        for key, layouts in by_layout.items()
        if key in on_master
        or len(layouts) > 1
        or reach.get(key, 0) > ceiling
    } | on_master


# The share of a deck a shape has to reach before it is the brand's furniture
# rather than a section's artwork. Above half: a divider image belongs to the
# handful of slides in its section, and a logo belongs to all of them.
_CHROME_SHARE = 0.5


def _artwork_of(layout: Any, furniture: set[str]) -> list[Any]:
    """The shapes on a layout that belong to the slide rather than to the brand."""
    return [
        shape for shape in _loose_shapes(layout)
        if _content_key(shape) not in furniture
    ]


def _loose_shapes(container: Any) -> list[Any]:
    """Non-placeholder shapes directly on a layout.

    Placeholders are excluded on purpose: one is a slot, not artwork, and the
    slide's own shape is what fills it.
    """
    try:
        return [s for s in container.shapes if not _is_placeholder(s)]
    except Exception:
        return []


def _content_key(shape: Any) -> Optional[str]:
    """What a shape draws, identified by content so copies collapse together.

    A picture is its image. Anything else is its markup with the identifiers
    stripped out -- the shape id, the name and the creation GUID all differ
    between two copies of one band and say nothing about what it looks like.
    """
    try:
        sha1 = _safe(lambda: shape.image.sha1)
        if sha1:
            return f"image:{sha1}"
    except Exception:
        pass
    try:
        element = copy.deepcopy(shape._element)
        for node in element.iter():
            for attribute in ("id", "name"):
                if attribute in node.attrib:
                    del node.attrib[attribute]
        for ext in element.iter(f"{_A_NS}extLst"):
            ext.getparent().remove(ext)
        return "xml:" + hashlib.sha1(
            etree.tostring(element)
        ).hexdigest()
    except Exception:
        return None


def _copy_onto(slide: Any, shape: Any, position: int) -> bool:
    """Put a copy of a layout's shape on the slide at `position` in the z-order.

    Any part it points at is related to the slide rather than copied, so a
    photograph on nine slides is still one image in the file.

    EVERY relationship in the copied subtree, not just the one on `a:blip`.
    An icon from PowerPoint's library carries two: the `a:blip` and, inside
    its extension list, an `asvg:svgBlip` pointing at the SVG it actually
    draws from. Re-pointing only the first left the second holding a
    relationship id that means something else on the slide, or nothing at all,
    and the icon arrived as a broken-image box. The tag is not worth matching
    on -- what matters is that the attribute is there.
    """
    try:
        element = copy.deepcopy(shape._element)
        for node in element.iter():
            for attribute in (_R_EMBED, _R_LINK):
                rid = node.get(attribute)
                if not rid:
                    continue
                part = shape.part.related_part(rid)
                node.set(
                    attribute,
                    slide.part.relate_to(part, _rel_type_of(shape.part, rid)),
                )
        slide.shapes._spTree.insert(position, element)
        return True
    except Exception:
        log.debug("could not carry a layout shape onto a slide", exc_info=True)
        return False


def _rel_type_of(part: Any, rid: str) -> str:
    """The relationship type the source used, so the copy declares the same.

    An SVG and its raster fallback are both images, but reading the type back
    rather than assuming it means a relationship this does not know about is
    carried across intact instead of being relabelled.
    """
    try:
        return str(part.rels[rid].reltype)
    except Exception:
        return _IMAGE_REL


def _safe(call):
    try:
        return call()
    except Exception:
        return None
