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
# A ceiling on slides per call, not an exact count. Past this the model's
# attention is spread too thin whatever the payload measures, and a deck of
# very small slides would otherwise put thirty in one call.
DEFAULT_BATCH_SIZE = 8

# How much payload one call may carry, in rough tokens. Sized off a real
# deck: its slides ran 1,590 to 10,610 tokens, so this holds the largest of
# them with room beside it, or four or five ordinary ones. Big enough that the
# long tail of simple slides stops costing a call each; small enough that a
# dense slide still travels nearly alone.
DEFAULT_BATCH_TOKENS = 24000


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
    """The rule findings, one entry per defect rather than per occurrence.

    Sent so the model knows what has already been proved and can label its own
    restatements. One dense slide was sending 88 of them, 28 of which were
    `color.text.off_palette` on the same shape -- about 7,600 tokens for one
    slide, and the model was never going to write 28 findings about one
    shape's colour. Repeats collapse to one entry carrying the count.

    The surviving entry keeps the FIRST ref of its group, which is the one a
    restatement will name and the one the merge absorbs into. The others stay
    in the report untouched, fixable as they always were; what is dropped is
    the model's chance to label them individually, and it was never going to.

    `category` goes too: it is derivable from `rule_id`, which is right there.
    """
    grouped: dict[tuple, dict[str, Any]] = {}
    for index, issue in enumerate(issues):
        key = (issue.slide, issue.rule_id, issue.shape)
        seen = grouped.get(key)
        if seen is not None:
            seen["n"] = seen.get("n", 1) + 1
            continue
        grouped[key] = {
            "ref": ref_for(index),
            "rule_id": issue.rule_id,
            "slide": issue.slide,
            "shape": issue.shape,
            "severity": issue.severity.value,
            "message": issue.message,
            "expected": issue.expected,
            "found": issue.found,
        }
    # `slide` is kept even when it is null: that is how a deck-level finding
    # says so, and the batcher reads it to decide which findings travel with
    # which slides.
    return [
        {k: v for k, v in entry.items() if v is not None or k == "slide"}
        for entry in grouped.values()
    ]


def build_slide_digests(slides: Sequence[SlideProfile]) -> list[dict[str, Any]]:
    return [_slide_digest(slide) for slide in slides]


def _slide_digest(slide: SlideProfile) -> dict[str, Any]:
    """One slide, as the model sees it.

    Split out so the packer can measure a slide with the same object that is
    later sent. Measuring one thing and sending another is how a budget stops
    meaning anything.
    """
    return {
        "n": slide.number,
        "layout": slide.layout_name,
        "hidden": slide.hidden or None,
        "shapes": [
            digest for digest in
            (_shape_digest(shape) for shape in slide.shapes)
            if digest is not None
        ],
    }


