"""The JSON contract for extracting brand rules out of a reference PDF.

Two things shape this schema.

**Every value carries its evidence.** A brand book states some rules and
implies others, and a model asked to fill in a form will fill it in. Requiring
a verbatim quote and a page number for each value gives the reviewer something
to check, and gives `mapping.py` a hard rule to enforce: a value with no
evidence is discarded rather than trusted. That is the difference between
extraction and invention.

**Every measurement carries its unit.** Brand books specify margins in mm as
often as inches, and type in pt or px. The unit travels with the number and
`mapping.py` converts; a number without a unit is not usable.

Nothing here is a `BrandGuidelines`. This is the raw reading of the document.
`mapping.py` turns it into the model, and drops whatever fails the checks.
"""

from __future__ import annotations

from typing import Any

from ..models import TextRole

# Units a brand book plausibly uses. px is accepted so that it can be
# reported and rejected explicitly rather than silently misread as pt: it has
# no fixed physical size without a stated DPI.
LENGTH_UNITS = ["in", "mm", "cm", "pt", "px"]

_ROLES = [r.value for r in TextRole if r is not TextRole.UNKNOWN]

_EVIDENCE_PROPERTIES: dict[str, Any] = {
    "evidence": {
        "type": ["string", "null"],
        "description": (
            "Short verbatim quote from the document that states this value. "
            "Null if the document does not state it. Do not paraphrase and do "
            "not quote a passage that only implies the value."
        ),
    },
    "page": {
        "type": ["integer", "null"],
        "description": "1-based page number the evidence appears on.",
    },
}
_EVIDENCE_REQUIRED = ["evidence", "page"]


def _obj(properties: dict[str, Any], extra_required: list[str] | None = None) -> dict:
    """An object that always carries evidence alongside its own fields."""
    merged = {**properties, **_EVIDENCE_PROPERTIES}
    return {
        "type": "object",
        "properties": merged,
        "required": list(properties) + _EVIDENCE_REQUIRED
        if extra_required is None
        else extra_required + _EVIDENCE_REQUIRED,
        "additionalProperties": False,
    }


_COLOR = _obj(
    {
        "label": {
            "type": "string",
            "description": (
                "The name the document gives the colour, e.g. 'Deep Navy'. "
                "Used verbatim in reports, so keep the document's wording."
            ),
        },
        "hex": {
            "type": ["string", "null"],
            "description": (
                "Six-digit hex, with or without a leading hash. Null when the "
                "document gives only Pantone, CMYK or a spot name."
            ),
        },
        "spec_as_written": {
            "type": ["string", "null"],
            "description": (
                "The colour exactly as the document specifies it, e.g. "
                "'PANTONE 2965 C' or 'C100 M85 Y40 K35'. Kept for the reviewer "
                "when no hex is given."
            ),
        },
    }
)

_FONT = _obj(
    {
        "name": {
            "type": "string",
            "description": "Typeface family name as the document writes it.",
        },
        "usage": {
            "type": ["string", "null"],
            "description": (
                "What the document says it is for, e.g. 'headlines only'. "
                "Null if unstated."
            ),
        },
    }
)

_ROLE = _obj(
    {
        "role": {"type": "string", "enum": _ROLES},
        "fonts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Typefaces permitted for this role. Empty if unstated.",
        },
        "min_size": {"type": ["number", "null"]},
        "max_size": {"type": ["number", "null"]},
        "size_unit": {
            "type": ["string", "null"],
            "description": (
                "Unit for min_size and max_size, normally 'pt'. One of: "
                + ", ".join(LENGTH_UNITS)
            ),
        },
        "colors": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Permitted colours for this role, as hex or as the palette "
                "label the document uses. Empty if unstated."
            ),
        },
        "max_lines": {"type": ["integer", "null"]},
        "required": {
            "type": ["boolean", "null"],
            "description": "True only if the document says this role must be present.",
        },
    }
)

_LOGO = _obj(
    {
        "min_width": {"type": ["number", "null"]},
        "clear_space": {
            "type": ["number", "null"],
            "description": (
                "Minimum clear space around the logo. Brand books often give "
                "this as a multiple of a logo feature rather than an absolute "
                "length; in that case return null and put the rule in notes."
            ),
        },
        "unit": {
            "type": ["string", "null"],
            "description": (
                "Unit for min_width and clear_space. One of: "
                + ", ".join(LENGTH_UNITS)
            ),
        },
        "allowed_corners": {
            "type": "array",
            "items": {"type": "string", "enum": ["tl", "tr", "bl", "br"]},
            "description": (
                "Corners the logo may sit in: tl, tr, bl, br. Empty if the "
                "document does not constrain placement."
            ),
        },
        "required_on_first_slide": {"type": ["boolean", "null"]},
        "required_on_every_slide": {"type": ["boolean", "null"]},
    }
)

_MARGINS = _obj(
    {
        "top": {"type": ["number", "null"]},
        "right": {"type": ["number", "null"]},
        "bottom": {"type": ["number", "null"]},
        "left": {"type": ["number", "null"]},
        "unit": {
            "type": ["string", "null"],
            "description": "One of: " + ", ".join(LENGTH_UNITS),
        },
    }
)

_TYPOGRAPHY = _obj(
    {
        "max_orphan_words": {
            "type": ["integer", "null"],
            "description": "Words that may be left alone on a final line.",
        },
        "min_widow_chars": {"type": ["integer", "null"]},
        "max_title_lines": {"type": ["integer", "null"]},
        "allow_hyphenation": {"type": ["boolean", "null"]},
    }
)

_NOTE = _obj(
    {
        "rule": {
            "type": "string",
            "description": (
                "A formatting rule the document states that cannot be reduced "
                "to a number or a list, in one sentence. This is where "
                "conditional and contextual rules belong."
            ),
        },
    }
)


BRANDBOOK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "brand_name": {"type": ["string", "null"]},
        "palette": {"type": "array", "items": _COLOR},
        "latin_fonts": {"type": "array", "items": _FONT},
        "arabic_fonts": {
            "type": "array",
            "items": _FONT,
            "description": (
                "Arabic typefaces. Latin fonts do not cover Arabic, so these "
                "are tracked separately and never merged with latin_fonts."
            ),
        },
        "roles": {"type": "array", "items": _ROLE},
        "logo": _LOGO,
        "safe_margins": _MARGINS,
        "typography": _TYPOGRAPHY,
        "notes": {"type": "array", "items": _NOTE},
        "not_specified": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Field names this document does not specify. A cross-check on "
                "the nulls above, not a substitute for them."
            ),
        },
    },
    "required": [
        "brand_name",
        "palette",
        "latin_fonts",
        "arabic_fonts",
        "roles",
        "logo",
        "safe_margins",
        "typography",
        "notes",
        "not_specified",
    ],
    "additionalProperties": False,
}
