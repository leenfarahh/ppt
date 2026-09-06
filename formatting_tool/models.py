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
    deck: Optional[str] = None          # filled in by the pipeline
    expected: Optional[str] = None
    found: Optional[str] = None
    suggestion: Optional[str] = None
    confidence: Optional[float] = None  # AI layer only

    def dedupe_key(self) -> tuple:
        """Identity used to collapse a rule finding and its AI restatement."""
        return (self.deck, self.slide, self.shape, self.category.value, self.found)

    def to_dict(self) -> dict[str, Any]:
        return enum_safe(asdict(self))


@dataclass
class Dismissal:
    """A rule finding the AI layer judged a false positive, and its reason.

    Recorded rather than merely dropped. A dismissal deletes something the
    deterministic layer proved from the file, so it is a claim in its own
    right and has to be as reviewable as the finding it removes.
    """

    rule_id: Optional[str]
    reason: str
    deck: Optional[str] = None
    slide: Optional[int] = None
    shape: Optional[str] = None
    message: str = ""
    severity: Optional[str] = None


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
    # What the report does not show, and why. Between them these two account
    # for the gap between what the rules found and what is printed.
    dismissals: list[Dismissal] = field(default_factory=list)
    skipped_rules: list[SkippedRule] = field(default_factory=list)

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
    is_picture: bool = False
    is_group: bool = False
    image_sha1: Optional[str] = None    # identifies a logo asset across decks
    autofit: Optional[str] = None
    word_wrap: Optional[bool] = None
    children: list["ShapeProfile"] = field(default_factory=list)

    @property
    def placeholder_token(self) -> Optional[str]:
        return placeholder_token(self.placeholder_type)


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
