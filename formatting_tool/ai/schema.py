#The JSON contract for the ai validation layer.

from __future__ import annotations

from typing import Any, Optional

from ..models import (
    FIX_OPS,
    Category,
    FixAction,
    Issue,
    LayoutChoice,
    Severity,
    Source,
)

_CATEGORIES = [c.value for c in Category]
_SEVERITIES = [s.value for s in Severity]


AI_ISSUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slide": {
            "type": ["integer", "null"],
            "description": "1-based slide number, or null for a deck-level finding.",
        },
        "shape": {
            "type": ["string", "null"],
            "description": "Shape name exactly as given in the payload, or null.",
        },
        "category": {"type": "string", "enum": _CATEGORIES},
        "severity": {"type": "string", "enum": _SEVERITIES},
        "message": {
            "type": "string",
            "description": "One sentence, stating the defect. No preamble.",
        },
        "expected": {"type": ["string", "null"]},
        "found": {"type": ["string", "null"]},
        "suggestion": {
            "type": ["string", "null"],
            "description": "The specific fix, or null if none is obvious.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0-1.0. Below 0.5 means a judgement call, not a defect.",
        },
        "basis": {
            "type": "string",
            "enum": ["render", "geometry"],
            "description": (
                "'render' only when a rendered image of the slide was "
                "supplied and you actually read this from it. 'geometry' for "
                "anything reasoned from the numbers."
            ),
        },
        "confirms_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Refs of the rule findings this restates, explains or argues "
                "with. Fill it whenever a rule finding names the same defect "
                "on the same shape, however differently you word it; list "
                "several when your sentence covers several. Empty ONLY when "
                "no rule finding is about this defect at all. A labelled "
                "restatement merges into the rule finding and inherits its "
                "correction; an unlabelled one is reported twice."
            ),
        },
        "fix": {
            "type": ["object", "null"],
            "description": (
                "The single mechanical action that would correct this "
                "finding, or null when there is not one. Null is the common "
                "case. The target is validated against the brand system "
                "before it is applied and refused if it does not match, so "
                "name the palette entry or approved typeface, never a value "
                "you chose yourself."
            ),
            "properties": {
                "op": {
                    "type": "string",
                    "enum": sorted(FIX_OPS),
                    "description": (
                        "recolor_fill / recolor_line / recolor_text need "
                        "`hex`; set_font needs `font`; set_font_size needs "
                        "`size_pt`; move needs `left_in` and `top_in`; "
                        "resize needs `width_in` and `height_in`; "
                        "remove_note deletes the shape and needs nothing "
                        "else; disable_autofit and delete_empty_paragraphs "
                        "need nothing else."
                    ),
                },
                "shape_id": {
                    "type": ["integer", "null"],
                    "description": (
                        "The `id` of the shape from the payload. Always give "
                        "this: names repeat within a slide and ids do not, so "
                        "a fix without it may land on the wrong shape and be "
                        "refused."
                    ),
                },
                "shape": {
                    "type": ["string", "null"],
                    "description": (
                        "The exact shape name from the payload, alongside the "
                        "id."
                    ),
                },
                "left_in": {"type": ["number", "null"]},
                "top_in": {"type": ["number", "null"]},
                "width_in": {"type": ["number", "null"]},
                "height_in": {"type": ["number", "null"]},
                "hex": {
                    "type": ["string", "null"],
                    "description": "Six hex digits, no leading hash.",
                },
                "font": {"type": ["string", "null"]},
                "size_pt": {"type": ["number", "null"]},
            },
            "required": [
                "op", "shape_id", "shape", "hex", "font", "size_pt",
                "left_in", "top_in", "width_in", "height_in",
            ],
            "additionalProperties": False,
        },
    },
    "required": [
        "slide",
        "shape",
        "category",
        "severity",
        "message",
        "expected",
        "found",
        "suggestion",
        "confidence",
        "basis",
        "confirms_refs",
        "fix",
    ],
    "additionalProperties": False,
}


LAYOUT_CHOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slide": {"type": "integer", "description": "1-based slide number."},
        "layout": {
            "type": "string",
            "description": (
                "The layout's name, copied exactly from master_layouts in the "
                "reference block. Never a name that is not on that list."
            ),
        },
        "confidence": {
            "type": "number",
            "description": (
                "0.0-1.0. Low when the master offers nothing that fits, which "
                "is a fact about the master worth reporting rather than a "
                "reason to guess."
            ),
        },
        "why": {
            "type": "string",
            "description": "One clause: what the slide is, and what it needs.",
        },
    },
    "required": ["slide", "layout", "confidence", "why"],
    "additionalProperties": False,
}


AI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": AI_ISSUE_SCHEMA},
        # There is deliberately no way to dismiss a rule finding. The
        # deterministic layer proves what it reports, and a model judging a
        # whole class of finding away in one call is not a review, it is a
        # filter nobody asked for. Context that argues against a finding
        # belongs on the finding, as a low confidence and an explanation.
        "summary": {
            "type": "string",
            "description": "Two or three sentences on the state of the deck.",
        },
        # Which master layout each slide belongs on, read off the rendered
        # picture. Asked of the model because the deterministic matcher counts
        # content regions from a messy slide's loose text boxes, and a messy
        # slide has a lot of them: on a real deck that put a table of contents
        # and a two-column slide onto a four-region comparison layout, where
        # the model read both correctly. It is a coarse categorical judgement
        # about what a slide IS, which is what a render is good for -- unlike
        # "which of these eleven badges is 0.2in out", which it is not.
        "layout_choices": {
            "type": "array",
            "items": LAYOUT_CHOICE_SCHEMA,
            "description": (
                "One entry per slide in this batch that carried a rendered "
                "image. Omit a slide you were given no picture of."
            ),
        },
    },
    "required": ["issues", "summary", "layout_choices"],
    "additionalProperties": False,
}

