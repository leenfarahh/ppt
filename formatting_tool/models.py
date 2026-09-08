"""Data model shared by every stage of the pipeline.

Stage 1  extract/  ->  DeckProfile (what the deck actually contains)
Stage 2  extract/  ->  MasterSpec  (what the deck is supposed to contain)
Stage 3  rules/    ->  Issue(source=RULE)
Stage 4  ai/       ->  Issue(source=AI)
Stage 5  report/   ->  ValidationReport

Everything here is a plain dataclass on purpose: the model is filled in by
python-pptx readers, serialized into the Gemini request payload, and written
out as JSON, so it stays dependency-free.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from hashlib import sha1
from pathlib import Path
from typing import Any, Iterator, Optional


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #

class Severity(str, Enum):
    BLOCKER = "blocker"   # ships broken: missing logo, unreadable text
    ERROR = "error"       # off-brand: wrong palette colour, wrong typeface
    WARNING = "warning"   # inconsistent but defensible
    INFO = "info"         # observation, no action required


SEVERITY_RANK: dict[str, int] = {
    Severity.BLOCKER.value: 0,
    Severity.ERROR.value: 1,
    Severity.WARNING.value: 2,
    Severity.INFO.value: 3,
}


class Category(str, Enum):
    COLOR = "color"
    FONT_FAMILY = "font_family"
    FONT_SIZE = "font_size"
    LOGO = "logo"
    TITLE = "title"
    SUBTITLE = "subtitle"
    TYPOGRAPHY = "typography"   # orphans, widows, rag, hyphenation
    SPACE = "space"             # safe margins, overflow, overlap, alignment
    LAYOUT = "layout"           # slide-to-layout binding, layout completeness
    OTHER = "other"


class Source(str, Enum):
    RULE = "rule"   # deterministic validation layer
    AI = "ai"       # Gemini validation layer


class TextRole(str, Enum):
    TITLE = "title"
    SUBTITLE = "subtitle"
    BODY = "body"
    CAPTION = "caption"
    FOOTER = "footer"
    UNKNOWN = "unknown"


class Provenance(str, Enum):
    """Where a brand value came from, which decides how much it is worth.

    A finding that rests on an AUTHORED value is a brand violation. The same
    finding resting on an INFERRED value is only a departure from what the
    master deck happens to do, which is a weaker claim and has to be labelled
    as one.
    """

    AUTHORED = "authored"   # stated in the brand reference file
    INFERRED = "inferred"   # observed in the master deck, nobody stated it
    DEFAULT = "default"     # built-in fallback, nobody signed off on it
    MISSING = "missing"     # not found anywhere; dependent rules stay silent


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

@dataclass
class Issue:
    """One inconsistency, from either validation layer."""

    category: Category
    severity: Severity
    message: str
    source: Source = Source.RULE
    rule_id: Optional[str] = None       # the deterministic rule, for a RULE issue
    # AI issues only: the ref (R7) of the rule finding this restates. Kept
    # apart from rule_id, which names a rule a consumer can look up; a ref is
    # meaningful only inside the run that produced it.
    confirms: Optional[str] = None
    slide: Optional[int] = None         # 1-based slide number; None = deck-level
    shape: Optional[str] = None         # shape name as it appears in the deck
    # The OOXML shape id, unique within its slide. Names are not: a real deck
    # routinely carries sixteen shapes called "Pentagon 7" on one slide, so a
    # fix matched on name alone would land on whichever came first.
    shape_id: Optional[int] = None
    deck: Optional[str] = None          # filled in by the pipeline
    expected: Optional[str] = None
    found: Optional[str] = None
    suggestion: Optional[str] = None
    confidence: Optional[float] = None  # AI layer only
    # AI layer only: whether the claim was made by looking at the rendered
    # slide ("render") or reasoned from the numbers ("geometry"). It is the
    # difference between "the text does not collide" as an observation and as
    # a guess, and a designer weighing a finding needs to know which.
    evidence: Optional[str] = None
    # A mechanical correction the AI layer proposed, when it could name one.
    # None on every rule finding: those carry a measured target in `expected`
    # and their fixers read it from there.
    fix: Optional["FixAction"] = None
    # Short stable handle, assigned once the finding is final. It is what a
    # designer ticks and what `apply --fix` takes, so it has to survive a
    # round trip through JSON and mean the same thing on the next run over an
    # unchanged deck. Derived from what the finding is about, never from its
    # position in the list, which moves as other findings come and go.
    id: Optional[str] = None

    def dedupe_key(self) -> tuple:
        """Identity used to collapse a rule finding and its AI restatement."""
        return (self.deck, self.slide, self.shape, self.category.value, self.found)

    def fingerprint(self) -> str:
        parts = [
            self.deck or "",
            str(self.slide or ""),
            self.shape or "",
            str(self.shape_id or ""),
            self.rule_id or self.category.value,
            self.found or "",
            self.message,
        ]
        return sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:8]

    def to_dict(self) -> dict[str, Any]:
        return enum_safe(asdict(self))


# Actions a machine can carry out from a finding the AI layer wrote. Closed on
# purpose: an open set would let the model name an operation nothing
# implements, and a fix that cannot be run is worse than a finding that says
# it needs a designer, because it reads as an offer.
#
# There is no move or resize here, and that is a decision rather than an
# omission. The model is told not to measure off a rendered image, and
# geometry is what the deterministic layer proves from the file; a coordinate
# from the model would be exactly the guess that instruction exists to stop.
FIX_OPS = frozenset({
    "recolor_fill",
    "recolor_line",
    "recolor_text",
    "set_font",
    "set_font_size",
    "disable_autofit",
    "delete_empty_paragraphs",
})


@dataclass
class FixAction:
    """One mechanical correction, as the AI layer proposed it.

    Every value is a proposal, never an instruction. The applier validates the
    target against the master before it touches the file -- a colour has to be
    a palette entry, a typeface has to be approved, a size has to sit in the
    role's range -- and refuses anything that is not, so the worst a bad
    proposal can do is cost itself.
    """

    op: str
    shape: Optional[str] = None         # exact name as the payload gave it
    hex: Optional[str] = None           # six hex digits, no leading hash
    font: Optional[str] = None
    size_pt: Optional[float] = None

    @property
    def valid(self) -> bool:
        return self.op in FIX_OPS


@dataclass
class LayoutChoice:
    """Which master layout a slide belongs on, as read off its rendered image.

    Separate from Issue because it is not a finding. A finding says something
    is wrong; this says where the slide should go, and it is an input to
    applying the master rather than something a designer ticks.
    """

    slide: int                  # 1-based
    layout: str                 # a name the master actually has
    confidence: float = 0.0
    why: str = ""


@dataclass
class SkippedRule:
    """A check that never ran, and what it was waiting for.

    A rule that reports nothing because it is disabled looks exactly like a
    rule that reports nothing because the deck is clean. This is the
    difference, on the report where a reader will see it.
    """

    rule_id: str
    reason: str
    unlocked_by: str = ""


@dataclass
class ValidationReport:
    master: str
    decks: list[str]
    generated_at: str
    issues: list[Issue] = field(default_factory=list)
    guidelines: Optional[str] = None
    ai_enabled: bool = False
    ai_summary: Optional[str] = None
    stats: dict[str, int] = field(default_factory=dict)
    # Brand values that were inferred from the master deck rather than stated
    # in the reference file. Findings resting on these are the weaker claims in
    # the report, so it says which they are.
    inferred_values: list[str] = field(default_factory=list)
    # The checks that never ran. Nothing else stands between what the rules
    # found and what is printed: no layer can remove a finding.
    skipped_rules: list[SkippedRule] = field(default_factory=list)
    # What the AI layer was sent and what it returned, verbatim, per batch.
    # Populated on request: it is the only way to tell a model that missed
    # something from a payload that never described it.
    ai_exchanges: list[dict[str, Any]] = field(default_factory=list)
    # What applying the master did, per deck, when it ran before the checks.
    # On the report because it changes what every finding is about: these were
    # measured on the restyled deck, not on the file that was uploaded.
    master_applied: list[dict[str, Any]] = field(default_factory=list)

    # The MasterSpec the run measured against, left here by the pipeline so a
    # later apply can check a proposed fix against the same values the
    # findings came from.
    #
    # Deliberately NOT annotated: an annotated name in a dataclass becomes a
    # field, and a field is serialised. This is working state that lives as
    # long as the process, not part of the report -- and asdict on a spec
    # would copy every layout and every shape on it into the JSON.
    spec = None

    def to_dict(self) -> dict[str, Any]:
        return enum_safe(asdict(self))


# --------------------------------------------------------------------------- #
# Extracted deck structure
# --------------------------------------------------------------------------- #

@dataclass
class Geometry:
    """Shape box in inches, because every brand rule is written in inches."""

    left_in: float
    top_in: float
    width_in: float
    height_in: float
    rotation: float = 0.0

    @property
    def right_in(self) -> float:
        return self.left_in + self.width_in

    @property
    def bottom_in(self) -> float:
        return self.top_in + self.height_in


@dataclass
class RunProfile:
    """A text run: the smallest unit that carries its own formatting."""

    text: str
    font_name: Optional[str] = None
    size_pt: Optional[float] = None
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    underline: Optional[bool] = None
    color_hex: Optional[str] = None     # six hex digits, no leading hash
    color_is_theme: bool = False        # True when it resolves through the theme
    # The theme slot it binds to ("accent1", "dk2"), when it binds to one. A
    # theme-bound colour is only as correct as the theme behind it: a deck
    # carrying its own theme resolves accent1 to its own accent1, which is
    # off-brand on screen while the run itself looks blameless.
    color_theme: Optional[str] = None
    language: Optional[str] = None      # drives Arabic vs Latin font rules


@dataclass
class ParagraphProfile:
    text: str
    level: int = 0
    alignment: Optional[str] = None
    space_before_pt: Optional[float] = None
    space_after_pt: Optional[float] = None
    line_spacing: Optional[float] = None
    runs: list[RunProfile] = field(default_factory=list)


# The alt text that marks a shape as the presentation space: the area a layout
# offers for content. Matched on the whole string, casefolded, so a shape
# described "PS logo lockup" is not mistaken for a frame.
#
# Deliberately not "pres_space", which is what some existing masters carry.
# The convention being asked of designers is "PS", and a reader that quietly
# accepted both would leave nobody able to tell which masters had been updated.
# A master still on the old mark reads as having no presentation space, and
# falls back to the frame derived from its placeholders.
PRESENTATION_SPACE_ALT = "ps"

# Placeholders that sit in the margin on purpose, so they never widen the
# usable frame. Shared with extract.master_spec, which derives the deck-wide
# frame from the same tokens: two copies would let the per-layout frame and the
# deck-wide one disagree about whether a footer counts as content.
MARGIN_CHROME = frozenset({"FOOTER", "SLIDE_NUMBER", "DATE"})


@dataclass
class ShapeProfile:
    shape_id: int
    name: str
    shape_type: str
    geometry: Geometry
    placeholder_type: Optional[str] = None
    placeholder_idx: Optional[int] = None   # the ph idx a slide shape binds to
    role: TextRole = TextRole.UNKNOWN
    text: str = ""
    paragraphs: list[ParagraphProfile] = field(default_factory=list)
    fill_hex: Optional[str] = None
    line_hex: Optional[str] = None
    # Theme slots, on the same terms as RunProfile.color_theme. A shape fill
    # bound to the theme carries no literal RGB, so `fill_hex` is None and the
    # colour rules saw nothing at all until these existed.
    fill_theme: Optional[str] = None
    line_theme: Optional[str] = None
    # The colours inside an SVG icon. PowerPoint calls this a Graphics Fill
    # and gives it its own ribbon tab; it is not `a:solidFill` on the shape,
    # so `fill_hex` is None for every icon and the colour rules saw none of
    # them. See `formatting_tool.svgicon`.
    graphic_colors: list = field(default_factory=list)
    is_picture: bool = False
    is_group: bool = False
    image_sha1: Optional[str] = None    # identifies a logo asset across decks
    autofit: Optional[str] = None
    word_wrap: Optional[bool] = None
    # cNvPr/@descr. Carried because a designer can put a machine-readable mark
    # in it, and one convention depends on that: a rectangle described "PS"
    # marks out the presentation space (see PRESENTATION_SPACE_ALT).
    alt_text: str = ""
    children: list["ShapeProfile"] = field(default_factory=list)

    @property
    def placeholder_token(self) -> Optional[str]:
        return placeholder_token(self.placeholder_type)

    @property
    def is_presentation_space(self) -> bool:
        return self.alt_text.strip().casefold() == PRESENTATION_SPACE_ALT


@dataclass
class SlideProfile:
    number: int                          # 1-based
    slide_id: Optional[int] = None
    layout_name: Optional[str] = None
    shapes: list[ShapeProfile] = field(default_factory=list)
    notes: str = ""
    hidden: bool = False


@dataclass
class LayoutProfile:
    """One slide layout, read from the master rather than from a slide.

    Slides carry direct formatting that hides what the layout actually
    defines, so the layout has to be read in its own right: to check that it
    is complete (header and footer furniture), and to decide which layout a
    messy slide should be rebuilt onto.
    """

    name: str
    index: int                           # position in the master, 0-based
    shapes: list[ShapeProfile] = field(default_factory=list)

    @property
    def placeholders(self) -> list[ShapeProfile]:
        return [s for s in self.shapes if s.placeholder_type]

    @property
    def presentation_space(self) -> list[ShapeProfile]:
        """The shapes this layout marks "PS": where content may go.

        A layout can carry several. Two columns are drawn as two rectangles,
        which is the point of reading them per layout rather than deck-wide:
        a two-column layout genuinely offers a different area than a
        full-width one, and one frame for the whole master cannot say so.
        """
        return [s for s in walk_shapes(self.shapes) if s.is_presentation_space]

    @property
    def declared_left_edges(self) -> list[float]:
        """Every left edge this layout offers content to start at.

        Both kinds of declaration count: a PS rectangle and a content
        placeholder are each the master saying "content goes here", and a
        two-column layout states three of them (its outer edge and one per
        column). One number per side cannot hold that, which is why the
        margin frame and this are separate readings of the same shapes.

        Chrome is excluded for the same reason it is excluded from the frame:
        a page number's left edge is furniture, not a column.
        """
        boxes = [s.geometry for s in self.presentation_space]
        boxes += [
            s.geometry
            for s in self.placeholders
            if s.placeholder_token not in MARGIN_CHROME
        ]
        return sorted({round(b.left_in, 3) for b in boxes})

    def content_frame(self) -> Optional[Geometry]:
        """The area this layout offers, as one box, or None if it says nothing.

        The union of the PS rectangles *and* the layout's own content
        placeholders, which is not the same as the PS union alone. Every real
        master drawn so far puts PS around the body area only, leaving the
        title band outside it: on one, PS runs from 2.00in down while the title
        sits at 0.40in. Taking PS by itself would put every title outside the
        frame it was placed by, so the placeholders are unioned in and PS does
        what it is for -- widening the frame past them, which is also what
        those masters do, PS reaching 0.59in where the placeholders stop at
        0.92in.

        Chrome is left out: a footer or page number lives in the margin by
        design, so including it would open the frame to the slide edge.
        """
        boxes = [s.geometry for s in self.presentation_space]
        if not boxes:
            return None
        boxes += [
            s.geometry
            for s in self.placeholders
            if s.placeholder_token not in MARGIN_CHROME
        ]
        left = min(b.left_in for b in boxes)
        top = min(b.top_in for b in boxes)
        return Geometry(
            left_in=round(left, 4),
            top_in=round(top, 4),
            width_in=round(max(b.right_in for b in boxes) - left, 4),
            height_in=round(max(b.bottom_in for b in boxes) - top, 4),
        )


@dataclass
class DeckProfile:
    path: str
    width_in: float
    height_in: float
    slides: list[SlideProfile] = field(default_factory=list)
    layouts: list[LayoutProfile] = field(default_factory=list)
    theme_fonts: dict[str, str] = field(default_factory=dict)   # major / minor
    theme_colors: dict[str, str] = field(default_factory=dict)  # accent1 -> hex

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def layout_names(self) -> list[str]:
        return [layout.name for layout in self.layouts]


# --------------------------------------------------------------------------- #
# Brand guidelines (authored) and master spec (derived)
# --------------------------------------------------------------------------- #

@dataclass
class Margins:
    """The usable area of the slide, per edge.

    None means unspecified, and the safe-margin rule skips that edge. There is
    no sensible default: a frame invented here would have the rule reporting
    every deck against a margin nobody set.
    """

    top_in: Optional[float] = None
    right_in: Optional[float] = None
    bottom_in: Optional[float] = None
    left_in: Optional[float] = None


@dataclass
class Tolerances:
    """How much drift the deterministic layer forgives before reporting."""

    color_delta_e: float = 3.0     # CIEDE2000; about 2 is imperceptible
    size_pt: float = 0.5
    position_in: float = 0.05


@dataclass
class RoleSpec:
    """Expected typography for one text role, e.g. slide titles."""

    role: TextRole
    fonts: list[str] = field(default_factory=list)
    min_size_pt: Optional[float] = None
    max_size_pt: Optional[float] = None
    colors: list[str] = field(default_factory=list)
    max_lines: Optional[int] = None
    required: bool = False


@dataclass
class LogoSpec:
    required_on_every_slide: bool = False
    required_on_first_slide: bool = True
    min_width_in: Optional[float] = None
    clear_space_in: Optional[float] = None
    allowed_corners: list[str] = field(default_factory=list)  # tl, tr, bl, br
    asset_sha1: list[str] = field(default_factory=list)       # approved logo files


@dataclass
class RuleTuning:
    """Thresholds that are not brand values.

    These are judgement calls about how a rule decides something is a defect,
    not statements about the brand, so a brand reference file will never supply
    them. They live here rather than as constants inside the rule classes so
    that everything a rule tests against arrives from one reviewable place,
    and so a noisy rule can be quietened without editing code.

    Each one starts as a guess. Tune them against a deck known to be correct.
    """

    # Fraction of the canvas a shape must cover to read as intentional bleed
    # rather than as content running off the slide.
    bleed_coverage: float = 0.9
    # Square inches of overlap below which two boxes are merely touching.
    min_overlap_in2: float = 0.02
    # Shapes that must share a left edge before it counts as a grid line.
    grid_support: int = 4
    # Fraction of a set that has to agree before the agreement reads as the
    # intent and the rest as departures from it. Below this there is no
    # majority, only a scatter, and a scatter has no odd one out.
    majority_fraction: float = 0.6
    # Multiple of position tolerance within which a miss is drift rather than
    # deliberate placement. Beyond it, the shape was put there on purpose.
    near_miss_factor: float = 4.0
    # Multiple of colour tolerance within which a palette entry is close
    # enough to name as the intended colour in a suggestion.
    suggestion_factor: float = 4.0
    # Roles where size variation across slides is a defect. Body copy varies
    # with content density by design; a title does not.
    strict_size_roles: list[str] = field(
        default_factory=lambda: ["title", "subtitle", "footer"]
    )
    # Depth of the strips at the top and bottom of a layout that a shape has
    # to sit in to read as header or footer furniture, as a fraction of slide
    # height. The footer strip is the shallower of the two by convention.
    header_band_fraction: float = 0.15
    footer_band_fraction: float = 0.12
    # Placeholder-signature overlap a layout must reach before a slide is
    # rebuilt onto it without the match being called out for review.
    layout_match_floor: float = 0.5
    # Identical shapes that have to be present before they read as a series
    # laid out on purpose rather than a coincidence of two equal boxes.
    repeat_min_members: int = 3
    # How far a member of a series can sit from the shared edge and still be
    # drift rather than a deliberate placement somewhere else.
    repeat_max_drift_in: float = 0.75
    # A satellite is a repeated shape that travels with a partner: a number on
    # a badge, an icon in a card, a tick beside a bullet. Its offset from that
    # partner should be the same on every copy, and these govern that check.
    #
    # How far apart two offsets can be and still count as the same placement.
    satellite_cluster_in: float = 0.05
    # Centres this close are the same place, not two places. Decks stack a
    # filled shape and its glyph in one box, and counting both makes a set of
    # five icons look like ten.
    satellite_colocated_in: float = 0.01
    # Pairs that have to share an offset before it reads as the intended one.
    # A mirrored layout has two such groups and both are legitimate, so this
    # is a floor on each rather than a majority of the whole.
    #
    # It sets the size of the smallest set worth looking at, too: a cohort plus
    # one member departing from it, so one above this. That is deliberately not
    # a second knob, because two knobs can be set to contradict each other and
    # the smaller would then be dead.
    satellite_cohort_min: int = 3
    # How far a satellite may sit from its partner, in multiples of the
    # partner's diagonal. Keeps pairing local: two repeated things at opposite
    # ends of a slide are not a component.
    satellite_reach: float = 2.5


@dataclass
class TypographyRules:
    max_orphan_words: int = 1        # a lone word left on the last line
    min_widow_chars: int = 4         # a stub pushed onto a new line
    max_title_lines: int = 2
    allow_hyphenation: bool = False


@dataclass
class BrandGuidelines:
    """The brand rules a deck is measured against.

    Produced two ways: extracted from a brand reference file by
    `brandbook/`, or written by hand. Either way it is reviewed by a person
    before it is trusted, and `provenance` records which values were actually
    stated in the reference file as opposed to inferred from a master deck.
    """

    name: str = "unnamed"
    palette: dict[str, str] = field(default_factory=dict)     # navy -> 1F2A44
    allowed_fonts: list[str] = field(default_factory=list)
    arabic_fonts: list[str] = field(default_factory=list)
    roles: dict[str, RoleSpec] = field(default_factory=dict)
    logo: LogoSpec = field(default_factory=LogoSpec)
    safe_margins: Margins = field(default_factory=Margins)
    typography: TypographyRules = field(default_factory=TypographyRules)
    tolerances: Tolerances = field(default_factory=Tolerances)
    tuning: RuleTuning = field(default_factory=RuleTuning)
    notes: str = ""      # free prose handed verbatim to the AI layer

    # Field path -> Provenance value, e.g. {"roles.title.min_size_pt":
    # "inferred"}. Absent keys read as DEFAULT.
    provenance: dict[str, str] = field(default_factory=dict)
    # The reference file the values were extracted from, for the report header.
    source: Optional[str] = None

    def provenance_of(self, path: str) -> Provenance:
        try:
            return Provenance(self.provenance.get(path, Provenance.DEFAULT.value))
        except ValueError:
            return Provenance.DEFAULT

    def paths_with(self, *provenance: Provenance) -> list[str]:
        wanted = {p.value for p in provenance}
        return sorted(k for k, v in self.provenance.items() if v in wanted)

    def to_dict(self) -> dict[str, Any]:
        return enum_safe(asdict(self))


@dataclass
class MasterSpec:
    """What the messy deck is measured against.

    Built by merging two sources: the authored BrandGuidelines, and whatever
    can be observed in the master deck (theme colours, theme fonts,
    placeholder geometry). The guidelines win on conflict -- the master deck is
    evidence, not truth.
    """

    source: str
    width_in: float
    height_in: float
    guidelines: BrandGuidelines
    palette: dict[str, str] = field(default_factory=dict)
    allowed_fonts: list[str] = field(default_factory=list)
    roles: dict[str, RoleSpec] = field(default_factory=dict)
    observed_sizes_pt: dict[str, list[float]] = field(default_factory=dict)
    logo_geometry: Optional[Geometry] = None
    layouts: list[LayoutProfile] = field(default_factory=list)
    # The master's own theme. A messy deck carries a theme too, and it is the
    # foreign brand's: a rule that compares a deck against its own theme is
    # asking whether the wrong brand was applied consistently. Every reference
    # to a theme downstream reads these, never DeckProfile.theme_*.
    theme_fonts: dict[str, str] = field(default_factory=dict)
    theme_colors: dict[str, str] = field(default_factory=dict)
    # The usable area, per edge. Authored values win; the rest are read off
    # the master's own layouts, which is where a designer actually drew the
    # frame. Without this the safe-margin check needed a brand file and so
    # never ran on a master-only run, which is most runs.
    safe_margins: Margins = field(default_factory=Margins)
    # The left edges the master declares content may start at, deduped within
    # the position tolerance. Empty when no layout marks its presentation
    # space, and the alignment check then falls back to inferring a grid from
    # the deck it is auditing, which is what it always did.
    #
    # Separate from safe_margins because they answer different questions off
    # the same shapes. "Is this inside the usable area" needs one number per
    # side. "Is this on the grid" needs all of them: a two-column layout has
    # three legitimate left edges and a frame can only report the outermost.
    grid_edges_in: list[float] = field(default_factory=list)

    @property
    def tolerances(self) -> Tolerances:
        return self.guidelines.tolerances

    @property
    def layout_names(self) -> list[str]:
        return [layout.name for layout in self.layouts]

    def layout_named(self, name: Optional[str]) -> Optional[LayoutProfile]:
        """Look a layout up by name, case- and separator-insensitively.

        Layout names are free text: they get renamed, localized, and
        punctuated differently between masters, so an exact match alone
        reports drift that is not there.
        """
        if not name:
            return None
        wanted = normalize_layout_name(name)
        for layout in self.layouts:
            if normalize_layout_name(layout.name) == wanted:
                return layout
        return None

    def to_dict(self) -> dict[str, Any]:
        return enum_safe(asdict(self))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def placeholder_token(value: Optional[str]) -> Optional[str]:
    """"SUBTITLE (4)" -> "SUBTITLE". Also tolerates a bare enum name.

    python-pptx renders PP_PLACEHOLDER members with their numeric value
    attached. Comparisons are on the exact token, never a substring: "TITLE"
    is a substring of "SUBTITLE".
    """
    if not value:
        return None
    return value.split("(")[0].strip().upper()


def normalize_layout_name(name: str) -> str:
    """Fold a layout name to something two masters can be compared on.

    Case, spacing, underscores and hyphens all vary between a designer's
    master and the same layout after a round trip through PowerPoint, and none
    of that variation means the layout changed.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def walk_shapes(shapes: list[ShapeProfile]) -> Iterator[ShapeProfile]:
    """Depth-first walk that descends into groups.

    Grouped shapes are where messy decks hide their defects, so no caller
    should ever iterate `slide.shapes` directly.
    """
    for shape in shapes:
        yield shape
        if shape.children:
            yield from walk_shapes(shape.children)


