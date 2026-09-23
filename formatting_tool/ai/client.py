#ai validation layer
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import ColorIntent, Issue, LayoutChoice, MasterSpec
from .claude import (
    AIValidationError,
    finish_reason as _finish_reason,
    build_client,
    DEFAULT_MODEL as _CLAUDE_MODEL,
    file_part,
    generate_json,
    to_claude_schema,
    usage_counts,
)
from .payload import build_reference_block, payload_to_text
from .schema import (
    AI_RESPONSE_SCHEMA,
    issues_from_response,
    color_intents_from_response,
    layout_choices_from_response,
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

# Claude only. `ai/gemini.py` is kept on disk and imported by nothing.
DEFAULT_MODEL = _CLAUDE_MODEL
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
- Take out what was never meant to ship: a message to whoever is making the
  deck rather than to whoever reads it. See "Production notes" below.
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
  formatting consistency against the brand system -- and production notes,
  which are the one thing you judge by what the words say rather than by how
  they are set. See "Production notes" below.
- Set confidence honestly. Below 0.5 means a judgement call worth a designer's
  glance, not a defect.

Production notes:

- A deck being worked on collects messages addressed to whoever is making it
  rather than to whoever will read it: "Design - can you redo the map and make
  the colour contrast stronger", "TBC with legal", "@Sara update these
  numbers", a coloured comment box parked in a corner. They are for the
  production process and they must not go to a client.
- Report each one with category "production_note" and `fix.op` "remove_note",
  naming the shape by its id. Quote the text verbatim in `found`, because
  removing something is the one act nobody can check by looking at the result.
- `remove_note` DELETES the shape. Its text is afterwards copied into a
  PowerPoint comment, so the words are recoverable, but the shape and its
  formatting are gone for good: a caption you call a note comes back as a
  comment nobody asked for, on a slide now missing a caption.
- Ask one question: is this addressed to the people making the deck, or to the
  people reading it? Only the first is a note. A caption that happens to be
  worded as an instruction is content: "Select a plot size to continue" on a
  slide about plot sizes is the deck talking to its audience. So is a
  disclaimer, a source line, a footnote, a legend and a page number.
- When it could be either, it is content. Leaving a note in costs a designer
  ten seconds; taking a caption out of a client deck is a defect nobody sees
  until the client does. Say so with a low confidence rather than removing it.
- Report ONLY a request addressed to whoever is building the deck. A red
  bubble asking for a redraw is one. A slide that merely looks unfinished is
  not, and neither is a heading you would have worded differently.

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
- `move` and `resize` are available, and are the one place you may reason from
  geometry to a target. Every box, the slide size and the safe margins are in
  the payload in inches and all of them are exact, so a position worked out
  from those is arithmetic on what you were given. It is still not licence to
  measure off the image: a target you cannot derive from the numbers is a
  guess, and `basis` is how you say which you did.
- A proposed box is checked against the safe margins before it is applied and
  refused if it falls outside them, then put through the same overlap and
  alignment checks a measured fix gets. Propose the position the payload
  supports, not the one that looks right.
- `shape_id` must be the `id` of the shape from the payload, always. Names
  repeat within a slide -- sixteen shapes called "Pentagon 7" is a real deck
  -- so a fix carrying only a name may land on the wrong one and be refused.
  Give the name as well, for a reader.
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

Judging whether colour means something, in `color_intent`, one entry per slide
you were given a picture of that has off-palette colours on it:

- The question is not whether a colour is on the palette. A rule already knows
  that and it is right. The question is WHY the colour is there, which a rule
  cannot see and you can.
- Answer `keep` only when the colours are doing work the palette cannot do and
  correcting them would destroy meaning rather than tidy it. What qualifies: a
  red/amber/green status column, where the red is the point; a diagram greyed
  out except for the part under discussion, so that reading across a row shows
  the scope change; a legend keyed to a chart; the real brand colours of
  companies being named; a heat map or any scale.
- Answer `change` for drift, which is most of what you will see: a colour
  pasted in from another deck, a near miss of a brand colour, an accent nobody
  chose, one heading a different blue from the three beside it. If you are
  weighing it up, the answer is `change`.
- Name the shapes in `shape_ids`. A slide can carry a real colour system AND a
  mistake, and holding the whole slide protects both.
- Be honest in `confidence`. A `keep` below 0.6 is discarded, which is the
  intended outcome: an uncertain keep leaves a real defect in a deck nobody
  will look at again, while an uncertain change is one click to undo.
- `keep` deletes nothing. The findings still reach the designer. What you are
  deciding is whether the tool corrects them without being asked.
- Set a low confidence when the master offers nothing that fits, and say so in
  `why`. That is a fact about the master worth having; a confident guess is
  not.
"""


@dataclass
class AIConfig:
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS
    api_key_env: str = "ANTHROPIC_API_KEY"

    @property
    def thinking_budget(self) -> int:
        return THINKING_BUDGETS.get(self.effort, THINKING_BUDGETS[DEFAULT_EFFORT])

    @property
    def max_output_tokens(self) -> int:
        """Answer allowance plus thinking, since `max_tokens` covers both."""
        return min(self.max_tokens + self.thinking_budget, MAX_OUTPUT_CEILING)


@dataclass
class AIResult:
    issues: list[Issue] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    # Which master layout each slide belongs on, read off its rendered image.
    # Not findings, so not in `issues`: an input to applying the master rather
    # than something a designer ticks.
    layout_choices: list[LayoutChoice] = field(default_factory=list)
    # Slides whose off-palette colours the model reads as deliberate. Holds
    # a correction back; never removes a finding. See ColorIntent.
    color_intents: list[ColorIntent] = field(default_factory=list)
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
        self.color_intents.extend(other.color_intents)
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
        self._response_schema = to_claude_schema(AI_RESPONSE_SCHEMA)
        self._client: Any = None

    # -- system prompt ------------------------------------------------------ #

    def system_instruction(self) -> str:

        return REVIEWER_INSTRUCTIONS + "\n\nBRAND REFERENCE\n" + self._reference_block

    def _cached_prefix(self, client: Any) -> Optional[str]:
        """Always None: there is no cache object to make.

        The system prefix is byte-identical on every call -- the reference
        block is built once in `__init__` for exactly that reason -- and Claude
        caches it by the marker `claude.generate_json` puts on the system
        prompt, so every batch after the first reads it from the cache.
        """
        return None

    def release(self) -> None:
        """Nothing to tidy up; kept so callers need not know that."""

    # -- the call ----------------------------------------------------------- #

    def validate_batch(
        self,
        payload: dict[str, Any],
        deck_name: str,
        images: Optional[list[tuple[int, Any]]] = None,
    ) -> AIResult:
        client = self._ensure_client()

        cached = self._cached_prefix(client)
        # `attached` is what actually went, which is not always what was
        # offered: a rendered PNG can be gone by the time its batch's turn
        # comes round. It decides the evidence label below, so it has to be
        # the pictures that were sent rather than the ones that were meant to
        # be -- see `_contents`.
        contents, attached = self._contents(payload, client, images)
        data, response = generate_json(
            client,
            model=self.config.model,
            contents=contents,
            # One or the other, never both: a request naming a cache carries
            # its system prefix already, and sending it again is the thing
            # this exists to stop.
            system_instruction=None if cached else self.system_instruction(),
            cached_content=cached,
            schema=self._response_schema,
            translate_schema=False,      # translated once in __init__
            thinking_budget=self.config.thinking_budget,
            max_output_tokens=self.config.max_output_tokens,
            api_key_env=self.config.api_key_env,
        )

        result = self._build_result(data, response, deck_name, rendered=attached)
        result.exchanges = [
            {
                "batch": payload.get("batch", {}),
                "slides": payload.get("batch", {}).get("slides", []),
                "images_attached": sorted(attached),
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
    ) -> tuple[Any, set[int]]:
        """The payload, then one labelled image per slide, and what went.

        Labelled, because a bare run of images leaves the model to infer which
        slide is which from the order, and a misattributed finding names the
        wrong slide in a designer's list.

        A PICTURE THAT IS NO LONGER THERE COSTS THE PICTURE, NOT THE RUN. The
        PNGs are exported to the system temporary directory and the batches
        are reviewed over several minutes, and Windows Storage Sense and the
        managed cleanup tools an IT department installs both delete from there
        on a schedule with no regard for a process that is using it -- see
        `render._explain`, where this is already the known hazard. So the file
        can be listed when the directory is collected and gone by the time its
        batch's turn comes round.

        It used to raise straight through the worker and out of the run: one
        missing PNG on slide 20 of 29 lost every deterministic finding and the
        sixteen batches that had already come back. Now that slide is reviewed
        on its geometry, which is what a batch with no render has always done,
        and the numbers returned are the pictures that really went so the
        evidence label stays honest -- a model saying it looked at a slide it
        was never shown is demoted in `pipeline._check_evidence`.
        """
        text = payload_to_text(payload)
        if not images:
            return text, set()

        parts: list[Any] = [text]
        attached: set[int] = set()
        missing: list[int] = []
        for number, path in images:
            try:
                part = file_part(client, path, "image/png")
            except OSError:
                # OSError and not Exception: a file that is gone is expected
                # here and is handled by carrying on. Anything else is not
                # understood, and swallowing it would hide a real fault behind
                # a slide that quietly lost its picture.
                missing.append(number)
                continue
            parts.append(f"Slide {number}, as PowerPoint renders it:")
            parts.append(part)
            attached.add(number)
        if missing:
            log.warning(
                "the rendered picture(s) for slide(s) %s are no longer on "
                "disk, so they are reviewed on their geometry alone. Files in "
                "the system temporary directory are removed on a schedule by "
                "Windows Storage Sense and by managed cleanup tools, whether "
                "or not something is using them.",
                ", ".join(str(n) for n in missing),
            )
        if not attached:
            return text, set()
        log.debug("attaching %d rendered slide(s) to the batch", len(attached))
        return parts, attached

    # -- response handling -------------------------------------------------- #

    def _build_result(
        self,
        data: dict[str, Any],
        response: Any,
        deck_name: str,
        rendered: Optional[set[int]] = None,
    ) -> AIResult:
        issues, summary = issues_from_response(data, deck_name)
        # Validated against the master's real layout names here, where the
        # spec is in reach. A pick naming a layout that does not exist is
        # dropped, and the slide keeps whatever the deterministic matcher
        # chose.
        picks = layout_choices_from_response(data, set(self.spec.layout_names))
        # Bounded to the slides this batch carried a picture of. A verdict
        # about a slide the model never saw is not a judgement, and acting
        # on it would protect a defect nobody reviewed.
        intents = color_intents_from_response(data, rendered)
        usage = usage_counts(response)
        return AIResult(
            issues=issues,
            summaries=[summary] if summary else [],
            layout_choices=picks,
            color_intents=intents,
            calls=1,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cache_read_tokens=usage["cache_read"],
            cache_write_tokens=usage["cache_write"],
        )

    def _log_usage(self, response: Any, payload: dict[str, Any]) -> None:
        usage = usage_counts(response)
        cache_read = usage["cache_read"]
        batch = payload.get("batch", {})
        log.info(
            "batch %s/%s: in=%s out=%s cache_read=%s cache_write=%s",
            batch.get("index"),
            batch.get("of"),
            usage["input"],
            usage["output"],
            cache_read,
            usage["cache_write"],
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
