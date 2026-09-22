"""Select Same: is this shape the same as that one, and which ones are.

PowerPoint has no equality for shapes, so the add-in invents one: build a
deterministic string for each shape and compare the strings ordinally. Section
10 of the pipeline document. Everything here is that idea, with one change
that removes most of the original's complication.

THE C# SPENDS A THIRD OF ITS SIGNATURE CODE WORKING AROUND COM. Every read
goes through a `TryGet*` wrapper that yields the literal token `na` on
failure, because a theme-driven colour, a pattern fill or a mixed transparency
is simply not readable through the object model -- and when enough of them
come back `na`, `BuildLineColorSignature` gives up, saves a temporary copy of
the entire presentation with `SaveCopyAs`, opens it as a package, finds the
shape by id and reads the raw `spPr/ln` XML out of it. A temp file, a package
read and a cache, to answer "what colour is this line".

We are already in the file. So the fallback is the whole implementation:
`_xml_signature` walks the element the C# goes to such lengths to reach, and
it is exact for the cases COM cannot express at all -- a `schemeClr` with a
`lumMod` on it compares equal to another one and unequal to the literal colour
it happens to resolve to, which is the answer a designer means.

WHAT THE SIGNATURES ARE NOT is comparable with the C#'s. They never were
between the C# and its own Python port either; the contract is only that a
signature is compared with another signature from this module, which is the
same contract the original has.

ATTRIBUTES ARE SORTED, CHILDREN ARE NOT. Two shapes given the same fill by the
same PowerPoint dialog can carry its attributes in either written order, so
sorting them is the difference between a working match and a match that works
on the deck you tested. Child order is fixed by the schema and carries
meaning, so it is left alone.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from . import properties as props
from . import text as textframe
from .actions import MatchAction
from .elements import EMU_PER_INCH, Box

log = logging.getLogger(__name__)

# How close two measurements have to be to count as the same. A hundredth of
# an inch: under a point, which is below what anyone sees, and above the
# rounding a deck built by hand carries. The same slack the rules bucket with.
TOLERANCE_IN = 0.01

# What an unreadable value reads as, kept from the C# so that a signature
# which could not answer is visibly different from one that answered "none".
NA = "na"


def _safe(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def _local(tag: Any) -> str:
    value = str(tag)
    return value.rsplit("}", 1)[-1] if "}" in value else value


def _close(first: Optional[float], second: Optional[float],
           tolerance_in: float = TOLERANCE_IN) -> bool:
    """C#: AreClose, in inches rather than points."""
    if first is None or second is None:
        return first is second
    try:
        return abs(float(first) - float(second)) <= tolerance_in
    except (TypeError, ValueError):
        return False


def _xml_signature(element: Any) -> str:
    """One element and everything under it, as a comparable string."""
    if element is None:
        return "none"
    parts = [_local(element.tag)]
    for key in sorted(element.attrib):
        parts.append(f"{_local(key)}={element.attrib[key]}")
    for child in element:
        parts.append(_xml_signature(child))
    return "(" + " ".join(parts) + ")"


# --------------------------------------------------------------------------- #
# The parts
# --------------------------------------------------------------------------- #

def fill_signature(shape: Any) -> str:
    """The shape's fill, or every cell's fill for a table.

    A table is signed by all of its cells rather than by the first, which is
    the one place this deliberately does NOT follow the C#'s "cell (1,1)
    stands in for the table". Reading is where that shortcut earns its keep --
    a font sampled from one cell is a font -- but a table whose header is dark
    and whose body is white is not the same as a table that is white
    throughout, and signing it by its first cell says it is.
    """
    parents = props._fill_parents(shape)
    if not parents:
        return NA
    return ",".join(_xml_signature(props._fill_child(parent)) for parent in parents)


def line_signature(shape: Any) -> str:
    """The whole `a:ln`: colour, weight, dash, caps and arrowheads."""
    spPr = props._sp_pr(shape)
    if spPr is None:
        return NA
    return _xml_signature(props._first_child(spPr, ("ln",)))


