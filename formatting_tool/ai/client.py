#ai validation layer
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import Issue, LayoutChoice, MasterSpec
from .gemini import (
    AIValidationError,
    finish_reason as _finish_reason,
    build_client,
    count as _count,
    file_part,
    generate_json,
)
from .payload import build_reference_block, payload_to_text
from .schema import (
    AI_RESPONSE_SCHEMA,
    issues_from_response,
    layout_choices_from_response,
    to_gemini_schema,
)

__all__ = [
    "AIConfig",
    "AIResult",
    "AIValidationError",
    "AIValidator",
    "DEFAULT_EFFORT",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "THINKING_BUDGETS",
]

log = logging.getLogger(__name__)

# gemini-2.5-pro was retired for new keys and answers 404, which made every AI
# run on a fresh key fail outright.
DEFAULT_MODEL = "gemini-3.1-pro-preview"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_TOKENS = 16000

THINKING_BUDGETS = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 24576,
    "max": 32768,
}
MAX_OUTPUT_CEILING = 65536

REVIEWER_INSTRUCTIONS = """\
You are reviewing a PowerPoint deck for formatting consistency against a brand
system. You work for a presentation design consultancy; the reader of your
output is a senior designer who will act on it directly.

You receive three things:
1. A brand reference block (system prompt): the palette, approved typefaces,
   role type scale, logo rules, safe margins, and authored notes.
2. Rule findings: what a deterministic checker already proved from the file.
   Each carries a ref like "R7".
3. Slide data: shape names, roles, boxes in inches, text, and the distinct
   text formats in play, for one batch of slides.

Your job is the judgement the deterministic layer cannot make:

- You cannot remove a rule finding. Every one of them was proved from the file
  and every one reaches the report. If the context justifies a finding -- a
  deliberate accent colour on a section divider, a full-bleed image crossing
  the safe margin, a legal line set small by design -- say so by naming it in
  confirms_refs with a low confidence and an explanation in `suggestion`. The
  designer decides whether to act on it; you do not decide for them.
- ALWAYS fill confirms_refs when your finding is about a defect a rule finding
  already names, whether you agree with it, disagree with it, or are simply
  describing it in your own words. Before you write a finding, read the rule
  findings for this batch and ask whether any of them is about the same defect
  on the same shape. If one is, its ref goes in confirms_refs. If two are --
  "48pt and shrinks to fit, above the 22pt maximum" is one sentence about a
  size finding AND an autofit finding -- list both.

  This is not bookkeeping. A labelled restatement merges into the rule finding
  and inherits its correction; an unlabelled one becomes a second entry saying
  the same thing, which the designer sees twice and can act on once. An
  unlabelled restatement is a worse answer than no finding at all.

  Name the shape the rule finding names. If a rule finding is about a shape
  inside a group and you are describing the group, use the shape's name and
  say "in <group>" in the message, not the group's name in `shape`.
- Report inconsistencies the rules missed: visual hierarchy that inverts
  between slides, a section divider styled like a content slide, spacing that
  is technically legal and visibly uneven, mixed capitalisation or tone across
  parallel elements, an element that reads as pasted from another deck.
- Judge severity in context. A logo missing from the cover is a blocker. A
  caption 1pt off is not.

When a rendered image of a slide is attached:

- Use it for what only exists once rendered: text clipped by its box, type
  that actually collides as opposed to boxes that merely overlap, contrast
  and legibility over an image, a shape hidden behind another, a line that
  reads as crooked. This is the reason the image is there.
- Do not use it to measure. The numbers in the payload are exact and the
  image is not: a 0.06in miss against a grid line is real and invisible, a
  colour eyedropped off a JPEG is not the colour in the file. Never contradict
  a measurement with an impression of one.
- Set `basis` to "render" only for a claim you actually read off the image,
  and "geometry" for everything else. A slide with no image attached is always
  "geometry". This is the difference between an observation and a guess, and
  the designer is told which they are reading.

Rules of engagement:

- Every finding must name the slide and, where it applies, the shape, using the
  exact names given in the payload. Never invent a shape name.
- Anchor each finding in the evidence you were given. If you cannot point at a
  value in the payload, do not report it.
- You are not judging the writing, the argument, or the design concept. Only
  formatting consistency against the brand system.
- Set confidence honestly. Below 0.5 means a judgement call worth a designer's
  glance, not a defect.

Proposing a correction, in `fix`:

- Fill it only when ONE mechanical action would correct the finding and you can
  name its target exactly from the payload or the brand reference. Everything
  else is null, and null is the common case: most of what you report is a
  judgement, and a judgement has no `op`.
- The target is checked against the brand system before anything is applied. A
  colour that is not a palette entry, a typeface that is not approved, a size
  outside the role's range -- each is refused and the finding goes to a
  designer. Proposing one costs a fix rather than buying one, so give the
  palette entry, not the colour you would have picked.
- There is no op for moving or resizing anything, deliberately. You are told
  not to measure off the image, and geometry is what the deterministic layer
  proves from the file; a coordinate from you would be the guess this whole
  instruction exists to prevent.
- `shape` must be the exact name from the payload, and the shape must be the
  one your finding is about. A fix on the wrong shape is worse than no fix.
- You see one batch of slides at a time. Do not report a deck-level pattern you
  can only see part of; report what this batch shows.

Choosing a layout, in `layout_choices`, one entry per slide you were given a
picture of:

- Pick the layout from `master_layouts` that the slide belongs on. Judge by
  what the slide IS and how its content is arranged: a cover, a section
  divider, a table of contents, one column of copy, two columns, a comparison,
  a full-bleed image, a chart. Match that against the regions each layout
  offers.
- Copy the name exactly. A name that is not on the list is discarded, so the
  slide keeps whatever the deterministic matcher chose.
- This is why you were given the picture. The deterministic matcher counts
  content regions from the slide's own text boxes, and a messy slide has many
  loose ones, so it reads a table of contents as a four-region comparison. You
  can see that it is a list.
- Set a low confidence when the master offers nothing that fits, and say so in
  `why`. That is a fact about the master worth having; a confident guess is
  not.
"""


