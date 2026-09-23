"""Related icons on a slide, drawn in one colour scheme.

THE DEFECT, OFF A REAL SLIDE. Two cards side by side, each with an icon above
its heading. One icon was the brand's own SVG, navy with a green accent; the
other was three loose freeform shapes in brown and gold. The palette sweep did
its job on both -- every colour landed on a palette entry -- and the brown and
gold came out blue and pale grey. Each colour was on the brand. The pair was
not: two icons meant to be the same kind of thing, drawn in two schemes, one
of them nearly invisible on its card.

The sweep cannot see that, because it moves colours one value at a time and
has no idea two shapes are siblings. This asks the question it cannot: which
icons on this slide belong together, and which of them has the colours the
rest should wear.

WHICH ICONS. An SVG graphic, a group made only of freeforms and SVGs, or a
cluster of loose freeform shapes that touch -- which is how an icon arrives
from a deck built in Google Slides. Never a plain rectangle or circle: those
are legend swatches, timeline dots and status pills, and a set of those in
different colours usually means something. Related means similar in size and
sharing a row or a column, which is what a row of cards or a list of items
looks like.

WHICH ONE IS RIGHT. The icon whose ORIGINAL colours sat closest to the
palette, measured before the sweep touched anything: the brand's own navy and
green were already brand colours, and the brown was not. So the colour rules
run first, as they always have, and the set then takes the scheme of the
member that needed them least. Where that does not decide it, the scheme most
of the set already shares; then an SVG over loose shapes, since an SVG is
usually from the brand's own icon library; then reading order.

HOW. Tone for tone. Each icon's colours are ranked darkest to lightest and
matched to the reference's by rank, so a two-tone icon keeps two tones -- its
outline takes the reference's outline colour and its accent the reference's
accent -- instead of being flattened into one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..colorutil import nearest_palette_entry, relative_luminance

log = logging.getLogger(__name__)

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SRGB = f"{_A_NS}srgbClr"
_EMU = 914400

# An icon is small. Past this on its longer side it is an illustration or a
# panel, and recolouring it to match a neighbour is a composition decision.
_MAX_ICON_IN = 1.2

# Freeforms this close count as one drawing: the gap between the two hands of
# a "hands holding a house" icon is 0.06in.
_TOUCH_IN = 0.08

# Two icons are the same kind of thing if the larger is at most this much the
# size of the smaller, and share a row or a column to this tolerance.
_SIZE_RATIO = 1.6
_LINE_IN = 0.25


@dataclass
class IconUnit:
    """One icon: the shapes that draw it, and the colours it draws in."""

    shapes: list[Any]
    box: tuple[float, float, float, float]      # left, top, width, height (in)
    svg: bool = False
    # colour -> weight: area for a shape's fill, statement count for an SVG
    weights: dict[str, float] = field(default_factory=dict)

    @property
    def colors(self) -> list[str]:
        return list(self.weights)


@dataclass
class HarmonyResult:
    icons: int = 0                      # icons recoloured
    sets: int = 0                       # related sets settled
    lines: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Finding the icons
# --------------------------------------------------------------------------- #

def icon_units(slide: Any) -> list[IconUnit]:
    """Every icon on a slide, top-level, each with its literal colours."""
    from .. import svgicon  # noqa: PLC0415

    units: list[IconUnit] = []
    loose: list[Any] = []
    for shape in _safely(lambda: list(slide.shapes)) or []:
        box = _box(shape)
        if box is None or max(box[2], box[3]) > _MAX_ICON_IN or _has_text(shape):
            continue
        if _safely(lambda s=shape: svgicon.svg_part(s)) is not None:
            unit = IconUnit([shape], box, svg=True)
            _svg_weights(shape, unit.weights)
            if unit.weights:
                units.append(unit)
        elif _is_group(shape):
            leaves = list(_leaves(shape))
            if leaves and all(_is_freeform(l) or svgicon.svg_part(l) is not None
                              for l in leaves):
                unit = IconUnit([shape], box)
                for leaf in leaves:
                    if svgicon.svg_part(leaf) is not None:
                        _svg_weights(leaf, unit.weights)
                        unit.svg = True
                    else:
                        _fill_weights(leaf, unit.weights)
                if unit.weights:
                    units.append(unit)
        elif _is_freeform(shape):
            loose.append(shape)

    for cluster in _clusters(loose):
        box = _union([_box(s) for s in cluster])
        if max(box[2], box[3]) > _MAX_ICON_IN:
            continue
        unit = IconUnit(cluster, box)
        for shape in cluster:
            _fill_weights(shape, unit.weights)
        if unit.weights:
            units.append(unit)
    return units


def related_sets(units: list[IconUnit]) -> list[list[IconUnit]]:
    """Icons that are the same kind of thing: alike in size, in one line."""
    sets: list[list[IconUnit]] = []
    placed: set[int] = set()
    ordered = sorted(units, key=lambda u: (u.box[1], u.box[0]))
    for i, unit in enumerate(ordered):
        if i in placed:
            continue
        members = [unit]
        for j in range(i + 1, len(ordered)):
            if j in placed:
                continue
            other = ordered[j]
            if _alike(unit, other) and any(_in_line(m, other) for m in members):
                members.append(other)
                placed.add(j)
        if len(members) >= 2:
            placed.add(i)
            sets.append(members)
    return sets


def _alike(a: IconUnit, b: IconUnit) -> bool:
    big_a, big_b = max(a.box[2], a.box[3]), max(b.box[2], b.box[3])
    small = min(big_a, big_b)
    return small > 0 and max(big_a, big_b) / small <= _SIZE_RATIO


def _in_line(a: IconUnit, b: IconUnit) -> bool:
    same_row = abs(a.box[1] - b.box[1]) <= _LINE_IN
    same_column = abs(a.box[0] - b.box[0]) <= _LINE_IN
    return same_row or same_column


# --------------------------------------------------------------------------- #
# Choosing and applying the scheme
# --------------------------------------------------------------------------- #

@dataclass
class _Planned:
    slide: int
    members: list[IconUnit]
    reference: IconUnit


def plan_harmony(presentation: Any, palette: dict[str, str]) -> list[_Planned]:
    """Which icons belong together and which one leads, read BEFORE the sweep.

    Before, because the reference is the icon whose own colours were already
    brand colours -- and after the sweep every icon's colours are.
    """
    plans = []
    for number, slide in enumerate(_safely(lambda: list(presentation.slides)) or [], 1):
        for members in related_sets(icon_units(slide)):
            schemes = {_scheme_key(m) for m in members}
            if len(schemes) < 2 and all(len(m.colors) for m in members):
                continue            # already one scheme
            plans.append(_Planned(number, members, _reference(members, palette)))
    return plans


def apply_harmony(plans: list[_Planned]) -> HarmonyResult:
    """Give every member of each set the reference's colours, as they are NOW.

    Re-read after the sweep, so the scheme copied is the reference's swept
    one: on the palette and matching everything else the sweep settled.
    """
    result = HarmonyResult()
    for plan in plans:
        target = _ranked(_current(plan.reference))
        if not target:
            continue
        moved = 0
        for member in plan.members:
            if member is plan.reference:
                continue
            mine = _ranked(_current(member))
            mapping = _tone_for_tone(mine, target)
            if not any(old != new for old, new in mapping.items()):
                continue
            if _recolor(member, mapping):
                moved += 1
        if moved:
            result.icons += moved
            result.sets += 1
            result.lines.append(
                f"slide {plan.slide}: {moved} icon(s) set in the colours of "
                f"{_name(plan.reference)!r} "
                f"({', '.join('#' + c for c in target)}), so the set reads as one"
            )
    return result


def _reference(members: list[IconUnit], palette: dict[str, str]) -> IconUnit:
    counts: dict[tuple, int] = {}
    for member in members:
        counts[_scheme_key(member)] = counts.get(_scheme_key(member), 0) + 1

    def score(member: IconUnit):
        distances = [
            nearest_palette_entry(c, palette)[1] or 0.0 for c in member.colors
        ] if palette else [0.0]
        drift = sum(distances) / max(len(distances), 1)
        return (
            round(drift, 1),                    # already on the brand
            -counts[_scheme_key(member)],       # what most of the set wears
            0 if member.svg else 1,             # the brand's icon library
            member.box[1], member.box[0],       # reading order
        )

    return min(members, key=score)


def _scheme_key(unit: IconUnit) -> tuple:
    return tuple(sorted(unit.colors))


def _ranked(weights: dict[str, float]) -> list[str]:
    """Colours darkest first."""
    return sorted(weights, key=lambda c: relative_luminance(c) or 0.0)


def _tone_for_tone(mine: list[str], theirs: list[str]) -> dict[str, str]:
    """Each of my colours onto the reference colour of the same rank."""
    if not mine or not theirs:
        return {}
    if len(mine) == 1:
        return {mine[0]: theirs[0]}
    last = len(mine) - 1
    return {
        colour: theirs[round(i * (len(theirs) - 1) / last)]
        for i, colour in enumerate(mine)
    }


def _current(unit: IconUnit) -> dict[str, float]:
    """The unit's colours as they are now, re-read from its shapes."""
    from .. import svgicon  # noqa: PLC0415

    weights: dict[str, float] = {}
    for shape in unit.shapes:
        for leaf in (_leaves(shape) if _is_group(shape) else [shape]):
            if svgicon.svg_part(leaf) is not None:
                _svg_weights(leaf, weights)
            else:
                _fill_weights(leaf, weights)
    return weights


