"""What colour the master gives the text in a placeholder.

The value a designer means when they say "the title colour", and it is nowhere
a run can be read from. A layout placeholder holds no text, so it carries no
`a:rPr`; what it carries is a DEFAULT, `a:lstStyle/a:lvl1pPr/a:defRPr`, and
where it does not carry one it inherits from the master's placeholder and then
from the master's `p:txStyles`. Reading runs finds `None` at every level, which
is why nothing here knew this value existed.

IT IS PER LAYOUT, NOT PER DECK, and that is the whole reason it is worth
having. Off one real master:

    Title Slide          title  bg1        white, over a dark cover
    Section Divider      title  accent1    the brand green
    Title with Content   title  (none)     so tx2, from the master's titleStyle

One "title colour" for the deck would be wrong on two of those three. So the
question this answers is always about a particular layout: what does THIS
layout say its title is.

The chain, most specific first:

1. the layout placeholder's own `a:lstStyle`
2. the master placeholder of the same kind -- its `a:lstStyle`
3. the master's `p:txStyles`: `p:titleStyle` for a title, `p:bodyStyle` for a
   body, `p:otherStyle` for the rest
4. the theme, where any of those name a scheme colour rather than an RGB one

Scheme colours are resolved through the master's own theme, because that is
what the renderer will do. Colour modifiers -- `a:lumMod`, `a:alpha` and the
rest -- are deliberately ignored: they shade a colour rather than choose one,
and the answer wanted here is which of the brand's colours this is, not the
exact pixel PowerPoint will draw.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

# `a:schemeClr` names the slots as a document refers to them; `a:clrScheme` in
# the theme names them as they are defined. The same twelve slots, two
# spellings, and `DeckProfile.theme_colors` is keyed by the second.
_SCHEME: dict[str, str] = {
    "tx1": "dk1", "tx2": "dk2", "bg1": "lt1", "bg2": "lt2",
    "dk1": "dk1", "dk2": "dk2", "lt1": "lt1", "lt2": "lt2",
    "accent1": "accent1", "accent2": "accent2", "accent3": "accent3",
    "accent4": "accent4", "accent5": "accent5", "accent6": "accent6",
    "hlink": "hlink", "folHlink": "folHlink",
}

# Which of the master's three text styles a placeholder falls under. The
# tokens are python-pptx's, as `ShapeProfile.placeholder_type` records them.
_TITLE_TOKENS = frozenset({"TITLE", "CENTER_TITLE", "VERTICAL_TITLE"})
_BODY_TOKENS = frozenset({"BODY", "VERTICAL_BODY", "SUBTITLE", "OBJECT"})


def placeholder_color(
    placeholder: Any, master: Any, theme_colors: dict[str, str], token: str
) -> tuple[Optional[str], Optional[str]]:
    """The colour this layout gives this placeholder: (hex, theme slot).

    Either half can be None. A placeholder whose colour is stated as an RGB
    value has a hex and no slot; one stated as a scheme colour has both, the
    hex resolved through the master's theme; one that states nothing anywhere
    in the chain has neither, and the caller should say nothing rather than
    invent a default -- black is not a brand decision.
    """
    try:
        fill = _own(placeholder)
        if fill is None:
            fill = _from_master(master, token)
        if fill is None:
            return None, None
        return _resolve(fill, theme_colors)
    except Exception:       # a hand-built template; never worth a failed read
        log.debug("could not read a placeholder's text colour", exc_info=True)
        return None, None


def _own(placeholder: Any) -> Optional[Any]:
    """The colour element in the placeholder's own default run properties."""
    element = getattr(placeholder, "_element", None)
    if element is None:
        return None
    return _first_fill(element.find(f".//{_A}lstStyle"))


def _from_master(master: Any, token: str) -> Optional[Any]:
    """The master's answer: its own placeholder first, then its text styles."""
    element = getattr(master, "_element", None)
    if element is None:
        return None

    for shape in _master_placeholders(master):
        if _token_of(shape) == token:
            fill = _own(shape)
            if fill is not None:
                return fill

    styles = element.find(f"{_P}txStyles")
    if styles is None:
        return None
    name = (
        "titleStyle" if token in _TITLE_TOKENS
        else "bodyStyle" if token in _BODY_TOKENS
        else "otherStyle"
    )
    return _first_fill(styles.find(f"{_P}{name}"))


def _master_placeholders(master: Any) -> list:
    try:
        return list(master.placeholders)
    except Exception:
        return []


def _token_of(shape: Any) -> str:
    try:
        return str(shape.placeholder_format.type).split(" ")[0].upper()
    except Exception:
        return ""


def _first_fill(container: Any) -> Optional[Any]:
    """The colour on the first level's default run properties.

    Level one only. A list style states nine levels and the deeper ones are
    for nested bullets; a placeholder's colour is what its first line is set
    in, and taking whichever level happened to state something would answer
    with the colour of a fourth-level bullet nobody uses.
    """
    if container is None:
        return None
    level = container.find(f"{_A}lvl1pPr")
    if level is None:
        return None
    properties = level.find(f"{_A}defRPr")
    if properties is None:
        return None
    solid = properties.find(f"{_A}solidFill")
    if solid is None:
        return None
    for child in solid:
        if child.tag in (f"{_A}srgbClr", f"{_A}schemeClr"):
            return child
    return None


def _resolve(
    fill: Any, theme_colors: dict[str, str]
) -> tuple[Optional[str], Optional[str]]:
    value = (fill.get("val") or "").strip()
    if not value:
        return None, None
    if fill.tag == f"{_A}srgbClr":
        return value.upper(), None
    slot = _SCHEME.get(value)
    if slot is None:
        return None, None
    hex_value = theme_colors.get(slot)
    return (hex_value.upper() if hex_value else None), slot
