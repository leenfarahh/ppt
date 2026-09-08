"""The colours inside an SVG icon, which are not the shape's colours.

An icon dropped in from PowerPoint's own library is a `p:pic`, not an
autoshape. PowerPoint calls its colour a *Graphics Fill* and gives it its own
ribbon tab, and none of that is `a:solidFill` on the shape: the colour lives
inside the SVG part the picture points at, in an `asvg:svgBlip` extension on
the `a:blip`.

So `shape.fill` answers nothing for one, `fore_color.rgb` raises, and every
colour rule looked straight past it. On a real deck that is most of the icons
on most of the slides -- a whole class of brand defect the report never
mentioned, and a recolour the tool could not perform however the finding was
worded.

Microsoft's icons carry their intent in the markup, which is what makes this
tractable rather than a guess:

    <style>.MsftOfcThm_Accent1_Stroke_v2 { stroke:#A32020; }</style>
    <path class="MsftOfcThm_Accent1_Stroke_v2" stroke="#A32020" .../>

The class name says the icon reads theme accent1; the literal hex beside it is
the colour that slot resolved to when the icon was inserted, kept so a
renderer that does not know the theme still draws something. Both have to be
read, and a recolour has to write both or the two disagree and which one wins
depends on what is doing the drawing.

The distinction the class name gives us is the useful one. A theme-bound icon
is not itself wrong -- it is reading the deck's theme, and it is the theme
that is off-brand, which `rebuild` fixes for every icon at once. An icon with
a literal colour and no theme class was recoloured by hand and is wrong on its
own terms, which is the one worth a fixer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

_A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_SVG_BLIP = "{http://schemas.microsoft.com/office/drawing/2016/SVG/main}svgBlip"
_R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"

# `fill="#1F4E79"`, `stroke='#abc'`, and the same two inside a `<style>` block
# as `fill:#1F4E79`. Three-digit hex is legal in SVG and PowerPoint's own
# exports do use it, so it is matched and expanded rather than skipped.
_COLOUR = re.compile(
    r"\b(fill|stroke)\s*[:=]\s*[\"']?\s*#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})\b"
)

# `MsftOfcThm_Accent1_Stroke_v2` -> accent1. Microsoft's own naming for "this
# element reads that theme slot", which is the only statement of intent an SVG
# carries and the difference between an icon that is wrong and one that is
# merely showing you a theme that is.
_THEME_CLASS = re.compile(
    r"MsftOfcThm_([A-Za-z]+?)(\d*)_(?:Fill|Stroke|Lumin)\w*", re.IGNORECASE
)

# The names in a class differ from the names in the theme part, the same way
# python-pptx's enum does. Folded to the theme part's spelling, because that is
# what `DeckProfile.theme_colors` is keyed by.
_SLOTS = {
    "accent": "accent{n}",
    "background": "lt{n}",
    "text": "dk{n}",
    "dark": "dk{n}",
    "light": "lt{n}",
    "hyperlink": "hlink",
    "followedhyperlink": "folHlink",
}


@dataclass(frozen=True)
class GraphicColor:
    """One colour an icon draws with, and the theme slot it reads, if any."""

    hex: str                            # six digits, upper case, no hash
    theme: Optional[str] = None         # "accent1" when the markup says so


def svg_part(shape: Any) -> Optional[Any]:
    """The SVG part behind a picture, or None for anything that is not one.

    Never raises. A shape with no blip, a blip with no SVG extension, a
    relationship that has gone missing -- each is "no SVG here", which is the
    common case and not an error.
    """
    element = getattr(shape, "_element", None)
    if element is None:
        return None
    # A group is not an icon, and its element contains its children's blips.
    # Without this a grouped icon is reported twice, once for the picture and
    # once for the group around it, which is one defect wearing two shapes.
    try:
        if bool(shape.shape_type == 6) or getattr(shape, "shapes", None) is not None:
            return None
    except Exception:
        pass
    try:
        for blip in element.iter(_A_BLIP):
            node = blip.find(f".//{_SVG_BLIP}")
            if node is None:
                continue
            rid = node.get(_R_EMBED)
            if not rid:
                continue
            return shape.part.related_part(rid)
    except Exception:
        log.debug("could not reach the SVG behind a picture", exc_info=True)
    return None


def colors_of(shape: Any) -> list[GraphicColor]:
    """Every distinct colour an icon draws with, in the order it declares them.

    Deduplicated on the colour, not on where it was written. One icon states
    the same hex in its `<style>` block and again on each of eight paths, and
    reporting nine findings for one colour would be nine ways of saying the
    same thing.
    """
    part = svg_part(shape)
    if part is None:
        return []
    try:
        markup = part.blob.decode("utf-8", "ignore")
    except Exception:
        log.debug("could not read an SVG part", exc_info=True)
        return []
    return colors_in(markup)


def colors_in(markup: str) -> list[GraphicColor]:
    """The colours in SVG markup, each tagged with its theme slot if it has one.

    The slot is read off the class rule that carries the same colour, not off
    whichever class happens to be nearest in the file: an icon with an accent
    stroke and a background fill has two rules and two colours, and pairing
    them by position would swap them on any icon that declares them in the
    other order.
    """
    by_class = _theme_by_colour(markup)
    seen: dict[str, GraphicColor] = {}
    for _prop, digits in _COLOUR.findall(markup):
        value = _expand(digits)
        if value not in seen:
            seen[value] = GraphicColor(hex=value, theme=by_class.get(value))
    return list(seen.values())


def recolor(markup: str, old: str, new: str) -> tuple[str, int]:
    """Every statement of `old` in the markup rewritten to `new`.

    Both halves, always: the `<style>` rule and the literal attribute beside
    it. Writing one and not the other leaves an icon whose colour depends on
    whether the thing drawing it applies stylesheets, which is a worse defect
    than the one being fixed because it is inconsistent between viewers.

    Returns the new markup and how many statements changed.
    """
    old = old.strip().lstrip("#").upper()
    new = new.strip().lstrip("#").upper()
    changed = 0

    def swap(match: re.Match) -> str:
        nonlocal changed
        if _expand(match.group(2)) != old:
            return match.group(0)
        changed += 1
        return match.group(0).replace(match.group(2), new)

    return _COLOUR.sub(swap, markup), changed


def _theme_by_colour(markup: str) -> dict[str, str]:
    """Colour -> theme slot, taken from the `MsftOfcThm_*` style rules.

    Read out of the `<style>` block rather than the elements, because that is
    where the class and its colour sit next to each other. An element repeats
    the colour as a literal and names the class, but a rule states both in one
    place and cannot be mispaired.
    """
    found: dict[str, str] = {}
    for block in re.findall(r"<style[^>]*>(.*?)</style>", markup, re.S | re.I):
        for rule in re.findall(r"\.([\w-]+)\s*\{([^}]*)\}", block):
            name, body = rule
            slot = _slot_of(name)
            if not slot:
                continue
            for _prop, digits in _COLOUR.findall(body):
                found.setdefault(_expand(digits), slot)
    return found


def _slot_of(class_name: str) -> Optional[str]:
    match = _THEME_CLASS.search(class_name)
    if match is None:
        return None
    family, number = match.group(1).lower(), match.group(2) or "1"
    template = _SLOTS.get(family)
    return template.format(n=number) if template else None


def _expand(digits: str) -> str:
    """`abc` -> `AABBCC`. Three-digit hex is legal SVG and PowerPoint emits it."""
    digits = digits.upper()
    return "".join(ch * 2 for ch in digits) if len(digits) == 3 else digits