# translation to gemini's schema subset

_TYPE_NAMES = {
    "object": "OBJECT",
    "array": "ARRAY",
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
}


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:

    out: dict[str, Any] = {}

    declared = schema.get("type")
    types = declared if isinstance(declared, list) else [declared]
    concrete = [t for t in types if t and t != "null"]
    if concrete:
        out["type"] = _TYPE_NAMES.get(concrete[0], str(concrete[0]).upper())
    if "null" in types:
        out["nullable"] = True

    for key in ("description", "enum", "required"):
        if key in schema:
            out[key] = schema[key]

    properties = schema.get("properties")
    if properties:
        out["properties"] = {k: to_gemini_schema(v) for k, v in properties.items()}
        out["propertyOrdering"] = list(properties)

    items = schema.get("items")
    if items:
        out["items"] = to_gemini_schema(items)

    return out

# mapping back onto the shared model

def layout_choices_from_response(
    payload: dict[str, Any],
    allowed: set[str],
) -> list[LayoutChoice]:
    """The model's layout picks, dropping any that names a layout that does
    not exist.

    Dropped rather than corrected: a pick the master cannot honour is not a
    near miss to be snapped to something, it is the model having invented a
    layout, and acting on it would put the slide somewhere nobody chose.
    """
    out: list[LayoutChoice] = []
    for raw in payload.get("layout_choices") or []:
        name = str(raw.get("layout") or "").strip()
        number = raw.get("slide")
        if not name or not isinstance(number, int) or name not in allowed:
            continue
        out.append(
            LayoutChoice(
                slide=number,
                layout=name,
                confidence=_confidence(raw.get("confidence")) or 0.0,
                why=str(raw.get("why") or "").strip(),
            )
        )
    return out


def issues_from_response(
    payload: dict[str, Any],
    deck_name: str,
) -> tuple[list[Issue], str]:

    issues: list[Issue] = []
    for raw in payload.get("issues", []):
        fix = _fix(raw.get("fix"))
        issues.append(
            Issue(
                category=_category(raw.get("category")),
                severity=_severity(raw.get("severity")),
                message=str(raw.get("message", "")).strip(),
                source=Source.AI,
                # The ref goes in `confirms`, not `rule_id`. A ref like "R7"
                # is an index into this run's rule findings; putting it in
                # rule_id hands a downstream consumer a rule name that does
                # not exist.
                confirms=_first_ref(raw.get("confirms_refs")),
                slide=raw.get("slide"),
                shape=raw.get("shape"),
                # Lifted off the proposal onto the finding, because that is
                # where the applier looks and where a name alone is not enough
                # to find a shape: names repeat within a slide, ids do not.
                shape_id=fix.shape_id if fix else None,
                deck=deck_name,
                expected=raw.get("expected"),
                found=raw.get("found"),
                suggestion=raw.get("suggestion"),
                confidence=_confidence(raw.get("confidence")),
                evidence=_basis(raw.get("basis")),
                fix=fix,
            )
        )

    return issues, str(payload.get("summary", "")).strip()


def _fix(raw: Any) -> Optional[FixAction]:
    """A proposed action, or None for anything that is not one.

    An unknown `op` is dropped here rather than carried and refused later. It
    would reach a designer as a fix that exists, which is a promise, and the
    honest reading of a name nothing implements is that no fix was proposed.
    """
    if not isinstance(raw, dict):
        return None
    op = str(raw.get("op") or "").strip()
    if op not in FIX_OPS:
        return None
    return FixAction(
        op=op,
        shape=(str(raw["shape"]).strip() or None) if raw.get("shape") else None,
        shape_id=_whole(raw.get("shape_id")),
        hex=(str(raw["hex"]).strip().lstrip("#").upper() or None)
        if raw.get("hex") else None,
        font=(str(raw["font"]).strip() or None) if raw.get("font") else None,
        size_pt=_number(raw.get("size_pt")),
        left_in=_number(raw.get("left_in")),
        top_in=_number(raw.get("top_in")),
        width_in=_number(raw.get("width_in")),
        height_in=_number(raw.get("height_in")),
    )


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _whole(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, int) else None


def _category(value: Any) -> Category:
    try:
        return Category(str(value))
    except ValueError:
        return Category.OTHER


def _severity(value: Any) -> Severity:
    try:
        return Severity(str(value))
    except ValueError:
        return Severity.WARNING


def _confidence(value: Any) -> Optional[float]:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _basis(value: Any) -> Optional[str]:
    """Only the two words mean anything; anything else is discarded.

    A claim that says it came from the render when no render was supplied is
    the failure mode worth guarding, and the caller checks that separately.
    """
    text = str(value or "").strip().lower()
    return text if text in ("render", "geometry") else None


def _first_ref(refs: Any) -> Optional[str]:
    if isinstance(refs, list) and refs:
        return str(refs[0])
    return None