@dataclass
class AIConfig:
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
class AIResult:
    issues: list[Issue] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    # Which master layout each slide belongs on, read off its rendered image.
    # Not findings, so not in `issues`: an input to applying the master rather
    # than something a designer ticks.
    layout_choices: list[LayoutChoice] = field(default_factory=list)
    # What each batch actually sent and got back. The AI layer is the part of
    # this tool you cannot read the source of to find out why it said
    # something, so the exchange is kept rather than discarded.
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def summary(self) -> str:
        return " ".join(s for s in self.summaries if s).strip()

    def merge(self, other: "AIResult") -> None:
        self.issues.extend(other.issues)
        self.summaries.extend(other.summaries)
        self.layout_choices.extend(other.layout_choices)
        self.exchanges.extend(other.exchanges)
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


class AIValidator:

    def __init__(self, spec: MasterSpec, config: Optional[AIConfig] = None) -> None:
        self.config = config or AIConfig()
        self.spec = spec
        # Built once and reused verbatim. Rebuilding it per call would change
        # the bytes and cost every cache hit.
        self._reference_block = build_reference_block(spec)
        self._response_schema = to_gemini_schema(AI_RESPONSE_SCHEMA)
        self._client: Any = None

    # -- system prompt ------------------------------------------------------ #

    def system_instruction(self) -> str:

        return REVIEWER_INSTRUCTIONS + "\n\nBRAND REFERENCE\n" + self._reference_block

    # -- the call ----------------------------------------------------------- #

    def validate_batch(
        self,
        payload: dict[str, Any],
        deck_name: str,
        images: Optional[list[tuple[int, Any]]] = None,
    ) -> AIResult:
        client = self._ensure_client()

        data, response = generate_json(
            client,
            model=self.config.model,
            contents=self._contents(payload, client, images),
            system_instruction=self.system_instruction(),
            schema=self._response_schema,
            translate_schema=False,      # translated once in __init__
            thinking_budget=self.config.thinking_budget,
            max_output_tokens=self.config.max_output_tokens,
            api_key_env=self.config.api_key_env,
        )

        result = self._build_result(data, response, deck_name)
        result.exchanges = [
            {
                "batch": payload.get("batch", {}),
                "slides": payload.get("batch", {}).get("slides", []),
                "images_attached": [n for n, _ in (images or [])],
                "rule_findings_sent": len(payload.get("rule_findings", [])),
                "response": data,
                "finish_reason": _finish_reason(response),
            }
        ]
        self._log_usage(response, payload)
        return result

    def _contents(
        self,
        payload: dict[str, Any],
        client: Any,
        images: Optional[list[tuple[int, Any]]],
    ) -> Any:
        """The payload, then one labelled image per slide in this batch.

        Labelled, because a bare run of images leaves the model to infer which
        slide is which from the order, and a misattributed finding names the
        wrong slide in a designer's list.
        """
        text = payload_to_text(payload)
        if not images:
            return text

        parts: list[Any] = [text]
        for number, path in images:
            parts.append(f"Slide {number}, as PowerPoint renders it:")
            parts.append(file_part(client, path, "image/png"))
        log.debug("attaching %d rendered slide(s) to the batch", len(images))
        return parts

    # -- response handling -------------------------------------------------- #

    def _build_result(
        self, data: dict[str, Any], response: Any, deck_name: str
    ) -> AIResult:
        issues, summary = issues_from_response(data, deck_name)
        # Validated against the master's real layout names here, where the
        # spec is in reach. A pick naming a layout that does not exist is
        # dropped, and the slide keeps whatever the deterministic matcher
        # chose.
        picks = layout_choices_from_response(data, set(self.spec.layout_names))
        usage = getattr(response, "usage_metadata", None)
        cached = _count(usage, "cached_content_token_count")
        return AIResult(
            issues=issues,
            summaries=[summary] if summary else [],
            layout_choices=picks,
            calls=1,
            # prompt_token_count already includes the cached prefix, so it
            # comes back out here to keep the two fields disjoint.
            input_tokens=max(_count(usage, "prompt_token_count") - cached, 0),
            output_tokens=(
                _count(usage, "candidates_token_count")
                + _count(usage, "thoughts_token_count")
            ),
            cache_read_tokens=cached,
            # Implicit caching is written by the service and never billed as a
            # write, so there is nothing to report here.
            cache_write_tokens=0,
        )

    def _log_usage(self, response: Any, payload: dict[str, Any]) -> None:
        usage = getattr(response, "usage_metadata", None)
        cache_read = _count(usage, "cached_content_token_count")
        batch = payload.get("batch", {})
        log.info(
            "batch %s/%s: in=%s out=%s thinking=%s cache_read=%s",
            batch.get("index"),
            batch.get("of"),
            max(_count(usage, "prompt_token_count") - cache_read, 0),
            _count(usage, "candidates_token_count"),
            _count(usage, "thoughts_token_count"),
            cache_read,
        )
        if batch.get("index", 1) > 1 and cache_read == 0:
            log.warning(
                "no cache hit on batch %s; the system prefix is either below "
                "the model's minimum cacheable length or is changing between "
                "calls",
                batch.get("index"),
            )

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        self._client = build_client(self.config.api_key_env)
        return self._client