def line_colour_signature(shape: Any) -> str:
    """Only the colour child of `a:ln`.

    C#: BuildLineColorSignature, minus its entire OpenXML fallback path --
    the fallback WAS reading this element, and here that is the first thing
    tried rather than the last.
    """
    line = _line_element(shape)
    if line is None:
        return NA
    return _xml_signature(props._fill_child(line))


def text_frame_signature(shape: Any) -> str:
    """Insets and both anchors: how the block sits in the box."""
    insets = textframe.insets_of(shape)
    parts = [
        "insets=" + ("na" if insets is None
                     else "/".join(f"{value:.4f}" for value in insets)),
        f"anchor={textframe.anchor_of(shape) or NA}",
        f"anchorCtr={_tri(textframe.anchor_centred(shape))}",
    ]
    return ";".join(parts)


def text_signature(shape: Any) -> str:
    """The first character's font and the first paragraph's shape.

    Sampled at character 1 for the reason `arrange.text.first_run` gives, and
    carrying the same cost: two shapes whose first characters agree and whose
    later runs do not compare as equal here.
    """
    run = textframe.first_run(shape)
    paragraph = textframe.primary_paragraph(shape)
    if run is None and paragraph is None:
        return "none"

    font = _safe(lambda: run.font) if run is not None else None
    size = _safe(lambda: font.size) if font is not None else None
    parts = [
        "name=" + str(_safe(lambda: font.name) or NA).upper(),
        f"size={'na' if size is None else format(size.pt, '.2f')}",
        f"bold={_tri(_safe(lambda: font.bold) if font else None)}",
        f"italic={_tri(_safe(lambda: font.italic) if font else None)}",
        f"underline={_safe(lambda: font.underline) if font else NA}",
        "colour=" + _run_colour_signature(run),
        f"align={textframe.alignment_of(shape) or NA}",
        f"rtl={_tri(textframe.reading_order_of(shape))}",
        "para=" + _paragraph_signature(paragraph),
        "bullet=" + _bullet_signature(paragraph),
    ]
    return ";".join(parts)


def effect_signature(shape: Any) -> str:
    """Shadow, glow, reflection, soft edge and the 3-D groups, as written.

    The C# probes nineteen COM paths one at a time and formats each answer,
    because there is no single object to ask. `a:effectLst` is that object.
    """
    spPr = props._sp_pr(shape)
    if spPr is None:
        return NA
    parts = [
        _xml_signature(props._first_child(spPr, ("effectLst", "effectDag"))),
        _xml_signature(props._first_child(spPr, ("scene3d",))),
        _xml_signature(props._first_child(spPr, ("sp3d",))),
    ]
    return ";".join(parts)


def signature(shape: Any) -> str:
    """Everything visible about a shape except where it is and how big.

    C#: BuildVisibleFormatSignature. Size and position are left out on
    purpose, there and here: "select every shape formatted like this one" is a
    question about formatting, and a set of cards laid across a slide differ
    in position by definition.
    """
    if shape is None:
        return ""
    parts = [
        f"kind={_kind(shape)}",
        f"geom={_geometry_preset(shape)}",
        f"fill={fill_signature(shape)}",
        f"line={line_signature(shape)}",
        f"frame={text_frame_signature(shape)}",
        f"text={text_signature(shape)}",
        f"effects={effect_signature(shape)}",
    ]
    return ";".join(parts)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def matches(
    shape: Any,
    action: MatchAction,
    style: props.Style,
    *,
    tolerance_in: float = TOLERANCE_IN,
) -> bool:
    """Is this shape already the same as the capture. C#: IsSameAsReference.

    Every branch is wrapped, because this is asked of every shape on a slide
    and one shape that refuses a read should cost that shape rather than the
    search.
    """
    try:
        return _matches(shape, action, style, tolerance_in)
    except Exception:
        log.debug("could not compare %r", shape, exc_info=True)
        return False


