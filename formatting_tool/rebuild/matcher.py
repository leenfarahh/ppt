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
from math import ceil
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from ..classify import DISTINCTIVE, SlideKind, classify_layout, column_bands, fit_slide
from ..models import (
    DeckProfile,
    LayoutChoice,
    LayoutProfile,
    SlideProfile,
    normalize_layout_name,
)

# Placeholder tokens folded into the families that actually differ from each
# other. PowerPoint distinguishes OBJECT from BODY from CHART, but a layout
# offering any of them accepts the same block of slide content.
#
# A PICTURE REGION IS NOT ONE OF THEM, and that correction is what this table
# is really for. Folding it in with the rest was the reading that "any of them
# accepts content", which is true of PowerPoint and false of the deck: a master
# whose layouts are `Title with Content 01..07`, `Content with Image 01..02`
# and `Project Card` offers two kinds of region, and counting them as one kind
# makes every one of those layouts score the same. On a real 17-slide deck it
# sent all seven content slides to `Content with Image 01` -- image layouts,
# for slides with no image -- and reported low confidence on each, which is the
# matcher saying it could not tell and choosing anyway.
#
# Text in a picture region and a photograph in a body region are both wrong,
# and a designer sees which straight away. So they are counted apart, and a
# layout with a picture region it cannot fill loses by exactly as much as one
# missing a region the slide needs.
FAMILIES: dict[str, str] = {
    "TITLE": "title",
    "CENTER_TITLE": "title",
    "VERTICAL_TITLE": "title",
    "SUBTITLE": "subtitle",
    "BODY": "content",
    "VERTICAL_BODY": "content",
    "OBJECT": "content",
    "PICTURE": "picture",
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
    seen: Optional["LayoutChoice"] = None,
    seen_floor: float = 0.5,
) -> Optional[LayoutMatch]:
    """Pick the layout for one slide, or None when the master defines none.

    `seen` is what the AI layer read off the rendered slide, when there was a
    render and it named a layout the master has.
    """
    if not layouts:
        return None
    deck = deck or DeckProfile(path="", width_in=13.333, height_in=7.5,
                               slides=[slide])

    # What somebody looked at comes first. Above the structural fit, because
    # that fit is exactly what the render corrects.
    #
    # The structural signal counts content regions from the slide's own text
    # boxes, and a messy slide is a pile of loose ones -- that is what makes it
    # messy. On a real deck it read a table of contents as a four-region
    # comparison and a two-column slide the same way, where the model looking
    # at the render got both right. Measured on the five slides of that deck:
    # the model matched or beat the structural pick on four, and the one it
    # agreed on was the cover.
    if seen is not None and seen.confidence >= seen_floor:
        picked = _exactly(seen.layout, layouts)
        if picked is not None:
            return LayoutMatch(
                layout=picked,
                score=seen.confidence,
                basis=(
                    f"read off the rendered slide: {seen.why}"
                    if seen.why else "read off the rendered slide"
                ),
                confident=True,
                kind=classify_layout(picked).kind,
                layout_kind=classify_layout(picked).kind,
            )

    # What the slide is for, when that is knowable, beats what it is built
    # from. A photo cover reads structurally as a three-region content slide,
    # and matching it on structure alone puts it on a content layout.
    fit = fit_slide(slide, deck, layouts, score_structure=structure_score, floor=floor)
    return _with_name(slide, layouts, fit, floor)


# How far below the best a named layout may score and still be taken. Small on
# purpose: this settles a tie, it does not overturn a decision. Two layouts
# within a twentieth of each other are two answers the measurements cannot
# separate, and the designer's own name for one of them is better evidence
# than the order they happen to sit in the master.
_NAME_MARGIN = 0.05

# How sure the classifier has to be before its reading outranks the name the
# slide carries. `classify_slide` grades itself: a title saying "Agenda" is
# 0.9, a first slide with one region is 0.75, and "too little text to be a
# content slide" is 0.6. The first two are evidence; the last is a guess, and a
# guess does not move a slide off the layout its author named -- on a real deck
# it called the cover a section and took it off 'Title Slide'.
_CLASSIFICATION_TRUSTED = 0.75


