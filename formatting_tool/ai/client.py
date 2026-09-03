#ai validation layer
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import Issue, MasterSpec
from .gemini import AIValidationError, build_client, count as _count, generate_json
from .payload import build_reference_block, payload_to_text
from .schema import AI_RESPONSE_SCHEMA, issues_from_response, to_gemini_schema

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

DEFAULT_MODEL = "gemini-2.5-pro"
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

- Confirm or dismiss the rule findings. A finding is a false positive when the
  context justifies it (a deliberate accent colour on a section divider, a
  full-bleed image crossing the safe margin, a legal line set small by design).
  Dismiss it with a reason and do not restate it as an issue.
- Report inconsistencies the rules missed: visual hierarchy that inverts
  between slides, a section divider styled like a content slide, spacing that
  is technically legal and visibly uneven, mixed capitalisation or tone across
  parallel elements, an element that reads as pasted from another deck.
- Judge severity in context. A logo missing from the cover is a blocker. A
  caption 1pt off is not.

Rules of engagement:

- Every finding must name the slide and, where it applies, the shape, using the
  exact names given in the payload. Never invent a shape name.
- Anchor each finding in the evidence you were given. If you cannot point at a
  value in the payload, do not report it.
- You are not judging the writing, the argument, or the design concept. Only
  formatting consistency against the brand system.
- Set confidence honestly. Below 0.5 means a judgement call worth a designer's
  glance, not a defect.
- You see one batch of slides at a time. Do not report a deck-level pattern you
  can only see part of; report what this batch shows.
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
    dismissals: dict[str, str] = field(default_factory=dict)
    summaries: list[str] = field(default_factory=list)
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
        self.dismissals.update(other.dismissals)
        self.summaries.extend(other.summaries)
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

    def validate_batch(self, payload: dict[str, Any], deck_name: str) -> AIResult:
        client = self._ensure_client()

        data, response = generate_json(
            client,
            model=self.config.model,
            contents=payload_to_text(payload),
            system_instruction=self.system_instruction(),
            schema=self._response_schema,
            translate_schema=False,      # translated once in __init__
            thinking_budget=self.config.thinking_budget,
            max_output_tokens=self.config.max_output_tokens,
            api_key_env=self.config.api_key_env,
        )

        result = self._build_result(data, response, deck_name)
        self._log_usage(response, payload)
        return result

    # -- response handling -------------------------------------------------- #

    def _build_result(
        self, data: dict[str, Any], response: Any, deck_name: str
    ) -> AIResult:
        issues, dismissals, summary = issues_from_response(data, deck_name)
        usage = getattr(response, "usage_metadata", None)
        cached = _count(usage, "cached_content_token_count")
        return AIResult(
            issues=issues,
            dismissals=dismissals,
            summaries=[summary] if summary else [],
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
