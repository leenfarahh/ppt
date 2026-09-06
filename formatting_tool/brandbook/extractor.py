"""Read a brand reference PDF and return the rules it actually states.

One call. The whole PDF goes in as a file part, so the model sees the pages as
pages: a palette swatch grid, a type-scale table and a logo clear-space diagram
are all visual, and text extraction alone would lose most of them.

The prompt does one job and refuses the adjacent one: report what the document
states, and do not derive rules from the example slides it shows. A brand book
almost always includes sample layouts, and reading a type size off a sample is
how an accident becomes a brand rule. Values read that way belong in `notes`
for a person to judge, or nowhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..ai.client import DEFAULT_MODEL, THINKING_BUDGETS
from ..ai.gemini import AIValidationError, build_client, count, file_part, generate_json
from ..ai.schema import to_gemini_schema
from ..extract import DeckReadError, read_deck
from ..models import BrandGuidelines, Provenance, guideline_paths
from .mapping import Rejection, guidelines_from_extraction
from .schema import BRANDBOOK_SCHEMA

log = logging.getLogger(__name__)

DEFAULT_EFFORT = "high"
DEFAULT_MAX_TOKENS = 16000
MAX_OUTPUT_CEILING = 65536

EXTRACTION_INSTRUCTIONS = """\
You are reading a brand guidelines document and recording the presentation
formatting rules it states, so that a validator can check decks against them.
The reader of your output is a senior designer who will review and correct it
before it is used.

Record only what the document states. This is the whole job, and the
distinction that matters most:

- A stated rule is one the document asserts, in words or in a labelled spec:
  "Headlines are set in Inter Tight, 32-40pt", a palette swatch labelled with
  its hex, a diagram annotating a margin as 12mm.
- An example is not a rule. Brand books show sample slides, mock layouts and
  application photographs. Do NOT measure or eyedrop a value off an example and
  report it as a specification. If a sample slide appears to use 28pt titles
  but the document never says so, the title size is not specified.
- If the document does not state a value, return null for it. A null is a
  correct and useful answer. An invented number is worse than no number,
  because the validator will report deck after deck against it.

Evidence is mandatory. For every value you record, quote the words or the
labelled spec from the document that state it, verbatim, and give the page.
A value whose quote does not actually state it will be discarded, so do not
supply a quote that merely mentions the topic.

Specific guidance:

- Roles are the text roles in a slide deck: title, subtitle, body, caption,
  footer. Map the document's own vocabulary onto them (headline to title,
  deck for standfirst or kicker to subtitle, and so on). If the document
  describes a role that has no equivalent, put it in notes.
- Colours: give the hex when the document gives one. Brand books often specify
  Pantone or CMYK only; in that case leave hex null and put the exact
  specification in spec_as_written. Do not convert between colour spaces.
- Measurements: always report the unit the document uses. Never convert. If a
  margin is given as a proportion ("one module", "half the logo height")
  rather than a length, leave the number null and state the rule in notes.
- Arabic typefaces go in arabic_fonts, never merged into latin_fonts. A Latin
  font does not cover Arabic, and the validator checks them separately.
- Notes are for rules that cannot be reduced to a number or a list:
  conditional rules, exceptions, rules about hierarchy, anything you had to
  leave null above. Notes are passed to the deck reviewer verbatim, so they
  earn their place. One sentence each.
- list every field the document does not specify in not_specified.
"""


@dataclass
class ExtractConfig:
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS
    api_key_env: str = "GEMINI_API_KEY"

    @property
    def thinking_budget(self) -> int:
        return THINKING_BUDGETS.get(self.effort, THINKING_BUDGETS[DEFAULT_EFFORT])

    @property
    def max_output_tokens(self) -> int:
        """Answer allowance plus thinking, since Gemini counts both."""
        return min(self.max_tokens + self.thinking_budget, MAX_OUTPUT_CEILING)


@dataclass
class ExtractionResult:
    """What one reference file yielded, and what was thrown away."""

    guidelines: BrandGuidelines
    source: str
    evidence: dict[str, str] = field(default_factory=dict)
    pages: dict[str, int] = field(default_factory=dict)
    rejections: list[Rejection] = field(default_factory=list)
    unspecified: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def authored(self) -> list[str]:
        return self.guidelines.paths_with(Provenance.AUTHORED)

    @property
    def missing(self) -> list[str]:
        return self.guidelines.paths_with(Provenance.MISSING)


class BrandBookError(RuntimeError):
    """Raised when a reference file cannot be read."""


def extract_guidelines(
    path: str | Path,
    config: Optional[ExtractConfig] = None,
    client: Any = None,
) -> ExtractionResult:
    """Extract guidelines from a brand reference, PDF or approved deck.

    The two sources answer different questions and are not interchangeable. A
    PDF brand book states rules, so what comes out of it is AUTHORED and
    carries a quote. An approved deck states nothing; it demonstrates. What
    comes out of it is INFERRED, and a reviewer has to promote each value
    before a report will speak about it in brand terms.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return extract_from_pdf(path, config, client)
    if suffix == ".pptx":
        return extract_from_deck(path)
    raise BrandBookError(
        f"{Path(path).name}: expected a .pdf brand book or a .pptx approved "
        "deck. Export a DOCX or Keynote brand document to PDF first, so that "
        "what is read is what a designer sees on the page."
    )