def _with_name(
    slide: SlideProfile, layouts: Sequence[LayoutProfile], fit, floor: float
) -> LayoutMatch:
    """The structural pick, with the slide's own layout name breaking ties.

    THE NAME USED TO DECIDE THIS OUTRIGHT: a slide whose layout name existed in
    the master was placed on it, scored 1.0, called confident, and never
    measured. That is the right answer often enough -- two files agreeing on a
    name is usually a designer's statement -- and wrong in the case that
    matters most. A name survives everything: a deck rebuilt onto one master
    and then handed a new one carries the old names, a template renamed around
    its slides carries names that describe what a layout used to be, and an
    author duplicating "Title with Content 03" to make something else keeps the
    name. In every one of those the name is a label and the structure is the
    evidence, and the tool was trusting the label.

    So the structure decides, and the name is what settles a tie: where the
    named layout scores within `_NAME_MARGIN` of the best, it is taken. Ties
    are common and this is good evidence for breaking them -- it is certainly
    better than the tie-break it replaces, which was whichever layout came
    first in the master.

    The disagreement is said out loud either way. A slide that names one layout
    and measures as another is worth a designer's eye, whichever of the two it
    ends up on.
    """
    named = _by_name(slide.layout_name, layouts)
    match = LayoutMatch(
        layout=fit.layout,
        score=fit.score,
        basis=fit.basis,
        confident=fit.fit == "good",
        kind=fit.kind,
        layout_kind=fit.layout_kind,
    )
    if named is None or fit.layout is None:
        return match
    if named is fit.layout:
        match.basis = (
            f"{fit.basis}, and the slide already names it"
        )
        return match

    # Not for a cover, an agenda, a section or a closing slide. For those,
    # `fit_slide` has already declared structure not comparable -- a photo
    # cover shares no regions with a cover layout -- and settling a tie by
    # comparing structural scores would take that back through the side door.
    # The classification decided; the name may disagree in the report.
    if not any(
        count for family, count in slide_regions(slide).items()
        if family in ("content", "picture")
    ):
        # Nothing to measure, so nothing outranks the name. An empty slide, or
        # one carrying a title and no body, gives the structure no evidence at
        # all -- and "structure first" cannot mean preferring a silence to a
        # designer's own statement.
        return LayoutMatch(
            layout=named,
            score=1.0,
            basis=(
                f"the slide names {slide.layout_name!r} and carries nothing "
                "to measure against the master's layouts"
            ),
            confident=True,
            kind=fit.kind,
            layout_kind=classify_layout(named).kind,
        )

    if getattr(fit.classification, "confidence", 1.0) < _CLASSIFICATION_TRUSTED:
        return LayoutMatch(
            layout=named,
            score=structure_score(slide, named),
            basis=(
                f"the slide names {slide.layout_name!r}, and what it reads as "
                f"is a guess ({fit.classification.basis})"
            ),
            confident=True,
            kind=fit.kind,
            layout_kind=classify_layout(named).kind,
        )

    named_score = structure_score(slide, named)
    # Close enough to be a tie, and of the kind already chosen. The second half
    # is what keeps this from undoing the classification: choosing between two
    # content layouts is a tie-break, while moving a cover onto a content
    # layout because the name says so is what `fit_slide` refuses -- structure
    # is not comparable for a cover, an agenda, a section or a closing slide.
    a_tie = named_score >= fit.score - _NAME_MARGIN
    same_kind = classify_layout(named).kind is fit.layout_kind
    if a_tie and (same_kind or fit.kind not in DISTINCTIVE):
        return LayoutMatch(
            layout=named,
            score=named_score,
            basis=(
                f"the slide names {slide.layout_name!r} and it fits as well as "
                f"anything else here ({named_score:.2f} against "
                f"{fit.score:.2f} for {fit.layout.name!r})"
            ),
            confident=named_score >= floor or fit.fit == "good",
            kind=fit.kind,
            layout_kind=classify_layout(named).kind,
        )

    match.basis = (
        f"{fit.basis}. The slide names {slide.layout_name!r}, which the master "
        f"has, but it fits less well ({named_score:.2f} against "
        f"{fit.score:.2f})"
    )
    return match