def _matches(
    shape: Any, action: MatchAction, style: props.Style, tolerance_in: float
) -> bool:
    box = Box.of(shape)

    if action is MatchAction.WIDTH:
        return _close(box.width / EMU_PER_INCH,
                      (style.width or 0) / EMU_PER_INCH, tolerance_in)

    if action is MatchAction.HEIGHT:
        return _close(box.height / EMU_PER_INCH,
                      (style.height or 0) / EMU_PER_INCH, tolerance_in)

    if action is MatchAction.SIZE:
        return (_matches(shape, MatchAction.WIDTH, style, tolerance_in)
                and _matches(shape, MatchAction.HEIGHT, style, tolerance_in))

    if action is MatchAction.ROTATION:
        return _close(_safe(lambda: float(shape.rotation or 0.0), 0.0),
                      style.rotation, tolerance_in=0.1)

    if action is MatchAction.CORNER_RADIUS:
        if not style.has_corner_radius:
            return False
        ok, radius = props.corner_radius_of(shape)
        return ok and _close(radius, style.corner_radius, tolerance_in=0.001)

    if action is MatchAction.FILL_COLOR:
        # EVERY FILL-BEARING PART, NOT THE JOINED SIGNATURE. A capture holds
        # one element and a table signs itself with one per cell, so comparing
        # the two strings would mean no table ever matched anything. Asking
        # instead whether every part carries the captured fill is the question
        # the action answers: it is what `make_same` would have produced.
        if not style.has_fill_capture:
            return False
        wanted = _xml_signature(style.fill)
        parents = props._fill_parents(shape)
        return bool(parents) and all(
            _xml_signature(props._fill_child(parent)) == wanted
            for parent in parents
        )

    if action is MatchAction.LINE_WEIGHT:
        return _same_line_visibility(shape, style) and (
            not _line_shows(style) or _line_attribute(shape, "w") == style.line_width
        )

    if action is MatchAction.LINE_STYLE:
        if not _same_line_visibility(shape, style):
            return False
        if not _line_shows(style):
            return True
        line = _line_element(shape)
        return (
            _line_attribute(shape, "cap") == style.line_cap
            and _line_attribute(shape, "cmpd") == style.line_compound
            and _xml_signature(props._first_child(line, ("prstDash", "custDash")))
            == _xml_signature(style.line_dash)
        )

    if action is MatchAction.LINE_COLOR:
        return _same_line_visibility(shape, style) and (
            not _line_shows(style)
            or line_colour_signature(shape) == _xml_signature(style.line_fill)
        )

    if action is MatchAction.ARROW_STYLE:
        line = _line_element(shape)
        if line is None:
            return False
        return (
            _xml_signature(props._first_child(line, ("headEnd",)))
            == _xml_signature(style.head_end)
            and _xml_signature(props._first_child(line, ("tailEnd",)))
            == _xml_signature(style.tail_end)
        )

    if action is MatchAction.FONT_NAME:
        run = textframe.first_run(shape)
        name = _safe(lambda: run.font.name) if run is not None else None
        if name is None or style.font_name is None:
            return name == style.font_name
        return str(name).lower() == str(style.font_name).lower()

    if action is MatchAction.FONT_SIZE:
        return _close(_font_size_pt(shape), style.font_size_pt, tolerance_in=0.1)

    if action is MatchAction.FONT_COLOR:
        return _run_colour_signature(textframe.first_run(shape)) == _xml_signature(
            style.font_fill)

    if action is MatchAction.FONT_STYLE:
        return _same_font_style(shape, style)

    if action is MatchAction.TEXT_ANCHOR:
        return (textframe.anchor_of(shape) == style.anchor
                and textframe.anchor_centred(shape) == style.anchor_centred)

    if action is MatchAction.TEXT_INSETS:
        mine = textframe.insets_of(shape)
        if mine is None or style.insets_in is None:
            return mine == style.insets_in
        return all(_close(a, b) for a, b in zip(mine, style.insets_in))

    if action is MatchAction.PARAGRAPH_ALIGNMENT:
        return (textframe.alignment_of(shape) == style.alignment
                and textframe.reading_order_of(shape) == style.reading_order)

    if action is MatchAction.FORMAT:
        return signature(shape) == style.signature

    return False


