"""Stage 2: turn the master deck plus the brand guidelines into a MasterSpec.

The MasterSpec is the single source of expected values. Rules never look at the
master DeckProfile directly, so that a run against guidelines alone (no master)
and a run against a master alone both go through the same code path.

Precedence: authored guidelines override observed master values. The master
deck is evidence of intent, not a definition of it -- masters drift too.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from ..models import (
    BrandGuidelines,
    DeckProfile,
    Geometry,
    MasterSpec,
    RoleSpec,
    TextRole,
    walk_shapes,
)


def derive_master_spec(
    master: DeckProfile,
    guidelines: BrandGuidelines,
) -> MasterSpec:
    """Merge observed master values with authored guidelines."""
    observed_sizes = _observed_sizes(master)

    spec = MasterSpec(
        source=master.name,
        width_in=master.width_in,
        height_in=master.height_in,
        guidelines=guidelines,
        palette=_palette(master, guidelines),
        allowed_fonts=_allowed_fonts(master, guidelines),
        roles=_roles(guidelines, observed_sizes),
        observed_sizes_pt={k: sorted(v) for k, v in observed_sizes.items()},
        logo_geometry=_logo_geometry(master, guidelines),
        layouts=list(master.layouts),
        theme_fonts=dict(master.theme_fonts),
        theme_colors=dict(master.theme_colors),
    )
    return spec


# --------------------------------------------------------------------------- #
# Palette and fonts
# --------------------------------------------------------------------------- #

def _palette(master: DeckProfile, guidelines: BrandGuidelines) -> dict[str, str]:
    """Authored palette, extended with the master theme scheme.

    Theme entries are prefixed so a report can say "matches theme accent2" as
    distinct from "matches brand navy".
    """
    palette = dict(guidelines.palette)
    for name, hex_value in master.theme_colors.items():
        palette.setdefault(f"theme:{name}", hex_value)
    return palette


def _allowed_fonts(master: DeckProfile, guidelines: BrandGuidelines) -> list[str]:
    fonts = list(guidelines.allowed_fonts) + list(guidelines.arabic_fonts)
    if not fonts:
        # No authored list: fall back to whatever the master theme declares, so
        # the font rule still has something to compare against.
        fonts = [f for f in master.theme_fonts.values() if f]
    return _dedupe(fonts)


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #

def _observed_sizes(master: DeckProfile) -> dict[str, list[float]]:
    """Collect the font sizes the master actually uses, per role.

    Used two ways: to fill in a size range the guidelines left blank, and to
    give the AI layer a factual "the master uses 40pt and 32pt for titles".
    """
    sizes: dict[str, set[float]] = defaultdict(set)
    for slide in master.slides:
        for shape in walk_shapes(slide.shapes):
            for paragraph in shape.paragraphs:
                for run in paragraph.runs:
                    if run.size_pt and run.text.strip():
                        sizes[shape.role.value].add(run.size_pt)
    return {role: sorted(values) for role, values in sizes.items()}


def _roles(
    guidelines: BrandGuidelines,
    observed_sizes: dict[str, list[float]],
) -> dict[str, RoleSpec]:
    """Authored role specs, with blanks filled from the master observations."""
    roles: dict[str, RoleSpec] = {}
    for role in TextRole:
        authored = guidelines.roles.get(role.value)
        observed = observed_sizes.get(role.value, [])
        if authored is None and not observed:
            continue
        spec = authored or RoleSpec(role=role)
        roles[role.value] = RoleSpec(
            role=role,
            fonts=spec.fonts or list(guidelines.allowed_fonts),
            min_size_pt=spec.min_size_pt if spec.min_size_pt is not None
            else (min(observed) if observed else None),
            max_size_pt=spec.max_size_pt if spec.max_size_pt is not None
            else (max(observed) if observed else None),
            colors=spec.colors,
            max_lines=spec.max_lines,
            required=spec.required,
        )
    return roles


# --------------------------------------------------------------------------- #
# Logo
# --------------------------------------------------------------------------- #

def _logo_geometry(
    master: DeckProfile,
    guidelines: BrandGuidelines,
) -> Optional[Geometry]:
    """Where the logo sits in the master, used as the expected placement.

    Matched by approved asset hash when the guidelines list one; otherwise by
    the first picture whose shape name mentions the logo.
    """
    approved = set(guidelines.logo.asset_sha1)
    for slide in master.slides:
        for shape in walk_shapes(slide.shapes):
            if not shape.is_picture:
                continue
            if approved and shape.image_sha1 in approved:
                return shape.geometry
            if not approved and "logo" in shape.name.lower():
                return shape.geometry
    return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _dedupe(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value and value not in seen:
            seen[value] = None
    return list(seen)
