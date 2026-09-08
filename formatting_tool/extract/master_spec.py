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
    MARGIN_CHROME,
    Margins,
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
        arabic_fonts=list(guidelines.arabic_fonts),
        theme_fonts=dict(master.theme_fonts),
        theme_colors=dict(master.theme_colors),
        safe_margins=_safe_margins(master, guidelines),
        grid_edges_in=_grid_edges(master, guidelines.tolerances.position_in),
        grid_right_edges_in=_grid_edges(
            master, guidelines.tolerances.position_in, side="right"
        ),
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
    # Merged only as the fallback below. Kept apart everywhere it matters:
    # a Latin typeface has no Arabic glyphs, so checking Arabic copy against
    # the Latin list passes text that renders as boxes.
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
# Safe margins
# --------------------------------------------------------------------------- #

# Placeholders that frame a slide rather than hold its content. A footer sits
# at the very bottom edge by design, and letting it set the bottom margin
# would put the frame outside anything a body of copy could breach.
# Shared with models.LayoutProfile.content_frame, which reads the same
# tokens for the per-layout frame.
_CHROME = MARGIN_CHROME


def _safe_margins(master: DeckProfile, guidelines: BrandGuidelines) -> Margins:
    """The usable area, authored where stated and read off the layouts where not.

    A layout's content placeholders are where the designer decided content may
    go, which makes their extent the frame -- a far better source than
    measuring where content happens to sit on sample slides, and one that works
    on a master with no slides at all.

    The most permissive layout wins each edge: any layout in the master is
    allowed, so content reaching the edge of the roomiest one is inside the
    system.
    """
    stated = guidelines.safe_margins
    observed = _layout_frame(master)
    return Margins(
        top_in=stated.top_in if stated.top_in is not None else observed.top_in,
        right_in=stated.right_in if stated.right_in is not None else observed.right_in,
        bottom_in=stated.bottom_in if stated.bottom_in is not None else observed.bottom_in,
        left_in=stated.left_in if stated.left_in is not None else observed.left_in,
    )


def _layout_frame(master: DeckProfile) -> Margins:
    """The deck-wide frame: the roomiest edge any layout offers.

    Where a layout marks its presentation space, that is the frame it
    contributes, because a designer drawing a PS rectangle has stated the
    answer this function otherwise has to infer. Where none does, its content
    placeholders stand in as before.

    This value is the fallback for slides whose own layout says nothing, and
    what the AI payload is told. The per-slide check prefers the slide's own
    layout; see `space.SafeMarginRule`.
    """
    edges: dict[str, list[float]] = {"top": [], "right": [], "bottom": [], "left": []}
    for layout in master.layouts:
        frame = layout.content_frame()
        if frame is not None:
            boxes = [frame]
        else:
            boxes = [
                shape.geometry
                for shape in layout.placeholders
                if shape.placeholder_token not in _CHROME
            ]
        if not boxes:
            continue
        edges["left"].append(min(b.left_in for b in boxes))
        edges["top"].append(min(b.top_in for b in boxes))
        edges["right"].append(master.width_in - max(b.right_in for b in boxes))
        edges["bottom"].append(master.height_in - max(b.bottom_in for b in boxes))

    def edge(side: str) -> Optional[float]:
        values = [v for v in edges[side] if v >= 0]
        return round(min(values), 2) if values else None

    return Margins(
        top_in=edge("top"),
        right_in=edge("right"),
        bottom_in=edge("bottom"),
        left_in=edge("left"),
    )


def _grid_edges(
    master: DeckProfile, tolerance: float, side: str = "left"
) -> list[float]:
    """The edges the master declares, across all its layouts.

    Only once some layout marks its presentation space. Every master has
    placeholders and could therefore supply edges this way, but reading them
    unasked would change the alignment check on every deck at once; a master
    carrying PS is one somebody has deliberately marked up, and that is the
    signal to trust its own declarations over the audited deck's habits.

    Once trusted, both kinds count -- see LayoutProfile.declared_left_edges --
    including on layouts that mark no PS themselves.
    """
    if not any(layout.presentation_space for layout in master.layouts):
        return []
    edges: list[float] = []
    for layout in master.layouts:
        edges.extend(
            layout.declared_right_edges if side == "right"
            else layout.declared_left_edges
        )
    return _collapse(edges, tolerance)


def _collapse(values: list[float], tolerance: float) -> list[float]:
    """Sorted values with near-duplicates merged to their mean.

    A layout drawn by hand states 0.917 and 0.918 for the same column, and two
    layouts state the same edge twice over. Left alone those are two grid lines
    a thousandth of an inch apart, which is how the inferred grid ends up
    blessing both 0.48 and 0.49 as intentional on a real deck.
    """
    out: list[float] = []
    cluster: list[float] = []
    for value in sorted(values):
        if cluster and value - cluster[0] > tolerance:
            out.append(round(sum(cluster) / len(cluster), 2))
            cluster = []
        cluster.append(value)
    if cluster:
        out.append(round(sum(cluster) / len(cluster), 2))
    return out


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