def _shape_digest(shape: ShapeProfile) -> Optional[dict[str, Any]]:
    if not _worth_sending(shape):
        return None
    box = shape.geometry
    digest: dict[str, Any] = {
        # The OOXML id, unique within its slide, sent so a finding can name a
        # shape rather than describe one. Names are not unique and in a real
        # deck are wildly not unique -- sixteen shapes called "Pentagon 7" on
        # one slide happens -- so a fix matched on a name lands on whichever
        # came first. This is what makes an AI-proposed correction safe to
        # apply at all.
        "id": shape.shape_id,
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
        children = [
            child for child in (_shape_digest(c) for c in shape.children)
            if child is not None
        ]
        if children:
            digest["children"] = children
    return {k: v for k, v in digest.items() if v is not None}


def _worth_sending(shape: ShapeProfile) -> bool:
    """Whether the model has anything to judge about this shape.

    A shape with no text, no fill, no outline, no image and no placeholder
    role is a decorative freeform or a stub -- on one real slide, 100 of 158
    shapes, including a 0.0017in embedded-object placeholder. The model cannot
    say anything about them that the geometry rules do not already prove, and
    they were two thirds of the biggest block in the payload.

    A group survives if any of its parts does, because the group is how the
    parts are addressed.
    """
    # Tested against what the digest actually carries, not against what the
    # profile knows. A first pass kept anything with a theme-bound outline and
    # saved almost nothing, because a theme-bound outline is not sent: the
    # shape still arrived as a name, a type and a box, which is exactly the
    # noise this is meant to remove.
    if shape.text.strip():
        return True
    if shape.fill_hex or shape.line_hex or shape.image_sha1:
        return True
    if shape.placeholder_type:
        return True
    return any(_worth_sending(child) for child in shape.children)


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
    budget_tokens: int = DEFAULT_BATCH_TOKENS,
) -> Iterator[dict[str, Any]]:
    """Slides packed into calls by how big they are, not by how many there are.

    One slide per call was the rule, for a good reason: the model's attention
    has to cover every shape on every slide in the batch, and what it misses
    first is the small stuff. But it made a 105-slide deck 105 calls, and
    measured across a real deck the slides are nothing like each other -- 1,590
    tokens for a cover, 10,610 for a diagram. Sending the cover alone spends a
    whole call's latency on almost nothing.

    So a call is filled to a token budget instead. A dense slide still goes
    nearly alone, which is where the attention argument actually bites; the
    long tail of simple slides collapses together, which is where it does not.
    On the deck measured, 105 calls became about 15.

    `batch_size` is now a ceiling on slides per call rather than an exact
    count, so `--batch-size 1` still means one slide per call and nothing
    about the old behaviour is out of reach.
    """
    findings = build_rule_findings(rule_issues)
    deck_level = [f for f in findings if f.get("slide") is None]
    by_slide: dict[int, list[dict[str, Any]]] = {}
    for finding in findings:
        number = finding.get("slide")
        if number is not None:
            by_slide.setdefault(number, []).append(finding)

    theme = _theme_block(deck, spec)
    # What every call carries whatever it holds: the theme block and the
    # deck-level findings. Counted against the budget so a deck with a lot of
    # both does not quietly send oversized calls.
    fixed = _tokens(theme) + _tokens(deck_level)

    # Built once. They are what is being measured and what is being sent, and
    # building them twice on a 105-slide deck is a second full walk of every
    # shape for nothing.
    digests = {slide.number: _slide_digest(slide) for slide in deck.slides}

    groups = _pack(
        deck.slides, digests, by_slide, batch_size, budget_tokens, fixed
    )
    total = len(groups) or 1
    for index, chunk in enumerate(groups, start=1):
        numbers = [slide.number for slide in chunk]
        yield {
            "deck": deck.name,
            "batch": {"index": index, "of": total, "slides": sorted(numbers)},
            "slide_size_in": [deck.width_in, deck.height_in],
            **theme,
            "rule_findings": deck_level + [
                finding for number in numbers
                for finding in by_slide.get(number, [])
            ],
            "slides": [digests[number] for number in numbers],
        }


def _pack(
    slides: Sequence[SlideProfile],
    digests: dict[int, dict[str, Any]],
    by_slide: dict[int, list[dict[str, Any]]],
    ceiling: int,
    budget: int,
    fixed: int,
) -> list[list[SlideProfile]]:
    """Consecutive slides gathered into calls that fit the budget.

    Consecutive, and never reordered. Slides next to each other are about the
    same thing, and a model reading four of them together is reading a
    section; packed by size alone it would be reading four unrelated slides
    and the batch would tell it nothing.

    A slide bigger than the budget on its own still goes, alone. Refusing it
    would drop it from the review entirely, which is a worse answer than a
    large call.
    """
    groups: list[list[SlideProfile]] = []
    current: list[SlideProfile] = []
    running = 0

    for slide in slides:
        cost = _tokens(digests[slide.number]) + _tokens(by_slide.get(slide.number, []))
        too_big = current and (running + cost + fixed > budget)
        too_many = len(current) >= max(1, ceiling)
        if too_big or too_many:
            groups.append(current)
            current, running = [], 0
        current.append(slide)
        running += cost
    if current:
        groups.append(current)
    return groups


def _tokens(payload: Any) -> int:
    """Rough token count for a piece of payload.

    Calibrated, not assumed. The usual four-characters-to-a-token rule is for
    prose; this is JSON, which is mostly short keys, quotes, braces and
    numbers, and every one of those is its own token. A packed call estimated
    at 19,840 tokens by the prose rule came back from the API measured at
    44,732 -- so the rule was out by more than two, and a budget built on it
    would let calls run to twice the size it was written for.

    Two characters to a token matches what the API reported, and erring low
    would be the wrong direction: it is what puts too many slides in a call.
    """
    try:
        return len(json.dumps(payload, ensure_ascii=False)) // 2
    except Exception:
        return 0


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