def guideline_paths() -> list[str]:
    """Every addressable brand value, as a dotted path.

    Derived from the dataclasses rather than hand-listed, so the extractor,
    the inference pass, the YAML writer and the loader cannot drift apart when
    a field is added. `tuning` is excluded: it is not a brand value, so it has
    no provenance.
    """
    paths = ["palette", "allowed_fonts", "arabic_fonts", "notes"]
    for role in TextRole:
        if role is TextRole.UNKNOWN:
            continue
        for spec_field in fields(RoleSpec):
            if spec_field.name == "role":
                continue
            paths.append(f"roles.{role.value}.{spec_field.name}")
    for group, cls in (
        ("logo", LogoSpec),
        ("safe_margins", Margins),
        ("typography", TypographyRules),
        ("tolerances", Tolerances),
    ):
        paths.extend(f"{group}.{f.name}" for f in fields(cls))
    return paths


def enum_safe(value: Any) -> Any:
    """Make any part of this model JSON-serializable.

    Two things JSON refuses: an Enum member (asdict keeps them) and a
    dataclass instance (which reaches here whenever a caller serializes a
    field rather than a whole object, e.g. guidelines.logo).
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return enum_safe(asdict(value))
    if isinstance(value, dict):
        return {enum_safe(k): enum_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [enum_safe(v) for v in value]
    return value
