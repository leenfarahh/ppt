"""Load authored brand guidelines from YAML or JSON into BrandGuidelines.

The loader is deliberately strict about unknown keys: a typo in a brand file
would otherwise silently disable a whole class of checks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    BrandGuidelines,
    LogoSpec,
    Margins,
    Provenance,
    RoleSpec,
    RuleTuning,
    TextRole,
    Tolerances,
    TypographyRules,
    guideline_paths,
)


class GuidelinesError(ValueError):
    """Raised when a guidelines file cannot be loaded or is malformed."""


def load_guidelines(path: str | Path | None) -> BrandGuidelines:
    """Load a guidelines file. With no path, return permissive defaults.

    Permissive defaults mean the deterministic layer reports only what it can
    prove without a brand reference (internal inconsistency, overflow), and the
    AI layer is told that no guidelines were supplied.
    """
    if path is None:
        return BrandGuidelines(name="none-supplied")

    path = Path(path)
    if not path.exists():
        raise GuidelinesError(f"guidelines file not found: {path}")

    raw = _read_structured(path)
    if not isinstance(raw, dict):
        raise GuidelinesError(f"{path}: expected a mapping at the top level")
    return _from_dict(raw, source=str(path))


def _read_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # noqa: PLC0415 - optional dependency, imported on use
        except ImportError as exc:  # pragma: no cover
            raise GuidelinesError(
                "PyYAML is required to read a .yaml guidelines file "
                "(pip install PyYAML), or supply the same content as .json"
            ) from exc
        return yaml.safe_load(text)
    if path.suffix.lower() == ".json":
        return json.loads(text)
    raise GuidelinesError(f"{path}: unsupported extension, use .yaml or .json")


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #

_TOP_LEVEL_KEYS = {
    "name",
    "palette",
    "allowed_fonts",
    "arabic_fonts",
    "roles",
    "logo",
    "safe_margins",
    "typography",
    "tolerances",
    "tuning",
    "notes",
    "provenance",
    "source",
}


def _from_dict(raw: dict[str, Any], source: str) -> BrandGuidelines:
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise GuidelinesError(
            f"{source}: unknown key(s) {sorted(unknown)}; "
            f"expected any of {sorted(_TOP_LEVEL_KEYS)}"
        )

    return BrandGuidelines(
        name=raw.get("name", Path(source).stem),
        palette=_normalize_palette(raw.get("palette", {}), source),
        allowed_fonts=list(raw.get("allowed_fonts", [])),
        arabic_fonts=list(raw.get("arabic_fonts", [])),
        roles=_roles(raw.get("roles", {}), source),
        logo=_dataclass_from(LogoSpec, raw.get("logo", {}), source, "logo"),
        safe_margins=_dataclass_from(
            Margins, raw.get("safe_margins", {}), source, "safe_margins"
        ),
        typography=_dataclass_from(
            TypographyRules, raw.get("typography", {}), source, "typography"
        ),
        tolerances=_dataclass_from(
            Tolerances, raw.get("tolerances", {}), source, "tolerances"
        ),
        tuning=_dataclass_from(RuleTuning, raw.get("tuning", {}), source, "tuning"),
        notes=raw.get("notes", "") or "",
        provenance=_provenance(raw.get("provenance", {}), source),
        source=raw.get("source") or None,
    )


def _provenance(raw: dict[str, str], source: str) -> dict[str, str]:
    """Validate the provenance block against the real field paths.

    A path that does not exist, or a value that is not a Provenance member,
    means the file was hand-edited into a state where the report would make
    claims about values it cannot locate. Rejected rather than ignored.
    """
    valid_paths = set(guideline_paths())
    valid_values = {p.value for p in Provenance}
    out: dict[str, str] = {}
    for path, value in (raw or {}).items():
        if path not in valid_paths:
            raise GuidelinesError(
                f"{source}: provenance names an unknown field path {path!r}"
            )
        if str(value) not in valid_values:
            raise GuidelinesError(
                f"{source}: provenance for {path!r} is {value!r}; "
                f"expected one of {sorted(valid_values)}"
            )
        out[path] = str(value)
    return out


def _normalize_palette(palette: dict[str, str], source: str) -> dict[str, str]:
    """Accept "#1F2A44", "1f2a44" or "1F2A44"; store bare uppercase hex."""
    out: dict[str, str] = {}
    for label, value in (palette or {}).items():
        hex_value = str(value).lstrip("#").strip().upper()
        if len(hex_value) not in (6, 8) or not all(
            c in "0123456789ABCDEF" for c in hex_value
        ):
            raise GuidelinesError(
                f"{source}: palette entry {label!r} is not a hex colour: {value!r}"
            )
        out[str(label)] = hex_value[:6]
    return out


def _roles(raw: dict[str, Any], source: str) -> dict[str, RoleSpec]:
    out: dict[str, RoleSpec] = {}
    for key, body in (raw or {}).items():
        try:
            role = TextRole(str(key).lower())
        except ValueError as exc:
            valid = [r.value for r in TextRole]
            raise GuidelinesError(
                f"{source}: unknown role {key!r}; expected one of {valid}"
            ) from exc
        body = dict(body or {})
        body.setdefault("role", role)
        body["role"] = role
        if "colors" in body:
            body["colors"] = [
                str(c).lstrip("#").upper() for c in body["colors"]
            ]
        out[role.value] = _dataclass_from(RoleSpec, body, source, f"roles.{key}")
    return out


def _dataclass_from(cls, raw: dict[str, Any], source: str, where: str):
    """Build a dataclass from a mapping, rejecting unknown keys."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(cls)}
    raw = dict(raw or {})
    unknown = set(raw) - fields
    if unknown:
        raise GuidelinesError(
            f"{source}: unknown key(s) {sorted(unknown)} under {where}; "
            f"expected any of {sorted(fields)}"
        )
    return cls(**raw)
