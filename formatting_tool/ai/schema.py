#The JSON contract for the ai validation layer.

from __future__ import annotations

from typing import Any, Optional

from ..models import Category, Issue, Severity, Source

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
        "confirms_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Refs of the rule findings this restates or explains. "
                "Empty when the finding is new."
            ),
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
        "confirms_refs",
    ],
    "additionalProperties": False,
}


AI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": AI_ISSUE_SCHEMA},
        "dismissed_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["ref", "reason"],
                "additionalProperties": False,
            },
            "description": (
                "Rule findings that are false positives in context, with the "
                "reason. Used to suppress them from the final report."
            ),
        },
        "summary": {
            "type": "string",
            "description": "Two or three sentences on the state of the deck.",
        },
    },
    "required": ["issues", "dismissed_refs", "summary"],
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

def issues_from_response(
    payload: dict[str, Any],
    deck_name: str,
) -> tuple[list[Issue], dict[str, str], str]:

    issues: list[Issue] = []
    for raw in payload.get("issues", []):
        issues.append(
            Issue(
                category=_category(raw.get("category")),
                severity=_severity(raw.get("severity")),
                message=str(raw.get("message", "")).strip(),
                source=Source.AI,
                rule_id=_first_ref(raw.get("confirms_refs")),
                slide=raw.get("slide"),
                shape=raw.get("shape"),
                deck=deck_name,
                expected=raw.get("expected"),
                found=raw.get("found"),
                suggestion=raw.get("suggestion"),
                confidence=_confidence(raw.get("confidence")),
            )
        )

    dismissals = {
        str(entry.get("ref")): str(entry.get("reason", ""))
        for entry in payload.get("dismissed_refs", [])
        if entry.get("ref")
    }
    return issues, dismissals, str(payload.get("summary", "")).strip()


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


def _first_ref(refs: Any) -> Optional[str]:
    if isinstance(refs, list) and refs:
        return str(refs[0])
    return None
