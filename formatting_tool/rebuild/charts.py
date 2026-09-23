"""A chart is one element, and it crosses the restyle exactly as it arrived.

THE DEFECT. A designer ran a deck through and got its charts back with bars
missing, gridlines gone and an axis half there. Nothing had failed. Every
piece of the restyle that deletes or moves shapes had done what it was built
to do, one shape at a time:

    `series.furniture_of`   a bar is a wordless box no bigger than a label,
                            sitting in the block of a run of data labels, so
                            it read as the run's drawing and was deleted with
                            it
    `claim_runs`            the data labels ARE a run -- five like-sized boxes
                            in a row -- and were claimed into a layout's list
                            and removed from the plot
    `_transplant`           a native chart points at a chart part, which the
                            XML route did not know how to carry, so the whole
                            chart was left behind
    `apply.palette`         every series colour inside the chart part was
                            snapped to the nearest brand entry, which is how
                            two series land on one colour

The model's roles were supposed to hold this back, and did only for the parts
that carry words: `_leave_alone` matched protected shapes by their COPY, and a
bar, a gridline and an axis have none. `SlideRoles.protects`, which asks by
position, was written and never called.

So a chart is decided once, as a whole, and everything inside it goes with it:

  - a native chart (`p:graphicFrame` holding `c:chart` or `cx:chart`) is never
    claimed, deleted, recoloured or re-placed, and is copied part and all;
  - any shape sitting inside a native chart's frame is part of that chart --
    a callout line, a label drawn over a bar;
  - any shape sitting inside a region the model called a chart is part of it,
    whether or not it carries words, which is what covers a chart drawn as
    forty rectangles.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger(__name__)

_EMU = 914400

_P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_GRAPHIC_FRAME = f"{_P_NS}graphicFrame"

# What a graphic frame's `a:graphicData/@uri` says when it holds a chart: the
# classic chart part, and the 2016 "chartex" family (waterfall, treemap,
# sunburst, funnel) PowerPoint writes under its own namespace.
CHART_URIS = frozenset({
    "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "http://schemas.microsoft.com/office/drawing/2014/chartex",
})

# The last segment of the relationship types that point at a chart part.
CHART_KINDS = frozenset({"chart", "chartEx"})

# How much of a shape has to sit inside a chart's frame before it is part of
# the chart. The same share `ai.roles` uses, for the same reason: a label half
# in and half out is not inside anything.
_INSIDE_SHARE = 0.75

Box = tuple[float, float, float, float]      # left, top, right, bottom, inches


def is_chart(shape: Any) -> bool:
    """Whether this shape is a native chart."""
    element = getattr(shape, "_element", shape)
    if getattr(element, "tag", None) != _GRAPHIC_FRAME:
        return False
    data = element.find(f"{_A_NS}graphic/{_A_NS}graphicData")
    return data is not None and data.get("uri") in CHART_URIS


def box_of(shape: Any) -> Optional[Box]:
    """Where a shape sits, in inches, or None for one that will not say."""
    try:
        left, top = float(shape.left or 0), float(shape.top or 0)
        width, height = float(shape.width or 0), float(shape.height or 0)
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return (
        left / _EMU, top / _EMU, (left + width) / _EMU, (top + height) / _EMU,
    )


def chart_boxes(shapes: Iterable[Any]) -> list[Box]:
    """The frames of the native charts among these shapes."""
    return [
        box for box in (box_of(shape) for shape in shapes if is_chart(shape))
        if box is not None
    ]


def inside(box: Optional[Box], regions: Sequence[Box]) -> bool:
    """Whether `box` sits within one of `regions`."""
    if box is None or not regions:
        return False
    area = (box[2] - box[0]) * (box[3] - box[1])
    if area <= 0:
        return False
    for region in regions:
        across = min(box[2], region[2]) - max(box[0], region[0])
        down = min(box[3], region[3]) - max(box[1], region[1])
        if across > 0 and down > 0 and (across * down) / area >= _INSIDE_SHARE:
            return True
    return False


def part_of_a_chart(
    shape: Any, charts: Sequence[Box] = (), here: Optional[Any] = None,
) -> bool:
    """Whether this shape is a chart or any piece of one.

    `charts` is the native chart frames on the slide, from `chart_boxes`.
    `here` is `ai.roles.SlideRoles` for the slide, or None: the regions the
    model called a chart, which is the only way a chart drawn as ordinary
    shapes can be recognised at all.
    """
    if is_chart(shape):
        return True
    box = box_of(shape)
    if inside(box, charts):
        return True
    if here is not None and getattr(here, "reviewed", False):
        try:
            return bool(here.inside_a_chart(box))
        except Exception:
            return False
    return False


def unpin(shape: Any) -> bool:
    """Take a chart out of its placeholder, so no layout can re-place it.

    A chart dropped into a content placeholder is still "content placeholder
    2" to PowerPoint, and applying a new layout moves it into whatever that
    layout's idx 2 is -- resized, re-placed, and on a layout that puts a
    photograph there, under a photograph. A graphic frame always states its
    own `p:xfrm`, so removing the `p:ph` loses nothing it was inheriting.
    """
    if not is_chart(shape):
        return False
    ph = getattr(shape._element, "ph", None)
    if ph is None:
        return False
    parent = ph.getparent()
    if parent is None:
        return False
    parent.remove(ph)
    return True


# --------------------------------------------------------------------------- #
# Copying a chart into another package
# --------------------------------------------------------------------------- #

def clone_chart(rel: Any, tgt_part: Any) -> str:
    """Copy the chart `rel` points at into `tgt_part`'s package. Returns the rId.

    The chart part, and everything it points at -- the embedded workbook its
    data is edited in, its style and colour parts, a drawing of user shapes --
    each given a free name in the target package and related to the copy, so
    the chart opens in PowerPoint exactly as it did and "Edit Data" still
    finds its numbers.

    Every part is copied byte for byte under the content type the source
    declared, a picture-filled bar's image included: nothing is re-sniffed, so
    nothing can be relabelled on the way (see `builder._reimport_image` for
    what relabelling costs). Raises on anything it cannot carry, and the
    caller then reports the chart rather than inserting it half-built.
    """
    memo: dict[int, Any] = {}
    copied = _clone_part(rel.target_part, tgt_part.package, memo)
    rid = tgt_part.relate_to(copied, rel.reltype)
    _clone_children(rel.target_part, copied, memo)
    return rid


def _clone_part(source: Any, package: Any, memo: dict[int, Any]) -> Any:
    from pptx.opc.package import PartFactory  # noqa: PLC0415 - opc internals
    from pptx.opc.packuri import PackURI  # noqa: PLC0415

    partname = package.next_partname(_template(str(source.partname)))
    copied = PartFactory(
        PackURI(str(partname)), source.content_type, package, source.blob,
    )
    memo[id(source)] = copied
    return copied


def _clone_children(source: Any, copied: Any, memo: dict[int, Any]) -> None:
    """Relate the copy to copies of everything the source points at.

    Related one at a time, each before the next is named: `next_partname`
    only sees parts already reachable from the package, so naming two before
    relating either would give them the same name.

    The copy's own rIds are then rewritten from a map in one pass, never by
    successive replacement -- the new part numbers its relationships from 1,
    and replacing rId2 with rId1 and then rId1 with rId2 swaps them back.
    """
    mapping: dict[str, str] = {}
    for rid, rel in list(source.rels.items()):
        if rel.is_external:
            mapping[rid] = copied.relate_to(
                rel.target_ref, rel.reltype, is_external=True,
            )
            continue
        target = rel.target_part
        seen = id(target) in memo
        child = memo[id(target)] if seen else _clone_part(target, copied.package, memo)
        mapping[rid] = copied.relate_to(child, rel.reltype)
        if not seen:
            _clone_children(target, child, memo)
    _rewrite_rids(copied, mapping)


def _rewrite_rids(part: Any, mapping: dict[str, str]) -> None:
    if not mapping:
        return
    element = getattr(part, "_element", None)
    if element is None:
        if part.rels:
            # A binary part that points at others would need its bytes edited,
            # and nothing a chart carries is one.
            raise ValueError(f"cannot re-point the references inside {part.partname}")
        return
    for node in element.iter():
        for attr, value in list(node.attrib.items()):
            if attr.startswith(_R_NS) and value in mapping:
                node.set(attr, mapping[value])


def _template(partname: str) -> str:
    """`/ppt/charts/chart3.xml` -> `/ppt/charts/chart%d.xml`, for `next_partname`."""
    head, dot, ext = partname.rpartition(".")
    if not dot:
        head, ext = partname, ""
    head = re.sub(r"\d+$", "", head)
    return f"{head}%d{dot}{ext}"
