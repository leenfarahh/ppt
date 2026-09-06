"""Decide which master layout a messy slide should be rebuilt onto.

Three signals, in descending order of how much they are worth. An exact
(normalized) layout-name match is taken at face value: a designer names a
layout for its purpose, and two masters agreeing on a name is the strongest
statement available. Failing that, the slide's *kind* decides when the kind is
one structure cannot express -- see `formatting_tool.classify`. Failing both,
content regions decide.

Structure here means content regions, not placeholders. A messy slide keeps
most of its content in loose text boxes -- that is what makes it messy -- so
counting only its placeholders would say every slide in the deck wants nothing
but a title. A slide with a title and four text boxes wants a layout offering
a title and four content blocks, whether or not the deck ever bound them to
anything.

Counts matter, not just kinds: two content blocks and five are different
slides, and collapsing both to "has content" would put a comparison slide on a
single-column layout. Nothing here drops a slide. One that matches nothing
well is still placed, on the best layout available, and flagged for a designer.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Optional, Sequence

from ..classify import SlideKind, classify_layout, fit_slide
from ..models import DeckProfile, LayoutProfile, SlideProfile, normalize_layout_name

# Placeholder tokens folded into the families that actually differ from each
# other. PowerPoint distinguishes OBJECT from BODY from PICTURE, but a layout
# offering any of them accepts the same block of slide content.
FAMILIES: dict[str, str] = {
    "TITLE": "title",
    "CENTER_TITLE": "title",
    "VERTICAL_TITLE": "title",
    "SUBTITLE": "subtitle",
    "BODY": "content",
    "VERTICAL_BODY": "content",
    "OBJECT": "content",
    "PICTURE": "content",
    "CHART": "content",
    "TABLE": "content",
    "MEDIA_CLIP": "content",
    "ORG_CHART": "content",
}

# Placeholders PowerPoint keeps latent: they live on the layout but are never
# cloned onto a slide, so they cannot count for or against a match.
LATENT = frozenset({"FOOTER", "SLIDE_NUMBER", "DATE"})


@dataclass
class LayoutMatch:
    """The chosen layout, the score behind it, and whether to trust it."""

    layout: LayoutProfile
    score: float
    basis: str
    confident: bool
    kind: SlideKind = SlideKind.UNKNOWN          # what the slide is for
    layout_kind: SlideKind = SlideKind.UNKNOWN   # what the layout is drawn for

    @property
    def name(self) -> str:
        return self.layout.name


def choose_layout(
    slide: SlideProfile,
    layouts: Sequence[LayoutProfile],
    floor: float,
    deck: Optional[DeckProfile] = None,
) -> Optional[LayoutMatch]:
    """Pick the layout for one slide, or None when the master defines none."""
    if not layouts:
        return None
    deck = deck or DeckProfile(path="", width_in=13.333, height_in=7.5,
                               slides=[slide])

    named = _by_name(slide.layout_name, layouts)
    if named is not None:
        return LayoutMatch(
            layout=named,
            score=1.0,
            basis=f"layout name {slide.layout_name!r} matches the master",
            confident=True,
            kind=classify_layout(named).kind,
        )

    # What the slide is for, when that is knowable, beats what it is built
    # from. A photo cover reads structurally as a three-region content slide,
    # and matching it on structure alone puts it on a content layout.
    fit = fit_slide(slide, deck, layouts, score_structure=structure_score, floor=floor)
    return LayoutMatch(
        layout=fit.layout,
        score=fit.score,
        basis=fit.basis,
        confident=fit.fit == "good",
        kind=fit.kind,
        layout_kind=fit.layout_kind,
    )


def structure_score(slide: SlideProfile, layout: LayoutProfile) -> float:
    """How well a layout's content regions match the slide's, 0.0 to 1.0."""
    return _score(slide_regions(slide), layout_regions(layout))


def slide_regions(slide: SlideProfile) -> Counter:
    """How many content regions of each kind the slide actually carries.

    Groups are counted as one region rather than walked into: a grouped
    diagram is a single thing that needs a single home on the new layout.
    Loose shapes with no text frame -- rules, dividers, background art -- are
    counted together as at most one region, because a row of decorative marks
    is one visual block and not five demands on the layout.
    """
    regions: Counter = Counter()
    graphics = 0

    for shape in slide.shapes:
        family = _family(shape.placeholder_token)
        if family is not None:
            regions[family] += 1
        elif shape.placeholder_type:
            continue          # a latent footer placeholder; not a content region
        elif shape.paragraphs:
            regions["content"] += 1
        else:
            graphics += 1

    if graphics:
        regions["content"] += 1
    return regions


def layout_regions(layout: LayoutProfile) -> Counter:
    """How many content regions of each kind the layout offers a slide."""
    regions: Counter = Counter()
    for shape in layout.placeholders:
        family = _family(shape.placeholder_token)
        if family is not None:
            regions[family] += 1
    return regions


def _family(token: Optional[str]) -> Optional[str]:
    if not token or token in LATENT:
        return None
    return FAMILIES.get(token)


def _by_name(
    name: Optional[str], layouts: Sequence[LayoutProfile]
) -> Optional[LayoutProfile]:
    if not name:
        return None
    wanted = normalize_layout_name(name)
    for layout in layouts:
        if normalize_layout_name(layout.name) == wanted:
            return layout
    return None


def _score(wanted: Counter, offered: Counter) -> float:
    """Weighted Jaccard over region counts: shared regions over total demand.

    Both directions cost something. A layout missing a region the slide needs
    scores down because the content has nowhere to go; a layout offering more
    regions than the slide fills scores down too, because empty placeholders
    are their own defect. A slide with nothing on it fits anything.
    """
    keys = set(wanted) | set(offered)
    if not keys:
        return 1.0
    overlap = sum(min(wanted[key], offered[key]) for key in keys)
    total = sum(max(wanted[key], offered[key]) for key in keys)
    return overlap / total if total else 1.0
