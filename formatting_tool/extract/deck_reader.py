"""Stage 1: read a .pptx into a DeckProfile.

This is the only module that knows about python-pptx. Everything downstream
works off the DeckProfile dataclasses, which keeps the rules testable without
fixture decks and keeps the AI payload independent of the parser.

python-pptx is imported lazily so that `formatting-tool --help` and the unit
tests still run in an environment where it is not installed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree

from ..models import (
    DeckProfile,
    Geometry,
    ParagraphProfile,
    RunProfile,
    ShapeProfile,
    SlideProfile,
    TextRole,
)

_DRAWINGML_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

# Placeholder types that map onto a brand text role. python-pptx reports these
# as PP_PLACEHOLDER members, rendered as "SUBTITLE (4)"; the lookup is on the
# exact name, never a substring -- "TITLE" is a substring of "SUBTITLE".
_PLACEHOLDER_ROLES: dict[str, TextRole] = {
    "TITLE": TextRole.TITLE,
    "CENTER_TITLE": TextRole.TITLE,
    "VERTICAL_TITLE": TextRole.TITLE,
    "SUBTITLE": TextRole.SUBTITLE,
    "BODY": TextRole.BODY,
    "VERTICAL_BODY": TextRole.BODY,
    "OBJECT": TextRole.BODY,
    "FOOTER": TextRole.FOOTER,
    "SLIDE_NUMBER": TextRole.FOOTER,
    "DATE": TextRole.FOOTER,
}


class DeckReadError(RuntimeError):
    """Raised when a deck cannot be opened or parsed."""


def read_deck(path: str | Path) -> DeckProfile:
    """Open a .pptx and return its full profile."""
    path = Path(path)
    if not path.exists():
        raise DeckReadError(f"deck not found: {path}")

    try:
        from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency
    except ImportError as exc:  # pragma: no cover
        raise DeckReadError(
            "python-pptx is required to read decks (pip install python-pptx)"
        ) from exc

    try:
        prs = Presentation(str(path))
    except Exception as exc:  # python-pptx raises a range of types here
        raise DeckReadError(f"could not open {path.name}: {exc}") from exc

    theme_fonts, theme_colors = _read_theme(prs)

    deck = DeckProfile(
        path=str(path),
        width_in=_inches(prs.slide_width),
        height_in=_inches(prs.slide_height),
        layout_names=[_safe(lambda: lay.name) or "" for lay in _layouts(prs)],
        theme_fonts=theme_fonts,
        theme_colors=theme_colors,
    )

    for index, slide in enumerate(prs.slides, start=1):
        deck.slides.append(_read_slide(slide, index))
    return deck


# --------------------------------------------------------------------------- #
# Slides and shapes
# --------------------------------------------------------------------------- #

def _read_slide(slide: Any, number: int) -> SlideProfile:
    profile = SlideProfile(
        number=number,
        slide_id=_safe(lambda: slide.slide_id),
        layout_name=_safe(lambda: slide.slide_layout.name),
        hidden=_safe(lambda: slide.element.get("show")) == "0",
    )
    if _safe(lambda: slide.has_notes_slide):
        profile.notes = _safe(lambda: slide.notes_slide.notes_text_frame.text) or ""
    for shape in slide.shapes:
        profile.shapes.append(_read_shape(shape))
    return profile


def _read_shape(shape: Any) -> ShapeProfile:
    shape_type = str(_safe(lambda: shape.shape_type) or "UNKNOWN")
    placeholder_type = None
    if _safe(lambda: shape.is_placeholder):
        placeholder_type = str(_safe(lambda: shape.placeholder_format.type) or "")

    profile = ShapeProfile(
        shape_id=_safe(lambda: shape.shape_id) or -1,
        name=_safe(lambda: shape.name) or "",
        shape_type=shape_type,
        geometry=Geometry(
            left_in=_inches(_safe(lambda: shape.left)),
            top_in=_inches(_safe(lambda: shape.top)),
            width_in=_inches(_safe(lambda: shape.width)),
            height_in=_inches(_safe(lambda: shape.height)),
            rotation=float(_safe(lambda: shape.rotation) or 0.0),
        ),
        placeholder_type=placeholder_type,
        role=_role_for(placeholder_type, _safe(lambda: shape.name) or ""),
        fill_hex=_fill_hex(shape),
        line_hex=_line_hex(shape),
        is_picture="PICTURE" in shape_type,
        is_group="GROUP" in shape_type,
    )

    if profile.is_picture:
        profile.image_sha1 = _image_sha1(shape)

    if _safe(lambda: shape.has_text_frame):
        frame = shape.text_frame
        profile.text = _safe(lambda: frame.text) or ""
        profile.word_wrap = _safe(lambda: frame.word_wrap)
        profile.autofit = _stringify(_safe(lambda: frame.auto_size))
        for paragraph in frame.paragraphs:
            profile.paragraphs.append(_read_paragraph(paragraph))

    if profile.is_group:
        for child in _safe(lambda: list(shape.shapes)) or []:
            profile.children.append(_read_shape(child))

    return profile


def _read_paragraph(paragraph: Any) -> ParagraphProfile:
    return ParagraphProfile(
        text=_safe(lambda: paragraph.text) or "",
        level=int(_safe(lambda: paragraph.level) or 0),
        alignment=_stringify(_safe(lambda: paragraph.alignment)),
        space_before_pt=_points(_safe(lambda: paragraph.space_before)),
        space_after_pt=_points(_safe(lambda: paragraph.space_after)),
        line_spacing=_line_spacing(_safe(lambda: paragraph.line_spacing)),
        runs=[_read_run(run) for run in _safe(lambda: paragraph.runs) or []],
    )


def _read_run(run: Any) -> RunProfile:
    font = _safe(lambda: run.font)
    color_hex, is_theme = _font_color(font)
    return RunProfile(
        text=_safe(lambda: run.text) or "",
        font_name=_safe(lambda: font.name),
        size_pt=_points(_safe(lambda: font.size)),
        bold=_safe(lambda: font.bold),
        italic=_safe(lambda: font.italic),
        underline=_safe(lambda: font.underline),
        color_hex=color_hex,
        color_is_theme=is_theme,
        language=_stringify(_safe(lambda: font.language_id)),
    )


def _role_for(placeholder_type: Optional[str], shape_name: str) -> TextRole:
    """Best-effort role assignment.

    A placeholder tells us its role outright. A loose text box does not, so we
    fall back to the shape name, which designers usually leave meaningful
    ("Title 1", "Subtitle 2") -- and which is itself a signal worth reporting
    when it disagrees with the geometry.
    """
    token = _placeholder_token(placeholder_type)
    if token and token in _PLACEHOLDER_ROLES:
        return _PLACEHOLDER_ROLES[token]

    lowered = shape_name.lower()
    if "subtitle" in lowered:
        return TextRole.SUBTITLE
    if "title" in lowered:
        return TextRole.TITLE
    if "caption" in lowered:
        return TextRole.CAPTION
    return TextRole.UNKNOWN


def _placeholder_token(placeholder_type: Optional[str]) -> Optional[str]:
    """"SUBTITLE (4)" -> "SUBTITLE". Also tolerates a bare enum name."""
    if not placeholder_type:
        return None
    return placeholder_type.split("(")[0].strip().upper()


# --------------------------------------------------------------------------- #
# Colour and image helpers
# --------------------------------------------------------------------------- #

def _font_color(font: Any) -> tuple[Optional[str], bool]:
    """Return (hex, resolves_through_theme).

    A theme-bound colour has no literal RGB on the run; it is correct by
    construction, so the colour rule treats it differently from a hardcoded
    value that merely happens to match the palette.
    """
    color = _safe(lambda: font.color)
    if color is None:
        return None, False
    rgb = _safe(lambda: color.rgb)
    if rgb is not None:
        return str(rgb).upper(), False
    theme_color = _safe(lambda: color.theme_color)
    if theme_color is not None:
        # TODO: resolve theme_color through DeckProfile.theme_colors so the
        # colour rule can compare a theme-bound run against the palette.
        return None, True
    return None, False


def _fill_hex(shape: Any) -> Optional[str]:
    fill = _safe(lambda: shape.fill)
    if fill is None:
        return None
    if str(_safe(lambda: fill.type) or "") in ("None", "BACKGROUND (5)"):
        return None
    rgb = _safe(lambda: fill.fore_color.rgb)
    return str(rgb).upper() if rgb is not None else None


def _line_hex(shape: Any) -> Optional[str]:
    rgb = _safe(lambda: shape.line.color.rgb)
    return str(rgb).upper() if rgb is not None else None


def _image_sha1(shape: Any) -> Optional[str]:
    """Identify a picture by content so the same logo matches across decks."""
    sha1 = _safe(lambda: shape.image.sha1)
    if sha1:
        return str(sha1)
    blob = _safe(lambda: shape.image.blob)
    return hashlib.sha1(blob).hexdigest() if blob else None


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #

def _read_theme(prs: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Pull the major/minor typefaces and colour scheme out of theme1.xml.

    python-pptx exposes no theme API, so this reads the related theme part
    directly. It degrades to empty dicts rather than failing the run: a missing
    theme costs precision in the colour and font rules, not correctness.
    """
    xml = _theme_xml(prs)
    if xml is None:
        return {}, {}

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return {}, {}

    fonts: dict[str, str] = {}
    for key, tag in (("major", "majorFont"), ("minor", "minorFont")):
        node = root.find(f".//{_DRAWINGML_NS}{tag}/{_DRAWINGML_NS}latin")
        if node is not None and node.get("typeface"):
            fonts[key] = node.get("typeface", "")

    colors: dict[str, str] = {}
    scheme = root.find(f".//{_DRAWINGML_NS}clrScheme")
    if scheme is not None:
        for child in scheme:
            name = child.tag.replace(_DRAWINGML_NS, "")
            srgb = child.find(f"{_DRAWINGML_NS}srgbClr")
            if srgb is not None and srgb.get("val"):
                colors[name] = srgb.get("val", "").upper()
                continue
            sys_clr = child.find(f"{_DRAWINGML_NS}sysClr")
            if sys_clr is not None and sys_clr.get("lastClr"):
                colors[name] = sys_clr.get("lastClr", "").upper()
    return fonts, colors


