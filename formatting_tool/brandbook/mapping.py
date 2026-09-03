"""Turn a raw brand book reading into BrandGuidelines, discarding what fails.

This module is the gate between what the model said and what the tool will act
on. Four checks, in order:

1. **Evidence.** A value with no verbatim quote is dropped. The model has no
   way to state a number it cannot point at, so this is the only defence
   against a plausible invention becoming a brand rule.
2. **Unit.** A measurement with no unit, or in px (no fixed physical size
   without a stated DPI), is dropped.
3. **Type and range.** A negative margin, a max below a min, a hex that is not
   a hex.
4. **Provenance.** Whatever survives is marked AUTHORED; whatever does not is
   recorded as MISSING for the inference pass to fill.

Everything dropped is returned in `rejections` so the extraction report can
say what was thrown away and why. Silent discarding would be worse than
inventing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import (
    BrandGuidelines,
    LogoSpec,
    Margins,
    Provenance,
    RoleSpec,
    TextRole,
    TypographyRules,
)
from .schema import LENGTH_UNITS

log = logging.getLogger(__name__)

# Conversion to inches. px is deliberately absent: without a stated DPI it has
# no physical size, so a px measurement is a rejection, not a conversion.
_TO_INCHES = {
    "in": 1.0,
    "mm": 1.0 / 25.4,
    "cm": 1.0 / 2.54,
    "pt": 1.0 / 72.0,
}


@dataclass
class Rejection:
    """One value that was read but not kept."""

    path: str
    reason: str
    value: Any = None

    def __str__(self) -> str:
        return f"{self.path}: {self.reason} ({self.value!r})"


@dataclass
class MappingResult:
    guidelines: BrandGuidelines
    evidence: dict[str, str] = field(default_factory=dict)
    pages: dict[str, int] = field(default_factory=dict)
    rejections: list[Rejection] = field(default_factory=list)
    unspecified: list[str] = field(default_factory=list)

    @property
    def authored_paths(self) -> list[str]:
        return self.guidelines.paths_with(Provenance.AUTHORED)


def guidelines_from_extraction(
    raw: dict[str, Any],
    source: str,
) -> MappingResult:
    """Map a validated brand book reading onto BrandGuidelines."""
    result = MappingResult(
        guidelines=BrandGuidelines(
            name=str(raw.get("brand_name") or "unnamed").strip() or "unnamed",
            source=source,
        )
    )
    guidelines = result.guidelines

    guidelines.palette = _palette(raw.get("palette"), result)
    guidelines.allowed_fonts = _fonts(raw.get("latin_fonts"), "allowed_fonts", result)
    guidelines.arabic_fonts = _fonts(raw.get("arabic_fonts"), "arabic_fonts", result)
    guidelines.roles = _roles(raw.get("roles"), result)
    guidelines.logo = _logo(raw.get("logo"), result)
    guidelines.safe_margins, margins_found = _margins(raw.get("safe_margins"), result)
    guidelines.typography = _typography(raw.get("typography"), result)
    guidelines.notes = _notes(raw.get("notes"), result)

    # A Margins with no authored sides is the built-in default, and the safe
    # margin rule would then be testing an invented frame.
    if not margins_found:
        for side in ("top_in", "right_in", "bottom_in", "left_in"):
            _mark(guidelines, f"safe_margins.{side}", Provenance.MISSING)

    result.unspecified = [str(x) for x in raw.get("not_specified") or []]
    for rejection in result.rejections:
        log.debug("rejected %s", rejection)
    return result


# --------------------------------------------------------------------------- #
# Groups
# --------------------------------------------------------------------------- #

def _palette(entries: Any, result: MappingResult) -> dict[str, str]:
    palette: dict[str, str] = {}
    for entry in entries or []:
        label = str(entry.get("label") or "").strip()
        if not label:
            continue
        path = f"palette.{label}"
        if not _has_evidence(entry):
            result.rejections.append(
                Rejection(path, "no supporting quote in the document", entry.get("hex"))
            )
            continue
        hex_value = _hex(entry.get("hex"))
        if hex_value is None:
            # A Pantone-only or CMYK-only entry is real but not comparable, so
            # it is surfaced to the reviewer instead of being guessed at.
            result.rejections.append(
                Rejection(
                    path,
                    "no hex value; specified as "
                    f"{entry.get('spec_as_written') or 'an unconvertible colour space'}",
                    entry.get("spec_as_written"),
                )
            )
            continue
        palette[label] = hex_value
        _record(result, path, entry)

    if palette:
        _mark(result.guidelines, "palette", Provenance.AUTHORED)
        # The group quote is the first entry's, which is the one a reviewer
        # will want to open the document at.
        first = next(iter(palette))
        result.evidence.setdefault("palette", result.evidence.get(f"palette.{first}", ""))
    else:
        _mark(result.guidelines, "palette", Provenance.MISSING)
    return palette


def _fonts(entries: Any, path: str, result: MappingResult) -> list[str]:
    fonts: list[str] = []
    for entry in entries or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        if not _has_evidence(entry):
            result.rejections.append(
                Rejection(f"{path}.{name}", "no supporting quote in the document", name)
            )
            continue
        if name not in fonts:
            fonts.append(name)
            _record(result, f"{path}.{name}", entry)

    _mark(
        result.guidelines,
        path,
        Provenance.AUTHORED if fonts else Provenance.MISSING,
    )
    if fonts:
        result.evidence.setdefault(path, result.evidence.get(f"{path}.{fonts[0]}", ""))
    return fonts


def _roles(entries: Any, result: MappingResult) -> dict[str, RoleSpec]:
    roles: dict[str, RoleSpec] = {}
    seen: set[str] = set()

    for entry in entries or []:
        try:
            role = TextRole(str(entry.get("role") or "").lower())
        except ValueError:
            result.rejections.append(
                Rejection("roles", "unknown role name", entry.get("role"))
            )
            continue
        if role is TextRole.UNKNOWN or role.value in seen:
            continue
        seen.add(role.value)
        prefix = f"roles.{role.value}"

        if not _has_evidence(entry):
            result.rejections.append(
                Rejection(prefix, "no supporting quote in the document", entry)
            )
            continue

        spec = RoleSpec(role=role)
        unit = _unit(entry.get("size_unit"), default="pt")

        spec.fonts = [str(f).strip() for f in entry.get("fonts") or [] if str(f).strip()]
        if spec.fonts:
            _mark_and_record(result, f"{prefix}.fonts", entry)

        for key, attr in (("min_size", "min_size_pt"), ("max_size", "max_size_pt")):
            points = _to_points(entry.get(key), unit, f"{prefix}.{attr}", result)
            if points is not None:
                setattr(spec, attr, points)
                _mark_and_record(result, f"{prefix}.{attr}", entry)

        if (
            spec.min_size_pt is not None
            and spec.max_size_pt is not None
            and spec.min_size_pt > spec.max_size_pt
        ):
            result.rejections.append(
                Rejection(
                    prefix,
                    f"minimum {spec.min_size_pt}pt exceeds maximum "
                    f"{spec.max_size_pt}pt; both dropped",
                    (spec.min_size_pt, spec.max_size_pt),
                )
            )
            spec.min_size_pt = spec.max_size_pt = None
            _mark(result.guidelines, f"{prefix}.min_size_pt", Provenance.MISSING)
            _mark(result.guidelines, f"{prefix}.max_size_pt", Provenance.MISSING)

        colors = [_hex_or_label(c) for c in entry.get("colors") or []]
        spec.colors = [c for c in colors if c]
        if spec.colors:
            _mark_and_record(result, f"{prefix}.colors", entry)

        if entry.get("max_lines") is not None:
            spec.max_lines = int(entry["max_lines"])
            _mark_and_record(result, f"{prefix}.max_lines", entry)
        if entry.get("required") is not None:
            spec.required = bool(entry["required"])
            _mark_and_record(result, f"{prefix}.required", entry)

        roles[role.value] = spec

    for role in TextRole:
        if role is not TextRole.UNKNOWN and role.value not in seen:
            _mark(result.guidelines, f"roles.{role.value}.fonts", Provenance.MISSING)
    return roles


def _logo(entry: Any, result: MappingResult) -> LogoSpec:
    spec = LogoSpec()
    entry = entry or {}
    if not _has_evidence(entry):
        for attr in ("min_width_in", "clear_space_in", "allowed_corners"):
            _mark(result.guidelines, f"logo.{attr}", Provenance.MISSING)
        return spec

    unit = _unit(entry.get("unit"))
    for key, attr in (("min_width", "min_width_in"), ("clear_space", "clear_space_in")):
        inches = _to_inches(entry.get(key), unit, f"logo.{attr}", result)
        if inches is not None:
            setattr(spec, attr, inches)
            _mark_and_record(result, f"logo.{attr}", entry)
        else:
            _mark(result.guidelines, f"logo.{attr}", Provenance.MISSING)

    corners = [
        str(c).lower() for c in entry.get("allowed_corners") or []
        if str(c).lower() in ("tl", "tr", "bl", "br")
    ]
    if corners:
        spec.allowed_corners = corners
        _mark_and_record(result, "logo.allowed_corners", entry)
    else:
        _mark(result.guidelines, "logo.allowed_corners", Provenance.MISSING)

    for key in ("required_on_first_slide", "required_on_every_slide"):
        if entry.get(key) is not None:
            setattr(spec, key, bool(entry[key]))
            _mark_and_record(result, f"logo.{key}", entry)
        else:
            _mark(result.guidelines, f"logo.{key}", Provenance.MISSING)
    return spec


def _margins(entry: Any, result: MappingResult) -> tuple[Margins, bool]:
    margins = Margins()
    entry = entry or {}
    if not _has_evidence(entry):
        return margins, False

    unit = _unit(entry.get("unit"))
    found = False
    for key, attr in (
        ("top", "top_in"),
        ("right", "right_in"),
        ("bottom", "bottom_in"),
        ("left", "left_in"),
    ):
        inches = _to_inches(entry.get(key), unit, f"safe_margins.{attr}", result)
        if inches is None:
            _mark(result.guidelines, f"safe_margins.{attr}", Provenance.MISSING)
            continue
        setattr(margins, attr, inches)
        _mark_and_record(result, f"safe_margins.{attr}", entry)
        found = True
    return margins, found


def _typography(entry: Any, result: MappingResult) -> TypographyRules:
    rules = TypographyRules()
    entry = entry or {}
    has_evidence = _has_evidence(entry)

    for key, cast in (
        ("max_orphan_words", int),
        ("min_widow_chars", int),
        ("max_title_lines", int),
        ("allow_hyphenation", bool),
    ):
        path = f"typography.{key}"
        value = entry.get(key)
        if value is None or not has_evidence:
            _mark(result.guidelines, path, Provenance.MISSING)
            continue
        try:
            setattr(rules, key, cast(value))
        except (TypeError, ValueError):
            result.rejections.append(Rejection(path, "not a number", value))
            _mark(result.guidelines, path, Provenance.MISSING)
            continue
        _mark_and_record(result, path, entry)
    return rules


def _notes(entries: Any, result: MappingResult) -> str:
    lines: list[str] = []
    for entry in entries or []:
        rule = str(entry.get("rule") or "").strip()
        if not rule:
            continue
        if not _has_evidence(entry):
            result.rejections.append(
                Rejection("notes", "no supporting quote in the document", rule)
            )
            continue
        page = entry.get("page")
        lines.append(f"{rule} (p{page})" if page else rule)

    _mark(
        result.guidelines,
        "notes",
        Provenance.AUTHORED if lines else Provenance.MISSING,
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Value helpers
# --------------------------------------------------------------------------- #

def _has_evidence(entry: Any) -> bool:
    """The gate. No quote, no value.

    A one or two word quote is not evidence of a numeric rule either, so a
    minimum length applies.
    """
    if not isinstance(entry, dict):
        return False
    quote = entry.get("evidence")
    return isinstance(quote, str) and len(quote.strip()) >= 8


def _hex(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lstrip("#").upper()
    if len(cleaned) == 3:
        cleaned = "".join(c * 2 for c in cleaned)
    if len(cleaned) != 6 or not all(c in "0123456789ABCDEF" for c in cleaned):
        return None
    return cleaned


def _hex_or_label(value: Any) -> Optional[str]:
    """Role colours may be given as hex or as a palette label."""
    as_hex = _hex(value)
    if as_hex:
        return as_hex
    text = str(value or "").strip()
    return text or None


def _unit(value: Any, default: Optional[str] = None) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in LENGTH_UNITS:
        return text
    return default


def _to_inches(
    value: Any,
    unit: Optional[str],
    path: str,
    result: MappingResult,
) -> Optional[float]:
    if value is None:
        return None
    if unit is None:
        result.rejections.append(Rejection(path, "measurement has no unit", value))
        return None
    if unit == "px":
        result.rejections.append(
            Rejection(path, "px has no physical size without a stated DPI", value)
        )
        return None
    factor = _TO_INCHES.get(unit)
    if factor is None:
        result.rejections.append(Rejection(path, f"unknown unit {unit!r}", value))
        return None
    try:
        inches = float(value) * factor
    except (TypeError, ValueError):
        result.rejections.append(Rejection(path, "not a number", value))
        return None
    if inches <= 0:
        result.rejections.append(Rejection(path, "not a positive length", value))
        return None
    return round(inches, 4)


def _to_points(
    value: Any,
    unit: Optional[str],
    path: str,
    result: MappingResult,
) -> Optional[float]:
    inches = _to_inches(value, unit, path, result)
    return round(inches * 72.0, 2) if inches is not None else None


# --------------------------------------------------------------------------- #
# Provenance bookkeeping
# --------------------------------------------------------------------------- #

def _mark(guidelines: BrandGuidelines, path: str, provenance: Provenance) -> None:
    """Record provenance, but never downgrade an already authored value."""
    current = guidelines.provenance.get(path)
    if current == Provenance.AUTHORED.value and provenance is not Provenance.AUTHORED:
        return
    guidelines.provenance[path] = provenance.value


def _record(result: MappingResult, path: str, entry: dict[str, Any]) -> None:
    quote = str(entry.get("evidence") or "").strip()
    if quote:
        result.evidence[path] = quote
    page = entry.get("page")
    if isinstance(page, int):
        result.pages[path] = page


def _mark_and_record(result: MappingResult, path: str, entry: dict[str, Any]) -> None:
    _mark(result.guidelines, path, Provenance.AUTHORED)
    _record(result, path, entry)
