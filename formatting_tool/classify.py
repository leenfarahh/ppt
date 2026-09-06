"""What kind of slide is this, and which master layout is it for.

Two classifiers and a matcher between them.

A slide's *kind* is its job in the deck: a cover, an agenda, a section
divider, a content page, a closing. A layout's kind is the job it was drawn
for. Fitting a slide to a layout by structure alone gets the ordinary cases
right and the distinctive ones badly wrong: a photo cover has a full-bleed
image and three floating text boxes, which reads structurally as a
three-region content slide, and lands on a content layout.

So the two signals are used where each is reliable. For cover, agenda,
section and closing, purpose decides: those slides are recognisable by what
they say and how little they carry, and their structure is not comparable to
anything. For content and columns, structure decides: how many content
regions a slide has is measurable, and a guess about its purpose is not.

Nothing here reads the messy deck's own layouts. A deck arrives named by
whatever master it was built from, and those names describe a different
brand's system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from .models import (
    DeckProfile,
    LayoutProfile,
    ShapeProfile,
    SlideProfile,
    walk_shapes,
)

# Placeholders that frame a slide rather than carry its content. They must not
# count towards the block tallies that decide whether a slide reads as columns.
_CHROME = frozenset(
    {"TITLE", "CENTER_TITLE", "VERTICAL_TITLE", "SUBTITLE",
     "FOOTER", "SLIDE_NUMBER", "DATE"}
)


class SlideKind(str, Enum):
    COVER = "cover"
    AGENDA = "agenda"
    SECTION = "section"       # a divider between parts of the deck
    CONTENT = "content"
    COLUMNS = "columns"       # parallel blocks read side by side
    DIAGRAM = "diagram"       # built from shapes rather than copy
    CLOSING = "closing"       # thank you, questions, contact
    UNKNOWN = "unknown"


# Kinds where the slide's job is a better guide than its shape count. A cover
# and a content slide can carry the same number of boxes and want completely
# different layouts.
DISTINCTIVE = frozenset(
    {SlideKind.COVER, SlideKind.AGENDA, SlideKind.SECTION, SlideKind.CLOSING}
)

# Title wording that names the slide's job outright. Prezlab decks are
# bilingual, so the Arabic equivalents carry the same weight as the English.
KEYWORDS: list[tuple[SlideKind, tuple[str, ...]]] = [
    (
        SlideKind.AGENDA,
        (
            "agenda", "contents", "table of contents", "overview",
            "what we will cover", "what we'll cover", "in this document",
            "المحتويات", "جدول الأعمال", "جدول الاعمال", "نظرة عامة",
        ),
    ),
    (
        SlideKind.CLOSING,
        (
            "thank you", "thanks", "questions", "q&a", "any questions",
            "get in touch", "contact us", "appendix",
            "شكرا", "شكرًا", "شكراً", "الأسئلة", "أسئلة", "تواصل معنا",
        ),
    ),
]

# A slide carrying less copy than this is making a statement, not an argument:
# a cover, a divider, a closing. Well above a long action title, well below
# the lightest real content slide seen in practice.
SPARSE_CHARS = 400
# A divider is sparser still, and carries almost nothing but its own label.
SECTION_CHARS = 140
SECTION_SHAPES = 4
# Parallel blocks that have to line up before a slide reads as columns.
MIN_COLUMNS = 2
MAX_COLUMNS = 4
# Columns are the whole point of a columns slide, so there cannot be many
# other blocks and there cannot be much copy. Without these two a dense
# content page reads as columns the moment any few of its forty boxes happen
# to line up, which on real decks is most of them.
COLUMN_MAX_BOXES = 8
COLUMN_MAX_CHARS = 1200
# A slide is built from shapes rather than written when the loose graphics
# outnumber the text regions by this much, with enough of them to be a system.
DIAGRAM_RATIO = 2.0
DIAGRAM_FLOOR = 6

# Matched as whole words against the layout name, never as raw substrings.
# "13_Content slide _ VCS_to use" collapses to "contentslide" if the
# separators are stripped, which contains "contents" and turns every content
# layout in a real master into an agenda layout.
_LAYOUT_NAMES: list[tuple[SlideKind, tuple[str, ...]]] = [
    (SlideKind.COVER, ("cover", "title slide", "title page", "opening",
                       "front page", "titre")),
    (SlideKind.AGENDA, ("agenda", "contents", "table of contents", "toc")),
    (SlideKind.SECTION, ("section", "divider", "chapter", "break", "transition")),
    (SlideKind.CLOSING, ("thank you", "thanks", "closing", "back cover",
                         "contact", "end")),
    (SlideKind.COLUMNS, ("two column", "two columns", "2 column", "2 columns",
                         "three column", "three columns", "3 column",
                         "3 columns", "columns", "comparison", "split",
                         "side by side")),
    (SlideKind.DIAGRAM, ("diagram", "chart", "full bleed", "image only")),
    # Last, so that a layout named "Two Column Content" is read as columns
    # rather than as a plain content page.
    (SlideKind.CONTENT, ("content", "content slide", "body", "one column",
                         "1 column", "single column", "text")),
]

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD = re.compile(r"[^0-9a-z]+")


@dataclass
class Classification:
    """A kind, how sure the classifier is, and the evidence for it."""

    kind: SlideKind
    confidence: float
    basis: str


@dataclass
class SlideFit:
    """One slide, what it is, and the master layout that serves it."""

    slide: int
    classification: Classification
    layout: Optional[LayoutProfile]
    layout_kind: SlideKind
    score: float
    basis: str
    #  good   the master has a layout for this slide and the content fits it
    #  loose  the layout is the right kind, but the slide carries more or
    #         fewer blocks than it offers; a person should look
    #  none   the master has no layout for this kind of slide at all. Not a
    #         fit that needs adjusting, a layout that does not exist
    fit: str

    @property
    def kind(self) -> SlideKind:
        return self.classification.kind

    @property
    def layout_name(self) -> Optional[str]:
        return self.layout.name if self.layout else None


# --------------------------------------------------------------------------- #
# Slides
# --------------------------------------------------------------------------- #

def classify_slide(slide: SlideProfile, deck: DeckProfile) -> Classification:
    """Work out what job a slide is doing.

    Checked in order of how much the evidence is worth: wording that names the
    job outright, then the unmistakable shape of a cover, then sparseness,
    then structure. The first match wins, so a slide titled "Agenda" is an
    agenda however it happens to be built.
    """
    signals = _Signals.of(slide, deck)

    keyword = _by_keyword(signals)
    if keyword is not None:
        return keyword

    cover = _as_cover(signals)
    if cover is not None:
        return cover

    if not signals.chars:
        # A divider carries a label. A slide with no words at all is a blank
        # or a work in progress, and guessing at its job helps nobody.
        return Classification(
            SlideKind.UNKNOWN,
            0.3,
            f"no text at all, in {len(slide.shapes)} shape(s)",
        )

    # Columns before section: a sparse slide with two labelled blocks side by
    # side is a comparison, not a divider, and a divider has no parallel
    # structure to mistake for one.
    columns = _as_columns(signals)
    if columns is not None:
        return columns

    if signals.chars <= SECTION_CHARS and signals.text_shapes <= SECTION_SHAPES:
        return Classification(
            SlideKind.SECTION,
            0.6,
            f"{signals.chars} characters in {signals.text_shapes} text box(es), "
            f"too little to be a content slide",
        )

    if (
        signals.graphics >= DIAGRAM_FLOOR
        and signals.graphics >= signals.text_shapes * DIAGRAM_RATIO
    ):
        return Classification(
            SlideKind.DIAGRAM,
            0.5,
            f"{signals.graphics} loose graphic(s) against "
            f"{signals.text_shapes} text box(es)",
        )

    return Classification(
        SlideKind.CONTENT,
        0.4,
        f"{signals.regions} content region(s), {signals.chars} characters",
    )


def _by_keyword(signals: "_Signals") -> Optional[Classification]:
    """A title that names the slide's job.

    Only trusted on a slide light enough to be what it says. A content slide
    arguing about the agenda is not an agenda slide, and a long page whose
    title happens to contain "overview" is not a contents page.
    """
    lowered = signals.title.casefold().strip()
    if not lowered or signals.chars > SPARSE_CHARS * 2:
        return None
    for kind, words in KEYWORDS:
        for word in words:
            if word in lowered:
                return Classification(
                    kind, 0.9, f"the title says {signals.title.strip()[:40]!r}"
                )
    return None


def _as_cover(signals: "_Signals") -> Optional[Classification]:
    """The opening slide, recognised by what it does not have.

    Two shapes of cover. The photographic one is an image across the whole
    canvas with a few words over it; the typographic one is a title and a
    subtitle and nothing else. Both carry almost no copy, which is what
    separates them from a content slide that merely opens with an image.
    """
    if signals.chars > SPARSE_CHARS:
        return None

    if signals.full_bleed_picture:
        return Classification(
            SlideKind.COVER,
            0.85 if signals.first else 0.6,
            f"an image covers the canvas behind {signals.chars} characters",
        )

    if signals.first and signals.regions <= 3:
        return Classification(
            SlideKind.COVER,
            0.75,
            f"first slide, {signals.chars} characters in "
            f"{signals.regions} region(s)",
        )

    if signals.has_title and signals.has_subtitle and signals.content_placeholders == 0:
        return Classification(
            SlideKind.COVER,
            0.6 if signals.first else 0.45,
            "a title and subtitle with no content placeholder",
        )
    return None


def _as_columns(signals: "_Signals") -> Optional[Classification]:
    """Parallel blocks meant to be read across, not down.

    The bands are counted by which quarter of the canvas a block starts in,
    rather than by exact left edges: a heading and the list beneath it are one
    column even when the heading is nudged a little to the left.
    """
    if not MIN_COLUMNS <= len(signals.column_bands) <= MAX_COLUMNS:
        return None
    if signals.body_boxes > COLUMN_MAX_BOXES:
        return None
    if signals.body_chars > COLUMN_MAX_CHARS:
        return None
    return Classification(
        SlideKind.COLUMNS,
        0.6,
        f"{len(signals.column_bands)} bands of content across the canvas, "
        f"{signals.body_boxes} block(s) in total",
    )


# --------------------------------------------------------------------------- #
# Layouts
# --------------------------------------------------------------------------- #

def classify_layout(layout: LayoutProfile) -> Classification:
    """What job a master layout was drawn for.

    The name first, because a designer names a layout for its purpose and
    that is the most direct statement of intent available. Structure only
    when the name says nothing.
    """
    words = _words(layout.name)
    for kind, fragments in _LAYOUT_NAMES:
        for fragment in fragments:
            if f" {fragment} " in words:
                return Classification(
                    kind, 0.85, f"the layout is named {layout.name!r}"
                )

    tokens = [s.placeholder_token for s in layout.placeholders]
    titles = sum(1 for t in tokens if t in ("TITLE", "CENTER_TITLE", "VERTICAL_TITLE"))
    subtitles = sum(1 for t in tokens if t == "SUBTITLE")
    content = sum(
        1 for t in tokens
        if t in ("BODY", "OBJECT", "PICTURE", "CHART", "TABLE", "VERTICAL_BODY")
    )

    if titles and subtitles and not content:
        return Classification(SlideKind.COVER, 0.5, "a title and subtitle, no content")
    if content >= 2:
        return Classification(
            SlideKind.COLUMNS, 0.5, f"{content} content placeholders"
        )
    if titles and not content:
        return Classification(
            SlideKind.SECTION, 0.4, "a title with no content placeholder"
        )
    return Classification(SlideKind.CONTENT, 0.3, "one title and one content region")


# --------------------------------------------------------------------------- #
# Fitting
# --------------------------------------------------------------------------- #

def fit_slide(
    slide: SlideProfile,
    deck: DeckProfile,
    layouts: Sequence[LayoutProfile],
    *,
    score_structure,
    floor: float,
) -> SlideFit:
    """Classify one slide and choose the master layout that serves it.

    `score_structure(slide, layout) -> float` is passed in rather than
    imported so that this module stays independent of the rebuild's region
    arithmetic and can be tested on its own.
    """
    classification = classify_slide(slide, deck)
    if not layouts:
        return SlideFit(
            slide=slide.number,
            classification=classification,
            layout=None,
            layout_kind=SlideKind.UNKNOWN,
            score=0.0,
            basis="the master defines no layouts",
            fit="none",
        )

    scored = [(score_structure(slide, layout), layout) for layout in layouts]
    # Ties break toward the earlier layout, the order the designer arranged
    # the master in and so the more canonical choice.
    best_score, best = max(scored, key=lambda pair: (pair[0], -pair[1].index))

    same_kind = [
        pair for pair in scored
        if classify_layout(pair[1]).kind is classification.kind
    ]
    if same_kind:
        score, layout = max(same_kind, key=lambda pair: (pair[0], -pair[1].index))
        # For the distinctive kinds, structure was already declared not
        # comparable -- a photo cover shares no regions with a cover layout.
        # Grading the fit by the score it was chosen in spite of would only
        # take that back.
        fits = classification.kind in DISTINCTIVE or score >= floor
        return SlideFit(
            slide=slide.number,
            classification=classification,
            layout=layout,
            layout_kind=classification.kind,
            score=score,
            basis=(
                f"the master's {classification.kind.value} layout {layout.name!r}"
                if fits
                else (
                    f"{layout.name!r} is the right kind of layout, but the "
                    f"slide's blocks do not line up with the regions it offers"
                )
            ),
            fit="good" if fits else "loose",
        )

    if classification.kind in DISTINCTIVE:
        # A categorical gap, not a fit to adjust. A cover has no structural
        # counterpart on a content layout, so approximating it quietly would
        # hide the thing worth knowing: the master cannot make this slide.
        return SlideFit(
            slide=slide.number,
            classification=classification,
            layout=best,
            layout_kind=classify_layout(best).kind,
            score=best_score,
            basis=(
                f"the master has no {classification.kind.value} layout; "
                f"{best.name!r} is the closest of what there is"
            ),
            fit="none",
        )

    # An ordinary slide with no layout of its own kind. Structure is a fair
    # guide here: a content layout will hold columns, just not gracefully.
    return SlideFit(
        slide=slide.number,
        classification=classification,
        layout=best,
        layout_kind=classify_layout(best).kind,
        score=best_score,
        basis=f"closest content structure of the master's {len(layouts)} layout(s)",
        fit="good" if best_score >= floor else "loose",
    )


def layout_coverage(layouts: Sequence[LayoutProfile]) -> dict[SlideKind, list[str]]:
    """Which kinds of slide the master can actually serve."""
    coverage: dict[SlideKind, list[str]] = {}
    for layout in layouts:
        coverage.setdefault(classify_layout(layout).kind, []).append(layout.name)
    return coverage


def missing_kinds(
    fits: Sequence[SlideFit], layouts: Sequence[LayoutProfile]
) -> list[SlideKind]:
    """Slide kinds the deck needs and the master cannot make.

    Only the distinctive kinds count. A content layout will hold a columns
    slide awkwardly, so reporting "no columns layout" over-claims; nothing
    will hold a cover, so reporting "no cover layout" is a real blocker.
    """
    covered = set(layout_coverage(layouts))
    wanted = {fit.kind for fit in fits if fit.kind in DISTINCTIVE}
    return sorted(wanted - covered, key=lambda kind: kind.value)


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #

@dataclass
class _Signals:
    """Everything the classifier reads, measured once."""

    first: bool
    last: bool
    chars: int
    title: str
    text_shapes: int
    graphics: int
    regions: int
    has_title: bool
    has_subtitle: bool
    content_placeholders: int
    full_bleed_picture: bool
    body_boxes: int
    body_chars: int
    column_bands: list[int]

    @classmethod
    def of(cls, slide: SlideProfile, deck: DeckProfile) -> "_Signals":
        top = slide.shapes
        tokens = [s.placeholder_token for s in top if s.placeholder_token]
        text = [s for s in top if s.paragraphs]
        graphics = [s for s in top if not s.paragraphs and not s.placeholder_type]
        body = [s for s in text if s.text.strip() and s.placeholder_token not in _CHROME]

        canvas = max(deck.width_in * deck.height_in, 1e-6)
        biggest = max(
            (
                s.geometry.width_in * s.geometry.height_in
                for s in walk_shapes(top)
                if s.is_picture
            ),
            default=0.0,
        )

        return cls(
            first=slide.number == 1,
            last=bool(deck.slides) and slide.number == deck.slides[-1].number,
            chars=sum(len(s.text.strip()) for s in walk_shapes(top)),
            title=_title_text(top),
            text_shapes=len(text),
            graphics=len(graphics),
            regions=len(text) + (1 if graphics else 0),
            has_title=any(
                t in ("TITLE", "CENTER_TITLE", "VERTICAL_TITLE") for t in tokens
            ),
            has_subtitle=any(t == "SUBTITLE" for t in tokens),
            content_placeholders=sum(
                1 for t in tokens
                if t in ("BODY", "OBJECT", "PICTURE", "CHART", "TABLE")
            ),
            full_bleed_picture=biggest >= 0.9 * canvas,
            body_boxes=len(body),
            body_chars=sum(len(s.text.strip()) for s in body),
            column_bands=_column_bands(body, deck.width_in),
        )


def _title_text(shapes: list[ShapeProfile]) -> str:
    """The slide's title, or the topmost text standing in for one.

    A messy deck often has no title placeholder at all, and the line a reader
    would call the title is then just the highest box on the slide.
    """
    for shape in shapes:
        if shape.placeholder_token in ("TITLE", "CENTER_TITLE", "VERTICAL_TITLE"):
            if shape.text.strip():
                return shape.text
    candidates = [s for s in shapes if s.text.strip()]
    if not candidates:
        return ""
    return min(candidates, key=lambda s: s.geometry.top_in).text


def _column_bands(body: list[ShapeProfile], width_in: float) -> list[int]:
    """Which quarters of the canvas the body content starts in.

    Quarters rather than exact left edges, so a column heading sitting a
    little proud of the list beneath it still counts as one column and not two.
    """
    if width_in <= 0:
        return []
    quarter = width_in / 4
    return sorted({int(max(shape.geometry.left_in, 0.0) // quarter) for shape in body})


def _words(name: str) -> str:
    """A layout name as space-separated words, padded for whole-word matching.

    "TitleSlide_02" becomes " title slide 02 ", so a fragment can be tested as
    " title slide " without matching the middle of a longer word.
    """
    spaced = _CAMEL.sub(" ", name)
    return f" {_NON_WORD.sub(' ', spaced.lower()).strip()} "