def _same_font_style(shape: Any, style: props.Style) -> bool:
    """All six attributes at the first character. C#: IsSameFontStyle."""
    run = textframe.first_run(shape)
    if run is None or not style.has_font:
        return False
    font = _safe(lambda: run.font)
    if font is None:
        return False

    name = _safe(lambda: font.name)
    if (name is None) != (style.font_name is None):
        return False
    if name is not None and str(name).lower() != str(style.font_name).lower():
        return False
    return (
        _close(_font_size_pt(shape), style.font_size_pt, tolerance_in=0.1)
        and _safe(lambda: font.bold) == style.font_bold
        and _safe(lambda: font.italic) == style.font_italic
        and _safe(lambda: font.underline) == style.font_underline
        and _run_colour_signature(run) == _xml_signature(style.font_fill)
    )


def select_same(
    shapes: Sequence[Any],
    reference: Any,
    action: MatchAction,
    *,
    tolerance_in: float = TOLERANCE_IN,
) -> list:
    """Every shape in `shapes` formatted like `reference`. C#: SelectSame.

    The reference is included whether or not it matched itself, and a search
    that found nothing else returns it alone -- the add-in does the same, so
    that pressing the button never leaves the designer with nothing selected.
    Here the equivalent is that a caller always gets a set it can act on
    rather than an empty list it has to special-case.

    SCOPE IS THE CALLER'S. The C# decides it -- the group the reference sits
    in, or every top-level shape on the slide -- because it has a selection to
    work back from and one level of group to worry about. A caller here has
    already walked the deck to whatever depth it meant, so handing that
    decision back would be taking it away from somewhere it is better made.
    """
    style = props.capture(reference, action)
    if action is MatchAction.FORMAT:
        style.signature = signature(reference)

    found: list = []
    seen: set = set()
    for shape in [reference, *(shapes or ())]:
        key = _key(shape)
        if key is not None and key in seen:
            continue
        if shape is not reference and not matches(
                shape, action, style, tolerance_in=tolerance_in):
            continue
        if key is not None:
            seen.add(key)
        found.append(shape)
    return found


def _key(shape: Any):
    """C#: GetShapeKey. The OOXML id, which is unique within a slide."""
    return _safe(lambda: int(shape.shape_id))


# --------------------------------------------------------------------------- #
# Small reads
# --------------------------------------------------------------------------- #

def _kind(shape: Any) -> str:
    return str(_safe(lambda: shape.shape_type) or NA).split(" ")[0]


def _geometry_preset(shape: Any) -> str:
    """`a:prstGeom/@prst`: which autoshape this is, as PowerPoint names it."""
    spPr = props._sp_pr(shape)
    if spPr is None:
        return NA
    preset = props._first_child(spPr, ("prstGeom",))
    if preset is None:
        return "custom" if props._first_child(spPr, ("custGeom",)) is not None else NA
    return preset.get("prst") or NA


def _line_element(shape: Any):
    """`a:ln` as it stands, WITHOUT creating one.

    `properties._line_of` adds the element when it is missing, which is right
    for a write and wrong for every read here: a comparison that created an
    empty `a:ln` on each of forty shapes would change the file it was asked
    only to look at.
    """
    spPr = props._sp_pr(shape)
    if spPr is None:
        return None
    return props._first_child(spPr, ("ln",))


def _line_attribute(shape: Any, name: str) -> Optional[str]:
    line = _line_element(shape)
    return None if line is None else line.get(name)