def _theme_xml(prs: Any) -> Optional[bytes]:
    for master in _safe(lambda: list(prs.slide_masters)) or []:
        rels = _safe(lambda: master.part.rels)
        if not rels:
            continue
        for rel in _safe(lambda: list(rels.values())) or []:
            if "theme" in str(_safe(lambda: rel.reltype) or "").lower():
                blob = _safe(lambda: rel.target_part.blob)
                if blob:
                    return blob
    return None


def _layouts(prs: Any) -> list[Any]:
    layouts: list[Any] = []
    for master in _safe(lambda: list(prs.slide_masters)) or []:
        layouts.extend(_safe(lambda: list(master.slide_layouts)) or [])
    return layouts


# --------------------------------------------------------------------------- #
# Unit helpers
# --------------------------------------------------------------------------- #

def _inches(length: Any) -> float:
    """python-pptx Length -> inches. Missing geometry reads as 0.0."""
    if length is None:
        return 0.0
    inches = _safe(lambda: length.inches)
    if inches is not None:
        return round(float(inches), 4)
    return round(float(length) / 914400.0, 4)  # EMU per inch


def _points(length: Any) -> Optional[float]:
    if length is None:
        return None
    points = _safe(lambda: length.pt)
    if points is not None:
        return round(float(points), 2)
    if isinstance(length, (int, float)):
        return round(float(length) / 12700.0, 2)  # EMU per point
    return None


def _line_spacing(value: Any) -> Optional[float]:
    """Line spacing is either a multiple (float) or an absolute Length."""
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 3)
    return _points(value)


def _stringify(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _safe(getter) -> Any:
    """Read an attribute off a python-pptx object, tolerating its exceptions.

    python-pptx raises rather than returning None in several places (a
    ColorFormat with no RGB, a shape with no fill, a picture whose image part
    is missing). A messy deck hits all of them, and none should abort a run.
    """
    try:
        return getter()
    except Exception:
        return None