def _recolor(unit: IconUnit, mapping: dict[str, str]) -> bool:
    from .. import svgicon  # noqa: PLC0415

    changed = False
    for shape in unit.shapes:
        for leaf in (_leaves(shape) if _is_group(shape) else [shape]):
            part = svgicon.svg_part(leaf)
            if part is not None:
                try:
                    markup = part.blob.decode("utf-8", "ignore")
                except Exception:
                    continue
                markup, count = svgicon.recolor_many(markup, mapping)
                if count:
                    part._blob = markup.encode("utf-8")
                    changed = True
                continue
            element = getattr(leaf, "_element", None)
            if element is None:
                continue
            for node in list(element.iter(_SRGB)):
                value = str(node.get("val") or "").upper()
                new = mapping.get(value)
                if new and new != value:
                    node.set("val", new)
                    changed = True
    return changed


# --------------------------------------------------------------------------- #
# Reading shapes
# --------------------------------------------------------------------------- #

def _svg_weights(shape: Any, weights: dict[str, float]) -> None:
    from .. import svgicon  # noqa: PLC0415

    part = svgicon.svg_part(shape)
    if part is None:
        return
    try:
        markup = part.blob.decode("utf-8", "ignore")
    except Exception:
        return
    for colour in svgicon.colors_in(markup):
        if colour.theme or not colour.hex:
            continue            # theme-bound: follows the master already
        weights[colour.hex] = weights.get(colour.hex, 0.0) + 1.0