def _exactly(name: str, layouts: Sequence[LayoutProfile]) -> Optional[LayoutProfile]:
    """The layout with exactly this name.

    Exact, not normalised. The name came back from a model and was already
    checked against the master's list; being strict here means a pick can only
    ever land on a layout that exists.
    """
    for layout in layouts:
        if layout.name == name:
            return layout
    return None


# How much of the score is counting regions, and how much is where they are.
#
# Counting dominates, because a layout that cannot hold the slide's content is
# wrong wherever its regions sit. Position is the tie-break, and on a real deck
# it is the whole of the decision: a messy slide made of fourteen loose text
# boxes asks for fourteen regions, no designed layout offers more than three,
# and every candidate scores the same 0.188. The match was then decided by
# which layout came first in the master -- seven content slides all sent to
# `Content with Image 01`, including the ones with no image.
#
# WHERE THE CONTENT SITS SURVIVES THAT. A photograph down the right half and a
# column of copy on the left is the same shape of slide whether the copy is in
# one placeholder or fourteen boxes, and it tells `Content with Image 01` from
# `Content with Image 02` -- which differ by nothing else.
_REGION_WEIGHT = 0.7
_PLACE_WEIGHT = 0.3


def structure_score(slide: SlideProfile, layout: LayoutProfile) -> float:
    """How well a layout serves this slide: what it offers, and where.

    Still 0.0 to 1.0, and still a fair thing to compare against `floor`: a
    layout that matches region for region and covers the same ground scores 1.0
    as it always did, and one that matches neither scores 0.
    """
    regions = _score(slide_regions(slide), layout_regions(layout))
    place = _place_score(slide, layout)
    fit = regions if place is None else (
        _REGION_WEIGHT * regions + _PLACE_WEIGHT * place
    )
    return fit * _crowding(slide, layout)


def _crowding(slide: SlideProfile, layout: LayoutProfile) -> float:
    """How much of the slide's copy this layout has somewhere to put, 0 to 1.

    Counting columns rather than boxes is right for CHOOSING a layout -- a
    heading, a list and a caption stacked in one column are one column of copy
    -- and it loses something that used to be carried by accident: how much
    copy there is. Twelve boxes and two boxes both read as one column, and a
    layout offering one content region is the right SHAPE for either, but it is
    only a good fit for the second.

    That difference belongs in the score because it is what `fit_slide`
    compares against its floor to decide whether to flag the slide, and a
    crammed slide rebuilt onto a one-region layout keeps its copy in loose
    boxes -- which is the thing a designer has to be told about.

    Nearly flat across the candidates, deliberately: it depends on how many
    regions a layout offers, and the layouts of one kind mostly offer the same
    number. So it lowers the scores of a messy slide together, leaving the
    ranking to the regions and their positions, and only tips the balance
    between layouts that differ in how much they can hold -- where it tips it
    the right way, towards the layout with room.
    """
    blocks = sum(
        1 for shape in slide.shapes
        if _slide_family(shape) == "content" and not shape.placeholder_type
    )
    if not blocks:
        return 1.0
    # Copy regions only. A picture region is not room for a body of text, and
    # counting it as room is a bug with a visible consequence: on a real deck
    # it sent slides with no photograph on them to an image layout, because
    # having two regions of any kind beat having one. The layout then arrives
    # with a half-canvas picture placeholder nothing can fill.
    offered = layout_regions(layout).get("content", 0)
    return min(1.0, (offered + 1) / (blocks + 1))


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
    pictures = 0
    loose: list = []

    for shape in slide.shapes:
        family = _family(shape.placeholder_token)
        if family is not None:
            regions[family] += 1
        elif shape.placeholder_type:
            continue          # a latent footer placeholder; not a content region
        elif _is_imagery(shape):
            pictures += 1
        elif shape.is_picture:
            graphics += 1     # an icon is part of the copy, not a photograph
        elif shape.paragraphs:
            loose.append(shape)
        else:
            graphics += 1

    # LOOSE COPY IS COUNTED IN COLUMNS, NOT IN BOXES, and this is the whole
    # difference between a match and a coin toss on a messy deck. One real
    # 17-slide deck put its copy in fourteen separate text boxes per slide, so
    # the slide asked for fourteen content regions; no designed layout offers
    # more than three, every candidate scored the same 0.188, and the pick fell
    # back to whichever layout came first in the master -- every content slide
    # in the deck onto one layout.
    #
    # Fourteen boxes is not fourteen demands. A heading with a list under it and
    # a caption beneath that is ONE column of copy, and a layout that offers one
    # content region can hold it. What the slide is really asking for is how
    # many columns of content it runs, which is what `classify.column_bands`
    # already answers for a different question, on the same evidence.
    #
    # A placeholder still counts one for one, above: it is already a region, and
    # a tidy deck should keep measuring exactly as it did.
    if loose:
        regions["content"] += max(1, len(column_bands(loose, _CANVAS_W)))

    # Both capped at one, and for the same reason: a row of decorative marks is
    # one visual block rather than five demands on the layout, and a slide
    # carrying four icons beside its copy is not asking for four picture
    # regions. What the cap records is that the slide HAS imagery, which is the
    # thing a layout either offers a home for or does not.
    if pictures:
        regions["picture"] += 1
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