def _line_shows(style: props.Style) -> bool:
    """Whether the captured line draws anything.

    The C#'s invisible-line shortcut, kept: when the reference's line is
    hidden, any shape with a hidden line matches regardless of the colour or
    the weight it would have if it were shown. A hidden line has no colour to
    disagree about.
    """
    if not style.has_line_capture:
        return False
    return not (style.line_fill is not None
                and _local(style.line_fill.tag) == "noFill")


def _line_visible(shape: Any) -> Optional[bool]:
    """True, False, or None where the shape states nothing and inherits."""
    line = _line_element(shape)
    if line is None:
        return None
    fill = props._fill_child(line)
    if fill is None:
        return None
    return _local(fill.tag) != "noFill"


def _same_line_visibility(shape: Any, style: props.Style) -> bool:
    theirs = None if not style.has_line_capture else (
        None if style.line_fill is None
        else _local(style.line_fill.tag) != "noFill"
    )
    return _line_visible(shape) == theirs


def _font_size_pt(shape: Any) -> Optional[float]:
    run = textframe.first_run(shape)
    if run is None:
        return None
    size = _safe(lambda: run.font.size)
    return None if size is None else round(size.pt, 2)


def _run_colour_signature(run: Any) -> str:
    if run is None:
        return NA
    properties = _safe(lambda: run._r.find(props._qn("a:rPr")))
    if properties is None:
        return "none"
    return _xml_signature(props._fill_child(properties))


# What a paragraph says about its own shape, minus the alignment and the
# reading order, which `text_signature` already carries by name.
_PARAGRAPH_ATTRIBUTES = ("lvl", "marL", "marR", "indent", "defTabSz")
_PARAGRAPH_CHILDREN = ("lnSpc", "spcBef", "spcAft")

# Every spelling of a bullet. `buNone` is one of them and it is the one that
# matters most: a paragraph that states "no bullet" is not the same as one
# that states nothing and inherits the layout's.
_BULLET_CHILDREN = ("buClrTx", "buClr", "buSzTx", "buSzPct", "buSzPts",
                    "buFontTx", "buFont", "buNone", "buAutoNum", "buChar",
                    "buBlip")


def _p_pr(paragraph: Any):
    """`a:pPr` as it stands, without creating one. See `_line_element`."""
    if paragraph is None:
        return None
    return _safe(lambda: paragraph._p.find(props._qn("a:pPr")))


def _paragraph_signature(paragraph: Any) -> str:
    """Indents, level and the three spacings.

    C#: the `ParagraphFormat` half of BuildTextSignature -- `SpaceWithin`,
    `SpaceBefore`, `SpaceAfter`, `FirstLineIndent`, `LeftIndent`. Read off the
    element here, which picks up `defTabSz` and the level as well, and those
    are two more ways for a row of cards to disagree while looking right.
    """
    properties = _p_pr(paragraph)
    if properties is None:
        return "none"
    parts = [
        f"{name}={properties.get(name) or NA}" for name in _PARAGRAPH_ATTRIBUTES
    ]
    for name in _PARAGRAPH_CHILDREN:
        parts.append(_xml_signature(props._first_child(properties, (name,))))
    return "|".join(parts)


def _bullet_signature(paragraph: Any) -> str:
    """The bullet, as every element that describes one. C#: BuildBulletSignature.

    The C# short-circuits to just "visible" when bullets are off, because its
    other six reads answer nothing useful then. Nothing needs short-circuiting
    here: a paragraph with no bullet carries `a:buNone` or carries nothing at
    all, and either way that is the whole answer already.
    """
    properties = _p_pr(paragraph)
    if properties is None:
        return "none"
    found = [
        _xml_signature(child) for child in properties
        if _local(child.tag) in _BULLET_CHILDREN
    ]
    return "|".join(found) if found else "inherit"


def _tri(value: Optional[bool]) -> str:
    return NA if value is None else ("1" if value else "0")