def extract_from_deck(path: str | Path) -> ExtractionResult:
    """Read an approved deck as a brand reference.

    No model call: everything worth having is already structured in the file.
    The theme carries the palette and the typefaces, and the slides carry the
    type sizes, the logo placement and the margins. Reading those is what
    `inference` already does for a master deck, so this returns the empty
    shell it fills.

    A deck is evidence of habit, never a stated rule, so every value it
    produces is marked MISSING here and INFERRED once inference has run. The
    report hedges findings that rest on them, deliberately.
    """
    path = Path(path)
    if not path.exists():
        raise BrandBookError(f"reference file not found: {path}")

    try:
        deck = read_deck(path)
    except DeckReadError as exc:
        raise BrandBookError(str(exc)) from exc

    if not deck.slides:
        log.warning(
            "%s has no slides, only layouts. The palette and typefaces come "
            "from the theme and will still be read, but type sizes, logo "
            "placement and margins are read from slides and will stay "
            "unspecified. Add one example slide per layout to get them.",
            path.name,
        )

    guidelines = BrandGuidelines(
        name=path.stem,
        source=path.name,
        # Nothing in a deck is stated, so every value starts unspecified and
        # inference decides which of them the file is consistent enough to
        # support.
        provenance={p: Provenance.MISSING.value for p in guideline_paths()},
    )

    log.info(
        "read %s: %d slide(s), %d layout(s), %d theme colour(s), %d theme font(s)",
        path.name,
        len(deck.slides),
        len(deck.layouts),
        len(deck.theme_colors),
        len(deck.theme_fonts),
    )
    return ExtractionResult(
        guidelines=guidelines,
        source=str(path),
        unspecified=guideline_paths(),
    )


def extract_from_pdf(
    path: str | Path,
    config: Optional[ExtractConfig] = None,
    client: Any = None,
) -> ExtractionResult:
    """Extract the stated rules from a brand reference PDF."""
    config = config or ExtractConfig()
    path = Path(path)

    if not path.exists():
        raise BrandBookError(f"reference file not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise BrandBookError(
            f"{path.name}: extract_from_pdf reads PDFs only; use "
            "extract_guidelines to accept an approved .pptx as well."
        )

    client = client or build_client(config.api_key_env)
    log.info("reading %s (%.1f MB)", path.name, path.stat().st_size / 1e6)

    try:
        document = file_part(client, path, "application/pdf")
    except AIValidationError:
        raise
    except Exception as exc:
        raise BrandBookError(f"could not attach {path.name}: {exc}") from exc

    data, response = generate_json(
        client,
        model=config.model,
        contents=[document, "Extract the stated formatting rules."],
        system_instruction=EXTRACTION_INSTRUCTIONS,
        schema=to_gemini_schema(BRANDBOOK_SCHEMA),
        translate_schema=False,
        thinking_budget=config.thinking_budget,
        max_output_tokens=config.max_output_tokens,
        api_key_env=config.api_key_env,
    )

    mapped = guidelines_from_extraction(data, source=path.name)
    usage = getattr(response, "usage_metadata", None)
    result = ExtractionResult(
        guidelines=mapped.guidelines,
        source=str(path),
        evidence=mapped.evidence,
        pages=mapped.pages,
        rejections=mapped.rejections,
        unspecified=mapped.unspecified,
        raw=data,
        input_tokens=count(usage, "prompt_token_count"),
        output_tokens=(
            count(usage, "candidates_token_count") + count(usage, "thoughts_token_count")
        ),
    )

    log.info(
        "%s: %d value(s) stated, %d not specified, %d reading(s) discarded",
        path.name,
        len(result.authored),
        len(result.missing),
        len(result.rejections),
    )
    for rejection in result.rejections:
        log.info("  discarded %s", rejection)
    return result
