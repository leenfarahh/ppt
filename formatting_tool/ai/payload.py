#Build the request payload for the AI validation layer.


from __future__ import annotations

import json
from typing import Any, Iterator, Optional, Sequence

from ..models import (
    MARGIN_CHROME,
    DeckProfile,
    Issue,
    MasterSpec,
    ShapeProfile,
    SlideProfile,
    enum_safe,
)

# One slide per call. Ten slides in one request is cheaper per slide and the
# reference block caches across calls either way, but the model's attention has
# to cover every shape on every slide in the batch, and what it misses first is
# the small stuff: the badge 0.2in out of line, the caption a point adrift. A
# single slide per call is the same total payload split so that each part gets
# read properly.
#
# The cost is that no call can see two slides at once, so a cross-slide pattern
# is invisible to this layer. It always partly was, which is why the reviewer
# instructions have never let the model report a deck-level pattern from one
# batch; the deterministic rules read the whole deck and carry those findings.
DEFAULT_BATCH_SIZE = 1


# cached half: guidelines and expected values

def build_reference_block(spec: MasterSpec) -> str:

    guidelines = spec.guidelines
    reference = {
        "brand": guidelines.name,
        "slide_size_in": [spec.width_in, spec.height_in],
        "palette": spec.palette,
        "allowed_fonts": spec.allowed_fonts,
        "arabic_fonts": guidelines.arabic_fonts,
        "roles": enum_safe({k: v for k, v in spec.roles.items()}),
        "logo": enum_safe(guidelines.logo),
        # The spec's frame, not the brand file's subset of it. These are the
        # numbers the deterministic layer measures against -- authored where
        # stated, read off the master's presentation space or its placeholders
        # where not -- and on a master-only run, which is most runs, the brand
        # file's own margins are empty. The model was being told there was no
        # frame while the rules were checking against one.
        "safe_margins_in": enum_safe(spec.safe_margins),
        "typography": enum_safe(guidelines.typography),
        "tolerances": enum_safe(guidelines.tolerances),
        "master_observed_sizes_pt": spec.observed_sizes_pt,
        # Names AND what each offers. A name is not a description, and the
        # model is being asked to choose between them: "title_comparison" and
        # "title_two_columns" are indistinguishable from their names alone.
        "master_layouts": _layout_inventory(spec),
        "notes": guidelines.notes,
    }
    return json.dumps(enum_safe(reference), indent=2, sort_keys=True)


def _layout_inventory(spec: MasterSpec) -> list[dict[str, Any]]:
    """Each layout, and the regions it offers content.

    Chrome is left out: every layout carries a footer and a page number, so
    listing them tells the model nothing and crowds what does.
    """
    out: list[dict[str, Any]] = []
    for layout in spec.layouts:
        regions: dict[str, int] = {}
        for placeholder in layout.placeholders:
            token = (placeholder.placeholder_token or "BODY").upper()
            if token in MARGIN_CHROME:
                continue
            kind = (
                "title" if "TITLE" in token
                else "subtitle" if token == "SUBTITLE"
                else "content"
            )
            regions[kind] = regions.get(kind, 0) + 1
        out.append({"name": layout.name, "offers": regions})
    return out


# volatile half: rule findings and slides

def ref_for(index: int) -> str:
    return f"R{index + 1}"


def build_rule_findings(issues: Sequence[Issue]) -> list[dict[str, Any]]:
    return [
        {
            "ref": ref_for(index),
            "rule_id": issue.rule_id,
            "slide": issue.slide,
            "shape": issue.shape,
            "category": issue.category.value,
            "severity": issue.severity.value,
            "message": issue.message,
            "expected": issue.expected,
            "found": issue.found,
        }
        for index, issue in enumerate(issues)
    ]


def build_slide_digests(slides: Sequence[SlideProfile]) -> list[dict[str, Any]]:

    return [
        {
            "n": slide.number,
            "layout": slide.layout_name,
            "hidden": slide.hidden or None,
            "shapes": [_shape_digest(shape) for shape in slide.shapes],
        }
        for slide in slides
    ]


