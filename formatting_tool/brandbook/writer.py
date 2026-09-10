"""Write extracted guidelines as YAML a designer can review and correct.

The output is the deliverable of the extraction step, and its job is to be
checked, not just parsed. Every value carries a trailing comment saying where
it came from: the quote and page from the reference file, or the master-deck
observation it was inferred from, or a note that nothing specifies it. A
reviewer can work down the file and confirm or fix each line without opening
the brand book at every entry.

PyYAML cannot emit comments, so this is a small hand-rolled emitter for this
one document shape rather than a general serializer. The result is read back by
`guidelines.load_guidelines`, and `tests` round-trips it to keep the two
honest.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from .. import __version__
from ..models import BrandGuidelines, Provenance, TextRole
from .inference import InferenceResult
from .mapping import Rejection

# Longest evidence quote kept in a comment. Enough to recognise the passage,
# short enough to keep the file readable.
QUOTE_CHARS = 72

HEADER = """\
# Brand guidelines for {name}
#
# Extracted from : {source}
# Master deck    : {master}
# Generated      : {generated} by formatting-tool {version}
#
# REVIEW BEFORE USE. Each value is annotated with where it came from:
#
#   authored  stated in the reference file; the quote and page follow
#   inferred  observed in the master deck, not stated anywhere
#   MISSING   nothing specifies it; the checks that need it stay silent
#
# Correct any value that is wrong. When you have confirmed an inferred value
# is genuinely the brand rule, change its entry in the provenance block at the
# bottom to `authored` so reports stop hedging it.
"""


def write_guidelines_yaml(
    guidelines: BrandGuidelines,
    *,
    evidence: Optional[dict[str, str]] = None,
    pages: Optional[dict[str, int]] = None,
    inference: Optional[InferenceResult] = None,
    rejections: Optional[list[Rejection]] = None,
    master: Optional[str] = None,
    unspecified: Optional[list[str]] = None,
) -> str:
    """Render the annotated YAML document."""
    writer = _Writer(
        guidelines=guidelines,
        evidence=evidence or {},
        pages=pages or {},
        inferred=(inference.inferred if inference else {}),
    )
    return writer.render(
        rejections=rejections or [],
        master=master,
        unspecified=unspecified or [],
    )


class _Writer:
    def __init__(
        self,
        guidelines: BrandGuidelines,
        evidence: dict[str, str],
        pages: dict[str, int],
        inferred: dict[str, str],
    ) -> None:
        self.g = guidelines
        self.evidence = evidence
        self.pages = pages
        self.inferred = inferred
        self.lines: list[str] = []

    # -- annotation --------------------------------------------------------- #

    def note(self, path: str, value: Any = None) -> str:
        """The trailing comment for one field path.

        `value` is what is being emitted on the line, which changes what a
        MISSING annotation has to say: a null genuinely disables the dependent
        check, whereas a built-in default standing in does not, and a reviewer
        must be able to tell those apart at a glance.
        """
        provenance = self.g.provenance_of(path)

        if provenance is Provenance.AUTHORED:
            return self._authored(path)

        if provenance is Provenance.INFERRED:
            return f"inferred: {self.inferred.get(path, 'from the master deck')}"

        if provenance is Provenance.MISSING:
            if value is None or value == [] or value == {}:
                return "MISSING - not specified; dependent checks stay silent"
            return f"MISSING - not specified; standing in with the built-in {value!r}"

        return "built-in default - not from the brand"

    def _authored(self, path: str) -> str:
        """State the page and quote only when they are actually known."""
        page = self.pages.get(path)
        quote = _collapse(self.evidence.get(path, ""))
        if page and quote:
            return f"authored p{page}: {quote!r}"
        if page:
            return f"authored p{page}"
        if quote:
            return f"authored: {quote!r}"
        return "authored"

    def item_note(self, item_path: str, group_path: str) -> str:
        """Comment for one entry inside a group, carrying its own quote.

        Provenance is tracked per group, not per palette entry, so an item
        falls back to the group's provenance while still showing the evidence
        recorded for the entry itself.
        """
        if item_path in self.evidence or item_path in self.pages:
            return self._authored(item_path)
        return self.note(group_path)

    # -- emit helpers ------------------------------------------------------- #

    def out(self, text: str = "", comment: str = "") -> None:
        if comment and text:
            self.lines.append(f"{text}    # {comment}")
        elif comment:
            self.lines.append(f"# {comment}")
        else:
            self.lines.append(text)

    def scalar(self, key: str, value: Any, path: str, indent: int = 0) -> None:
        pad = " " * indent
        self.out(f"{pad}{key}: {_yaml_scalar(value)}", self.note(path, value))

    # -- document ----------------------------------------------------------- #

    def render(
        self,
        rejections: list[Rejection],
        master: Optional[str],
        unspecified: list[str],
    ) -> str:
        self.out(
            HEADER.format(
                name=self.g.name,
                source=self.g.source or "not recorded",
                master=master or "none supplied",
                generated=date.today().isoformat(),
                version=__version__,
            ).rstrip()
        )
        self.out()
        self.out(f"name: {_yaml_scalar(self.g.name)}")
        if self.g.source:
            self.out(f"source: {_yaml_scalar(self.g.source)}")

        self._palette()
        self._fonts()
        self._roles()
        self._logo()
        self._margins()
        self._typography()
        self._tolerances()
        self._tuning()
        self._notes()
        self._provenance()
        self._appendix(rejections, unspecified)
        return "\n".join(self.lines).rstrip() + "\n"

    def _palette(self) -> None:
        self.out()
        self.out("# Label -> hex. Labels appear verbatim in reports.")
        if not self.g.palette:
            self.out("palette: {}", self.note("palette"))
            return
        self.out("palette:", self.note("palette", self.g.palette))
        for label, hex_value in self.g.palette.items():
            self.out(
                f"  {_yaml_key(label)}: {_yaml_scalar('#' + hex_value)}",
                self.item_note(f"palette.{label}", "palette"),
            )

    def _fonts(self) -> None:
        for path, label, values in (
            ("allowed_fonts", "Latin typefaces", self.g.allowed_fonts),
            ("arabic_fonts", "Arabic typefaces", self.g.arabic_fonts),
        ):
            self.out()
            self.out(comment=label)
            if not values:
                self.out(f"{path}: []", self.note(path))
                continue
            self.out(f"{path}:", self.note(path, values))
            for value in values:
                self.out(
                    f"  - {_yaml_scalar(value)}",
                    self.item_note(f"{path}.{value}", path),
                )

    def _roles(self) -> None:
        self.out()
        self.out(comment="Type scale per text role.")
        if not self.g.roles:
            self.out("roles: {}")
            return
        self.out("roles:")
        for role in TextRole:
            if role is TextRole.UNKNOWN:
                continue
            spec = self.g.roles.get(role.value)
            if spec is None:
                continue
            prefix = f"roles.{role.value}"
            self.out(f"  {role.value}:")
            self.out(
                f"    fonts: {_yaml_inline_list(spec.fonts)}",
                self.note(f"{prefix}.fonts", spec.fonts),
            )
            self.scalar("min_size_pt", spec.min_size_pt, f"{prefix}.min_size_pt", 4)
            self.scalar("max_size_pt", spec.max_size_pt, f"{prefix}.max_size_pt", 4)
            self.out(
                f"    colors: {_yaml_inline_list(['#' + c if _is_hex(c) else c for c in spec.colors])}",
                self.note(f"{prefix}.colors", spec.colors),
            )
            self.scalar("max_lines", spec.max_lines, f"{prefix}.max_lines", 4)
            self.scalar("required", spec.required, f"{prefix}.required", 4)

    def _logo(self) -> None:
        logo = self.g.logo
        self.out()
        self.out("logo:")
        self.scalar(
            "required_on_every_slide",
            logo.required_on_every_slide,
            "logo.required_on_every_slide",
            2,
        )
        self.scalar(
            "required_on_first_slide",
            logo.required_on_first_slide,
            "logo.required_on_first_slide",
            2,
        )
        self.scalar("min_width_in", logo.min_width_in, "logo.min_width_in", 2)
        self.scalar("clear_space_in", logo.clear_space_in, "logo.clear_space_in", 2)
        self.out(
            f"  allowed_corners: {_yaml_inline_list(logo.allowed_corners)}",
            self.note("logo.allowed_corners", logo.allowed_corners),
        )
        self.out("  # SHA-1 of each approved logo file. Without these a logo is")
        self.out("  # matched by shape name, which lets a re-export through. Get them")
        self.out("  # with: formatting-tool profile --deck master.pptx")
        self.out(f"  asset_sha1: {_yaml_inline_list(logo.asset_sha1)}")

    def _margins(self) -> None:
        self.out()
        self.out(comment="Usable area. Text crossing these edges is reported.")
        self.out("safe_margins:")
        for side in ("top", "right", "bottom", "left"):
            attr = f"{side}_in"
            self.scalar(
                attr, getattr(self.g.safe_margins, attr), f"safe_margins.{attr}", 2
            )

    def _typography(self) -> None:
        self.out()
        self.out("typography:")
        for key in (
            "max_orphan_words",
            "min_widow_chars",
            "max_title_lines",
            "allow_hyphenation",
        ):
            self.scalar(key, getattr(self.g.typography, key), f"typography.{key}", 2)

    def _tolerances(self) -> None:
        self.out()
        self.out("# How much drift is forgiven before a finding is reported.")
        self.out("# Loosen these first if the report is noisy.")
        self.out("tolerances:")
        for key in ("color_delta_e", "size_pt", "position_in"):
            self.scalar(key, getattr(self.g.tolerances, key), f"tolerances.{key}", 2)

    def _tuning(self) -> None:
        tuning = self.g.tuning
        self.out()
        self.out("# Not brand values: how the rules decide something is a defect.")
        self.out("# A brand book will never specify these. Tune them against a deck")
        self.out("# you know is correct.")
        self.out("tuning:")
        self.out(f"  bleed_coverage: {tuning.bleed_coverage}")
        self.out(f"  min_overlap_in2: {tuning.min_overlap_in2}")
        self.out(f"  grid_support: {tuning.grid_support}")
        self.out(f"  near_miss_factor: {tuning.near_miss_factor}")
        self.out(f"  suggestion_factor: {tuning.suggestion_factor}")
        self.out(f"  strict_size_roles: {_yaml_inline_list(tuning.strict_size_roles)}")
        self.out(f"  series_fit_min_affected: {tuning.series_fit_min_affected}")
        self.out(f"  series_fit_max_shrink: {tuning.series_fit_max_shrink}")
        self.out(f"  min_legible_pt: {tuning.min_legible_pt}")

    def _notes(self) -> None:
        self.out()
        self.out("# Rules that are not numbers. Passed to the AI reviewer verbatim.")
        if not self.g.notes.strip():
            self.out('notes: ""', self.note("notes", ""))
            return
        self.out("notes: |", self.note("notes"))
        for line in self.g.notes.splitlines():
            self.out(f"  {line}")

    def _provenance(self) -> None:
        self.out()
        self.out("# Machine-readable provenance. The report reads this to say which")
        self.out("# findings rest on inferred rather than authored values.")
        if not self.g.provenance:
            self.out("provenance: {}")
            return
        self.out("provenance:")
        for path in sorted(self.g.provenance):
            self.out(f'  "{path}": {self.g.provenance[path]}')

    def _appendix(self, rejections: list[Rejection], unspecified: list[str]) -> None:
        if not rejections and not unspecified:
            return
        self.out()
        self.out(comment="-" * 68)
        if rejections:
            self.out(comment="Readings discarded during extraction, and why:")
            for rejection in rejections:
                self.out(comment=f"  {rejection}")
        if unspecified:
            self.out(comment="The model reported these as unspecified in the document:")
            for name in unspecified:
                self.out(comment=f"  {name}")


# --------------------------------------------------------------------------- #
# Scalars
# --------------------------------------------------------------------------- #

def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # Quote anything YAML could reinterpret: a leading hash starts a comment,
    # a colon starts a mapping, and a bare 'yes' or '1.0' changes type.
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_key(key: str) -> str:
    text = str(key)
    if text and all(c.isalnum() or c in "-_" for c in text):
        return text
    return '"' + text.replace('"', '\\"') + '"'


def _yaml_inline_list(values: list[Any]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(_yaml_scalar(v) for v in values) + "]"


def _is_hex(value: str) -> bool:
    text = str(value).lstrip("#")
    return len(text) == 6 and all(c in "0123456789abcdefABCDEF" for c in text)


def _collapse(text: str) -> str:
    """One line, trimmed, for a trailing comment."""
    flat = " ".join(str(text).split())
    if len(flat) <= QUOTE_CHARS:
        return flat
    return flat[: QUOTE_CHARS - 3].rstrip() + "..."
