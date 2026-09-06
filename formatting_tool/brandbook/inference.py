"""Fill the gaps the brand book left, from what the master deck consistently does.

A brand book states less than a validator needs. It will give you a palette and
two typefaces and say nothing about footer size, safe margins or whether the
logo belongs top-left. The master deck answers those questions, but only as
evidence of habit, so anything filled in here is marked INFERRED and the report
says so.

The bar for inferring is consistency, not presence. One slide using 11pt
captions says nothing; every caption in the deck at 11pt is a convention worth
testing against. `DOMINANCE` is where that line sits.

Nothing here overwrites an authored value.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterator

from ..models import (
    BrandGuidelines,
    DeckProfile,
    Provenance,
    RoleSpec,
    ShapeProfile,
    TextRole,
    walk_shapes,
)

log = logging.getLogger(__name__)

# Share of observations that must agree before a habit counts as a convention.
DOMINANCE = 0.6
# Observations needed at all. Below this there is nothing to be consistent about.
MIN_OBSERVATIONS = 3


@dataclass
class InferenceResult:
    guidelines: BrandGuidelines
    inferred: dict[str, str] = field(default_factory=dict)   # path -> what and why
    still_missing: list[str] = field(default_factory=list)

    @property
    def paths(self) -> list[str]:
        return sorted(self.inferred)


def infer_gaps(guidelines: BrandGuidelines, master: DeckProfile) -> InferenceResult:
    """Fill MISSING values from the master deck, marking each as INFERRED."""
    result = InferenceResult(guidelines=guidelines)

    _infer_palette(guidelines, master, result)
    _infer_fonts(guidelines, master, result)
    _infer_roles(guidelines, master, result)
    _infer_logo(guidelines, master, result)
    _infer_margins(guidelines, master, result)

    result.still_missing = guidelines.paths_with(Provenance.MISSING)
    log.info(
        "inferred %d value(s) from %s, %d still unspecified",
        len(result.inferred),
        master.name,
        len(result.still_missing),
    )
    return result


# --------------------------------------------------------------------------- #
# Inferences
# --------------------------------------------------------------------------- #

def _infer_palette(
    guidelines: BrandGuidelines,
    master: DeckProfile,
    result: InferenceResult,
) -> None:
    """Palette from the theme colour scheme.

    The strongest inference available from a deck, and the only one not read
    off content: the colour scheme is a deliberate setting a designer made
    once, not a by-product of editing, and every theme-bound colour in the
    file resolves through it. Entries keep their theme slot names because
    nobody has stated a brand name for them.
    """
    if not _is_missing(guidelines, "palette") or guidelines.palette:
        return
    if not master.theme_colors:
        log.debug("%s declares no theme colours, not inferring a palette", master.name)
        return

    guidelines.palette = dict(master.theme_colors)
    # The basis is repeated on every palette line in the written file, so it
    # stays short: the slot names are the keys the reader is already looking at.
    _mark(
        guidelines,
        result,
        "palette",
        f"{len(master.theme_colors)} theme colour slots in {master.name}",
    )


def _infer_fonts(
    guidelines: BrandGuidelines,
    master: DeckProfile,
    result: InferenceResult,
) -> None:
    """Approved typefaces from the theme, then from what the deck actually uses.

    The theme fonts are the better source: they are a deliberate setting, not
    a by-product of editing.
    """
    if not _is_missing(guidelines, "allowed_fonts") or guidelines.allowed_fonts:
        return

    theme = [f for f in master.theme_fonts.values() if f]
    if theme:
        guidelines.allowed_fonts = _dedupe(theme)
        _mark(guidelines, result, "allowed_fonts", f"theme typefaces {theme}")
        return

    used = Counter(
        run.font_name
        for _slide, _shape, run in _runs(master)
        if run.font_name
    )
    dominant = [name for name, n in used.most_common(3) if n >= MIN_OBSERVATIONS]
    if dominant:
        guidelines.allowed_fonts = dominant
        _mark(
            guidelines,
            result,
            "allowed_fonts",
            f"most-used typefaces in the master {dominant}",
        )


def _infer_roles(
    guidelines: BrandGuidelines,
    master: DeckProfile,
    result: InferenceResult,
) -> None:
    """Per-role size range from the sizes the master uses for that role."""
    observed: dict[str, list[float]] = {}
    for _slide, shape, run in _runs(master):
        if shape.role is TextRole.UNKNOWN or not run.size_pt:
            continue
        observed.setdefault(shape.role.value, []).append(run.size_pt)

    for role_value, sizes in observed.items():
        if len(sizes) < MIN_OBSERVATIONS:
            log.debug("%s: only %d size observations, not inferring", role_value, len(sizes))
            continue

        spec = guidelines.roles.get(role_value) or RoleSpec(role=TextRole(role_value))
        guidelines.roles[role_value] = spec

        counts = Counter(sizes)
        dominant, hits = counts.most_common(1)[0]
        share = hits / len(sizes)

        if share >= DOMINANCE:
            # One size accounts for most of the role: that is the convention,
            # and a range would be looser than the evidence supports.
            low = high = dominant
            basis = f"{dominant}pt on {hits} of {len(sizes)} runs in the master"
        else:
            low, high = min(sizes), max(sizes)
            basis = (
                f"range {low}-{high}pt observed in the master "
                f"(no single dominant size)"
            )

        for attr, value in (("min_size_pt", low), ("max_size_pt", high)):
            path = f"roles.{role_value}.{attr}"
            if _is_missing(guidelines, path) and getattr(spec, attr) is None:
                setattr(spec, attr, value)
                _mark(guidelines, result, path, basis)

        path = f"roles.{role_value}.fonts"
        if _is_missing(guidelines, path) and not spec.fonts:
            fonts = Counter(
                run.font_name
                for _s, shape, run in _runs(master)
                if shape.role.value == role_value and run.font_name
            )
            if fonts:
                spec.fonts = [name for name, _ in fonts.most_common(2)]
                _mark(
                    guidelines,
                    result,
                    path,
                    f"typefaces used for {role_value} in the master {spec.fonts}",
                )


def _infer_logo(
    guidelines: BrandGuidelines,
    master: DeckProfile,
    result: InferenceResult,
) -> None:
    """Logo width and corner from where the master puts it."""
    placements = [
        (slide, shape)
        for slide, shape in _shapes(master)
        if shape.is_picture and "logo" in shape.name.lower()
    ]
    if not placements:
        return

    widths = [shape.geometry.width_in for _s, shape in placements]
    if _is_missing(guidelines, "logo.min_width_in") and guidelines.logo.min_width_in is None:
        smallest = round(min(widths), 3)
        guidelines.logo.min_width_in = smallest
        _mark(
            guidelines,
            result,
            "logo.min_width_in",
            f"smallest logo in the master is {smallest}in wide",
        )

    if _is_missing(guidelines, "logo.allowed_corners") and not guidelines.logo.allowed_corners:
        corners = Counter(
            _corner(shape, master.width_in, master.height_in)
            for _s, shape in placements
        )
        dominant, hits = corners.most_common(1)[0]
        if hits / len(placements) >= DOMINANCE:
            guidelines.logo.allowed_corners = [dominant]
            _mark(
                guidelines,
                result,
                "logo.allowed_corners",
                f"master places the logo {dominant} on {hits} of {len(placements)} slides",
            )

    if _is_missing(guidelines, "logo.required_on_every_slide"):
        visible = [s for s in master.slides if not s.hidden]
        with_logo = {slide.number for slide, _ in placements}
        if visible and len(with_logo) == len(visible):
            guidelines.logo.required_on_every_slide = True
            _mark(
                guidelines,
                result,
                "logo.required_on_every_slide",
                f"every one of the master's {len(visible)} slides carries a logo",
            )


def _infer_margins(
    guidelines: BrandGuidelines,
    master: DeckProfile,
    result: InferenceResult,
) -> None:
    """Safe margins from the tightest edge the master's text respects.

    The weakest inference here, and the one most worth a reviewer's eye: it
    reads a frame off content positions, so a single deliberately-bled text box
    in the master pulls the whole margin to zero. The dominance check guards
    against that by taking the modal edge rather than the extreme.
    """
    edges: dict[str, list[float]] = {"left": [], "top": [], "right": [], "bottom": []}
    for _slide, shape in _shapes(master):
        if not shape.paragraphs or not shape.text.strip():
            continue
        box = shape.geometry
        edges["left"].append(box.left_in)
        edges["top"].append(box.top_in)
        edges["right"].append(master.width_in - box.right_in)
        edges["bottom"].append(master.height_in - box.bottom_in)

    for side, values in edges.items():
        path = f"safe_margins.{side}_in"
        if not _is_missing(guidelines, path):
            continue
        usable = [round(v, 2) for v in values if v >= 0]
        if len(usable) < MIN_OBSERVATIONS:
            continue
        counts = Counter(usable)
        dominant, hits = counts.most_common(1)[0]
        if hits / len(usable) < DOMINANCE:
            log.debug("%s: master text edges disagree, not inferring a margin", path)
            continue
        setattr(guidelines.safe_margins, f"{side}_in", dominant)
        _mark(
            guidelines,
            result,
            path,
            f"{hits} of {len(usable)} text boxes sit {dominant}in from the {side} edge",
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _is_missing(guidelines: BrandGuidelines, path: str) -> bool:
    """Only a MISSING or DEFAULT value may be filled in.

    An authored value is never touched, and an already-inferred one is not
    inferred twice.
    """
    return guidelines.provenance_of(path) in (Provenance.MISSING, Provenance.DEFAULT)


def _mark(
    guidelines: BrandGuidelines,
    result: InferenceResult,
    path: str,
    basis: str,
) -> None:
    guidelines.provenance[path] = Provenance.INFERRED.value
    result.inferred[path] = basis


def _shapes(deck: DeckProfile) -> Iterator[tuple[object, ShapeProfile]]:
    for slide in deck.slides:
        for shape in walk_shapes(slide.shapes):
            yield slide, shape


def _runs(deck: DeckProfile) -> Iterator[tuple[object, ShapeProfile, object]]:
    for slide, shape in _shapes(deck):
        for paragraph in shape.paragraphs:
            for run in paragraph.runs:
                if run.text.strip():
                    yield slide, shape, run


def _corner(shape: ShapeProfile, deck_w: float, deck_h: float) -> str:
    box = shape.geometry
    horizontal = "l" if box.left_in + box.width_in / 2 < deck_w / 2 else "r"
    vertical = "t" if box.top_in + box.height_in / 2 < deck_h / 2 else "b"
    return f"{vertical}{horizontal}"


def _dedupe(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value and value not in seen:
            seen[value] = None
    return list(seen)