def _shape_digest(shape: ShapeProfile) -> dict[str, Any]:
    box = shape.geometry
    digest: dict[str, Any] = {
        "name": shape.name,
        "type": shape.shape_type,
        "role": shape.role.value,
        "ph": shape.placeholder_type,
        "box_in": [box.left_in, box.top_in, box.width_in, box.height_in],
    }
    if shape.text.strip():
        digest["text"] = shape.text
    if shape.fill_hex:
        digest["fill"] = shape.fill_hex
    if shape.line_hex:
        digest["line"] = shape.line_hex
    if shape.image_sha1:
        digest["image_sha1"] = shape.image_sha1
    if shape.autofit:
        digest["autofit"] = shape.autofit

    runs = _run_digests(shape)
    if runs:
        digest["runs"] = runs
    if shape.children:
        digest["children"] = [_shape_digest(child) for child in shape.children]
    return {k: v for k, v in digest.items() if v is not None}


def _run_digests(shape: ShapeProfile) -> list[dict[str, Any]]:

    seen: dict[tuple, dict[str, Any]] = {}
    for paragraph in shape.paragraphs:
        for run in paragraph.runs:
            if not run.text.strip():
                continue
            key = (
                run.font_name,
                run.size_pt,
                run.bold,
                run.italic,
                run.color_hex,
                run.color_is_theme,
                paragraph.level,
            )
            if key in seen:
                seen[key]["runs"] += 1
                continue
            entry = {
                "font": run.font_name,
                "pt": run.size_pt,
                "bold": run.bold or None,
                "italic": run.italic or None,
                "color": run.color_hex,
                "theme_color": run.color_is_theme or None,
                "level": paragraph.level or None,
                "align": paragraph.alignment,
                "runs": 1,
                "sample": run.text[:60],
            }
            seen[key] = {k: v for k, v in entry.items() if v is not None}
    return list(seen.values())


# batching

def build_batches(
    deck: DeckProfile,
    rule_issues: Sequence[Issue],
    batch_size: int = DEFAULT_BATCH_SIZE,
    spec: Optional[MasterSpec] = None,
) -> Iterator[dict[str, Any]]:

    findings = build_rule_findings(rule_issues)
    deck_level = [f for f in findings if f["slide"] is None]

    slides = deck.slides
    total = (len(slides) + batch_size - 1) // batch_size or 1
    theme = _theme_block(deck, spec)

    for index in range(0, len(slides), batch_size):
        chunk = slides[index: index + batch_size]
        numbers = {slide.number for slide in chunk}
        yield {
            "deck": deck.name,
            "batch": {
                "index": index // batch_size + 1,
                "of": total,
                "slides": sorted(numbers),
            },
            "slide_size_in": [deck.width_in, deck.height_in],
            **theme,
            "rule_findings": deck_level
            + [f for f in findings if f["slide"] in numbers],
            "slides": build_slide_digests(chunk),
        }


def _theme_block(deck: DeckProfile, spec: Optional[MasterSpec]) -> dict[str, Any]:
    """The theme the model should judge against, plus the deck's if it differs.

    Sending the deck's own theme under a neutral name told the model that the
    foreign brand it arrived with was the standard. The master's theme is the
    reference; the deck's appears only when the two disagree, named for what
    it is, because a deck carrying somebody else's theme is itself a finding
    worth the model's attention.
    """
    if spec is None:
        return {"theme_fonts": deck.theme_fonts, "theme_colors": deck.theme_colors}

    block: dict[str, Any] = {
        "master_theme_fonts": spec.theme_fonts,
        "master_theme_colors": spec.theme_colors,
    }
    if deck.theme_fonts != spec.theme_fonts or deck.theme_colors != spec.theme_colors:
        block["deck_own_theme"] = {
            "note": (
                "This deck carries its own theme, from the file it was built "
                "from. It is not the brand standard; judge against "
                "master_theme_* above."
            ),
            "fonts": deck.theme_fonts,
            "colors": deck.theme_colors,
        }
    return block


def payload_to_text(payload: dict[str, Any]) -> str:
    return json.dumps(enum_safe(payload), indent=1, sort_keys=True)


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def find_issue_by_ref(
    issues: Sequence[Issue],
    ref: str,
) -> Optional[Issue]:
    for index, issue in enumerate(issues):
        if ref_for(index) == ref:
            return issue
    return None