# The canvas, as a coarse grid. Fine enough to tell one half of a slide from
# the other and one column from its neighbour; coarse enough that a shape a
# tenth of an inch out of line reads as being in the same place, which is what
# a person comparing a slide to a layout would say.
_GRID_X = 48
_GRID_Y = 27


def _place_score(slide: SlideProfile, layout: LayoutProfile) -> Optional[float]:
    """How much of the slide's content lands where the layout offers room.

    An overlap of the two areas over their union, on a grid. None when either
    side has nothing to compare -- an empty slide, or a layout of chrome alone
    -- and then the caller falls back to counting regions, which is what this
    did before there was anything else.

    Both directions again, for the reason `_score` gives: content with nowhere
    to go is a defect, and a region left empty is a different one.
    """
    # MEASURED PER FAMILY, not over everything at once. Comparing the union of
    # a slide's content against the union of a layout's regions rewards a
    # layout for being BIG: an agenda layout with twenty-five placeholders, or
    # a section divider with a full-height picture region, covers most of the
    # canvas and so overlaps most of anything. Both came out ahead of the
    # content layouts on a real deck's content slides, and only the kind filter
    # kept them from being chosen.
    #
    # Asked family by family the question is the one that matters: is the title
    # where this layout puts titles, is the copy where it puts copy, is the
    # photograph where it puts photographs.
    scores: list[tuple[float, float]] = []
    for family, weight in _FAMILY_WEIGHTS.items():
        wanted = _cells(
            shape.geometry for shape in slide.shapes
            if _slide_family(shape) == family
        )
        offered = _cells(
            shape.geometry for shape in layout.placeholders
            if _family(shape.placeholder_token) == family
        )
        if not wanted and not offered:
            continue            # neither has one; not a difference
        if not wanted or not offered:
            scores.append((0.0, weight))
            continue
        scores.append((len(wanted & offered) / len(wanted | offered), weight))
    if not scores:
        return None
    everything = (
        sum(score * weight for score, weight in scores)
        / sum(weight for _score, weight in scores)
    )

    # And the image on its own, weighted heavily for its size. It is the first
    # thing a designer would look at -- a photograph down the left half and a
    # column of copy on the right is a layout you can name from the doorway --
    # and on a master whose image layouts differ by nothing else, it is the
    # only thing that tells them apart. `Content with Image 01` puts its
    # picture left and `02` puts it right; without this, a slide with a
    # left-hand photograph took whichever of the two the rest of its copy
    # happened to favour.
    pictures = _picture_overlap(slide, layout)
    if pictures is None:
        return everything
    return _CONTENT_PLACE * everything + _PICTURE_PLACE * pictures


_CONTENT_PLACE = 0.6
_PICTURE_PLACE = 0.4