def _fill_weights(shape: Any, weights: dict[str, float]) -> None:
    element = getattr(shape, "_element", None)
    box = _box(shape)
    if element is None or box is None:
        return
    area = max(box[2] * box[3], 1e-4)
    for node in element.iter(_SRGB):
        value = str(node.get("val") or "").upper()
        if len(value) == 6:
            weights[value] = weights.get(value, 0.0) + area


def _clusters(shapes: list[Any]) -> list[list[Any]]:
    """Loose freeforms grouped into the drawings they make together."""
    boxes = [_box(s) for s in shapes]
    parent = list(range(len(shapes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            if _near(boxes[i], boxes[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list[Any]] = {}
    for i, shape in enumerate(shapes):
        groups.setdefault(find(i), []).append(shape)
    return list(groups.values())


def _near(a, b) -> bool:
    return not (
        a[0] > b[0] + b[2] + _TOUCH_IN or b[0] > a[0] + a[2] + _TOUCH_IN
        or a[1] > b[1] + b[3] + _TOUCH_IN or b[1] > a[1] + a[3] + _TOUCH_IN
    )


def _union(boxes) -> tuple[float, float, float, float]:
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[0] + b[2] for b in boxes)
    bottom = max(b[1] + b[3] for b in boxes)
    return left, top, right - left, bottom - top


def _box(shape: Any) -> Optional[tuple[float, float, float, float]]:
    try:
        values = (shape.left, shape.top, shape.width, shape.height)
    except Exception:
        return None
    if any(v is None for v in values):
        return None
    return tuple(v / _EMU for v in values)


def _is_freeform(shape: Any) -> bool:
    try:
        return "FREEFORM" in str(shape.shape_type or "").upper()
    except Exception:
        return False


def _is_group(shape: Any) -> bool:
    try:
        return "GROUP" in str(shape.shape_type or "").upper()
    except Exception:
        return False


def _leaves(shape: Any) -> Iterable[Any]:
    for child in _safely(lambda: list(shape.shapes)) or []:
        if _is_group(child):
            yield from _leaves(child)
        else:
            yield child


def _has_text(shape: Any) -> bool:
    try:
        return bool(shape.has_text_frame and shape.text_frame.text.strip())
    except Exception:
        return False


def _name(unit: IconUnit) -> str:
    return str(getattr(unit.shapes[0], "name", "an icon"))


def _safely(call):
    try:
        return call()
    except Exception:
        return None