# A photograph is the strongest statement a slide makes about its shape, so
# where it sits counts for more than where a line of copy sits.
_FAMILY_WEIGHTS = {"title": 1.0, "subtitle": 1.0, "content": 1.0, "picture": 1.5}


def _slide_family(shape: Any) -> Optional[str]:
    """Which family a slide's shape belongs to, placeholder or not.

    A messy deck's copy is in loose boxes rather than placeholders, and they
    are still content: the question this answers is what the shape IS, not how
    it was authored.
    """
    if shape.placeholder_token in LATENT:
        return None
    family = _family(shape.placeholder_token)
    if family is not None:
        return family
    if _is_imagery(shape):
        return "picture"
    if shape.paragraphs:
        return "content"
    return None


def _picture_overlap(
    slide: SlideProfile, layout: LayoutProfile
) -> Optional[float]:
    """How far the slide's imagery and the layout's picture regions agree.

    None when either side has none, which is not a disagreement: a slide with
    no photograph and a layout with no picture region are already counted as
    matching by `slide_regions`, and one of each without the other is a
    difference that counting has priced in. This answers a narrower question --
    given that both have imagery, is it in the same place.
    """
    wanted = _cells(
        shape.geometry for shape in slide.shapes if _is_imagery(shape)
    )
    offered = _cells(
        shape.geometry for shape in layout.placeholders
        if shape.placeholder_token == "PICTURE"
    )
    if not wanted or not offered:
        return None
    return len(wanted & offered) / len(wanted | offered)


# How much of the canvas an image has to cover before it is asking for a
# layout's picture region. Not a delicate number: measured across one real
# deck, the photographs run 27% and 44% of the canvas and the icons sitting
# inside the copy run 0.2% each, so anything between them gives the same
# answer. What it is not is zero, and that was the bug -- three 0.4in icons
# beside a body of text made a slide with no photograph on it read as an image
# slide, and it went to an image layout with a half-canvas picture region
# nothing could fill.
_IMAGERY_SHARE = 0.04


def _is_imagery(shape: Any) -> bool:
    """Whether this shape is a photograph, rather than an icon in the copy.

    A picture PLACEHOLDER counts whatever its size: the deck's author put the
    slide's image in the slot made for one, which is a statement about what it
    is rather than a measurement.
    """
    if shape.placeholder_token == "PICTURE":
        return True
    if not shape.is_picture:
        return False
    box = shape.geometry
    return (box.width_in * box.height_in) >= _IMAGERY_SHARE * _CANVAS_W * _CANVAS_H


def _is_content(shape: Any) -> bool:
    """Whether this shape is content the layout has to find room for."""
    if shape.placeholder_token in LATENT:
        return False
    return bool(shape.paragraphs or shape.is_picture or shape.placeholder_type)


def _cells(boxes: Iterable[Any]) -> set:
    """The grid cells a set of boxes covers, on a canvas of 13.33 x 7.5in.

    Taken as fractions of the canvas rather than inches, so a 4:3 deck and a
    16:9 one are measured the same way and a slide is comparable to a layout
    from a master of another size.
    """
    covered: set = set()
    for box in boxes:
        if box is None or box.width_in <= 0 or box.height_in <= 0:
            continue
        left = max(0.0, box.left_in / _CANVAS_W)
        right = min(1.0, (box.left_in + box.width_in) / _CANVAS_W)
        top = max(0.0, box.top_in / _CANVAS_H)
        bottom = min(1.0, (box.top_in + box.height_in) / _CANVAS_H)
        if right <= left or bottom <= top:
            continue            # entirely off the canvas
        for x in range(int(left * _GRID_X), max(int(left * _GRID_X) + 1,
                                                ceil(right * _GRID_X))):
            for y in range(int(top * _GRID_Y), max(int(top * _GRID_Y) + 1,
                                                   ceil(bottom * _GRID_Y))):
                covered.add((min(x, _GRID_X - 1), min(y, _GRID_Y - 1)))
    return covered


# The canvas the fractions above are taken against. A deck and a master of
# different sizes are still comparable this way, which is the point: the
# question is which third of the page the content sits in, not how many inches
# from the edge.
_CANVAS_W = 13.333
_CANVAS_H = 7.5


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
