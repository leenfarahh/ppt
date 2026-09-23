"""Ask the model what each shape on a slide IS, before the master goes on.

A third small per-slide call, beside `ai.layout` and made from the same
render. `ai.layout` answers one question about the slide as a whole -- which
layout it belongs on -- and this one answers the question the restyle then
runs into: of the boxes on this slide, which is the subtitle, which is the
source line under a chart, which shapes are the chart, and which are drawn
furniture that has to survive the move unchanged.

WHY THE RESTYLE NEEDS TO BE TOLD. Applying a master moves a slide's copy into
the new layout's regions, and the two things that decide that today are the
file's own placeholder tags and geometry. Both are silent about meaning, and
on a real consulting deck that silence costs three things a designer sees
immediately:

  - THE SOURCE LINE ENDS UP UNDER THE TITLE. "Source: Oxford Economics, Team
    analysis" is a full-width box, so it measures like a subtitle and gets
    filled into the subtitle region at the top of the page. Nothing in the
    file says it is a footnote; the words do, and only a reader can see that.
  - THE SUBTITLE IS GUESSED AT. The region that should hold the one line under
    the title is filled by whichever loose box happens to be drawn over it,
    which on a chart slide is the chart's own caption.
  - A DRAWN ELEMENT IS SWALLOWED. A chevron banner carrying "1. What is the
    new ambition?" is a shape with words in it, so its copy is claimed into a
    body region and the chevron itself is deleted with the box it came out of.
    The words survive and the design does not.

WHAT IT IS NOT ASKED. No coordinates and no corrections -- the same rule the
rest of this package runs on. It is shown a picture and a list of what is on
it, and it answers with one word per shape. Everything that follows from that
word is measured off the file.

Never fatal. No key, no renderer, a quota spent halfway down: the roles come
back empty and every caller falls back to exactly what it did before this
existed, which is the file's own reading.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..models import DeckProfile
from .designqa import Listed, build_shape_map
from .gemini import Exhausted, build_client, file_part, generate_json

log = logging.getLogger(__name__)

# The whole vocabulary. Closed, and whitelisted on the way back, for the
# reason every other enum in this package is: a word nobody defined here has
# to read as "unlabelled" rather than as a new behaviour in the restyle.
#
# `decoration` and `chart` are the two that earn their place by what they stop
# happening rather than by what they cause. Neither is ever filled into a
# region and neither is ever rearranged, which is the whole of what the
# restyle and the design check need to be told about them.
ROLES = (
    "title", "subtitle", "body", "source", "chart", "decoration", "other",
)

# Roles whose shapes must reach the output exactly as they arrived: not
# claimed into a layout region, not deleted with the copy they carried, not
# moved to line up with anything.
#
# `source` is here and `body` is not, and that is the distinction the whole
# module exists to draw. A body box SHOULD move into the region the master
# gives it -- that is what applying a master is. A footnote should not: the
# master has no region for it, so the only place a restyle can put it is a
# region meant for something else.
PROTECTED = frozenset({"source", "chart", "decoration"})

# Room for the answer. One word per shape plus the JSON around it is small,
# and the floor is here so that a sparse slide's budget is not spent on the
# thinking it also pays for. See `ai.designqa._answer_tokens`.
_ANSWER_TOKENS_FLOOR = 2048
_ANSWER_TOKENS_PER_SHAPE = 40
_ANSWER_TOKENS_CAP = 16384


def _answer_tokens(shapes: int) -> int:
    return min(
        _ANSWER_TOKENS_CAP,
        max(_ANSWER_TOKENS_FLOOR, shapes * _ANSWER_TOKENS_PER_SHAPE + 512),
    )


INSTRUCTIONS = """\
You are a presentation designer about to move a slide onto a different master.
You are shown the slide exactly as PowerPoint renders it, and a list of the
shapes on it with where each one sits on the page. Say what each shape IS.

You are not judging the slide and you are not correcting it. One word per
shape, and the word decides what happens to that shape when the master goes
on, so read the picture rather than the shape's name -- names in a real deck
are "Rectangle 47" and mean nothing.

  title       the slide's own headline, the line that says what the slide is
              about. At most one per slide.
  subtitle    the ONE line that sits under the title and qualifies it: a
              kicker, a standfirst, a "1 of 2", the sentence that completes
              the headline's thought. At most one per slide, and only when
              there really is one -- most slides have none, and naming a
              chart's caption or a column heading as the subtitle puts it at
              the top of the page under the title, which is worse than leaving
              it where it is.
  body        ordinary copy: a paragraph, a bullet list, a card's text, a
              column of prose, a label inside a diagram. This is the default
              for words on a slide.
  source      the small print that has to stay at the bottom: "Source: ...",
              "Note: ...", a footnote marker's text, a methodology line, a
              disclaimer, a page reference. Judge it by what it says and how
              it is set -- small type, at the foot of the page, under the
              thing it qualifies -- not by whether it starts with a
              particular word. These are never moved anywhere.
  chart       a data graphic and everything drawn inside it: the plot, its
              bars or lines, its axis labels, its value labels, its legend,
              its gridlines. A chart is often drawn as dozens of ordinary
              shapes rather than as one object, and every one of those shapes
              is `chart`. Say `chart` for the numbers over the bars and the
              country names under them as readily as for the bars.

              A DIAGRAM IS NOT A CHART. A process flow, a matrix, a pyramid, a
              set of cards, a timeline, an org chart of boxes and connectors:
              those are drawn arrangements, they are `body` or `decoration`,
              and they are allowed to be tidied. What makes something a chart
              is that its shapes ENCODE VALUES -- a bar is a certain height
              because a number is a certain size -- so moving one of them or
              resizing it would state a different number. If you are unsure
              whether something is a chart or a diagram, look for an axis, a
              scale, a legend keyed to series, or labels that are quantities.
  decoration  something drawn that carries the design rather than the content:
              a banner, a chevron or arrow shape, a coloured band behind a
              heading, a divider rule, a bracket, a panel, a numbered badge, a
              connector. A shape can be decoration AND have words on it -- a
              chevron with "1. What is the new ambition?" across it is
              decoration -- and that is the case this word exists for. The
              shape is part of the design and must survive the move as it is.
  other       a logo, a page number, a piece of furniture, or anything none of
              the above describes.

RULES THAT MATTER MORE THAN THE DEFINITIONS.

- WHEN IN DOUBT BETWEEN `body` AND ANYTHING PROTECTED, THE PROTECTED WORD IS
  SAFER. `body` is the only word that lets a shape be moved into a region and
  its box deleted, so a wrong `body` loses a drawn element and a wrong
  `decoration` leaves a box where the designer drew it. One of those can be
  seen and fixed; the other cannot.
- ONE `title` AND ONE `subtitle` AT MOST. If two boxes could be the subtitle,
  neither is: name the one that is unmistakably a standfirst under the title,
  or name none.
- A GROUP AND ITS CHILDREN GET THE SAME WORD unless they genuinely differ. The
  list indents a group's children under it.
- ANSWER FOR EVERY SHAPE IN THE LIST, using its ref, and for no other.
"""


@dataclass(frozen=True)
class ShapeRole:
    """One shape and the word the model gave it."""

    ref: str
    role: str
    name: str = ""
    shape_id: Optional[int] = None
    path: tuple[int, ...] = ()
    # Where it sits, in inches. Kept because a chart is usually drawn as many
    # shapes and the useful question downstream is about the REGION they
    # occupy: is this label inside the chart, whether or not the model
    # remembered to name it. See `SlideRoles.protects`.
    box: Optional[tuple[float, float, float, float]] = None
    # The shape's copy, normalised. THE ONLY ADDRESS THAT SURVIVES THE
    # RESTYLE. These roles are read off the deck as it arrived and the restyle
    # then hands the file to PowerPoint, which duplicates every slide and
    # renumbers any shape id it finds twice -- so an id read here need not be
    # the id on the file the next stage opens. The copy is not renumbered by
    # anybody, and a source line or a chevron's label is distinctive enough to
    # be an address. See `SlideRoles.texts_for`.
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "role": self.role, "name": self.name,
            "shape_id": self.shape_id, "path": list(self.path),
        }


# How much of a shape has to sit inside a chart's region before it is taken to
# be part of the chart, whatever the model called it. A label half in and half
# out is not inside anything; one almost wholly inside is.
_INSIDE_SHARE = 0.75


@dataclass(frozen=True)
class SlideRoles:
    """What one slide's shapes are, or the reason nothing was read."""

    slide: int
    shapes: tuple[ShapeRole, ...] = ()
    reviewed: bool = False
    reason: str = ""

    def role_of(self, shape_id: Optional[int]) -> str:
        if shape_id is None:
            return ""
        for entry in self.shapes:
            if entry.shape_id == shape_id:
                return entry.role
        return ""

    def ids_for(self, *roles: str) -> set[int]:
        wanted = set(roles)
        return {
            entry.shape_id for entry in self.shapes
            if entry.role in wanted and entry.shape_id is not None
        }

    def texts_for(self, *roles: str) -> set[str]:
        """The copy carried by the shapes in these roles, normalised.

        For the stages that run on a file PowerPoint has rewritten, where a
        shape id read off the original is no longer an address. See
        `ShapeRole.text`.
        """
        wanted = set(roles)
        return {
            entry.text for entry in self.shapes
            if entry.role in wanted and entry.text
        }

    @property
    def subtitle(self) -> Optional[ShapeRole]:
        """The one shape the model called the subtitle, or None.

        None as soon as there are two. The word is defined as "at most one",
        so two of them is the model disagreeing with itself about which box is
        the standfirst, and filling the region from either is a coin toss that
        puts a heading at the top of somebody's page.
        """
        found = [e for e in self.shapes if e.role == "subtitle"]
        return found[0] if len(found) == 1 else None

    @property
    def protected_texts(self) -> set[str]:
        """The copy that must not be claimed into a region, normalised."""
        return self.texts_for(*PROTECTED)

    @property
    def chart_regions(self) -> list[tuple[float, float, float, float]]:
        """The rectangles the charts on this slide occupy, in inches.

        One per chart-marked shape rather than one per chart. They are used
        only to ask whether another shape is inside a chart, and for that a
        list of the parts answers exactly as well as a merged outline while
        being impossible to get wrong.
        """
        return [e.box for e in self.shapes if e.role == "chart" and e.box]

    def protects(self, shape_id: Optional[int], box: Optional[Any] = None) -> bool:
        """Whether this shape must be left exactly where and as it is.

        TWO WAYS TO BE PROTECTED, and the second is what makes the first
        usable. A shape the model named with a protected word is protected.
        So is any shape sitting INSIDE a chart's region, whatever it was
        called or whether it was listed at all -- because a chart drawn as
        forty shapes will not come back as forty `chart` answers every time,
        and a chart with thirty-eight parts protected and two free to be
        levelled is a chart this tool is allowed to rewrite.
        """
        if self.role_of(shape_id) in PROTECTED:
            return True
        return self.inside_a_chart(box)

    def inside_a_chart(self, box: Optional[Any]) -> bool:
        """Whether a rectangle sits within one of this slide's charts."""
        rect = _rect(box)
        if rect is None:
            return False
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        if area <= 0:
            return False
        for chart in self.chart_regions:
            across = min(rect[2], chart[2]) - max(rect[0], chart[0])
            down = min(rect[3], chart[3]) - max(rect[1], chart[1])
            if across <= 0 or down <= 0:
                continue
            if (across * down) / area >= _INSIDE_SHARE:
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "slide": self.slide,
            "shapes": [entry.to_dict() for entry in self.shapes],
            "reviewed": self.reviewed,
            "reason": self.reason,
        }


def _rect(box: Any) -> Optional[tuple[float, float, float, float]]:
    """(left, top, right, bottom) in inches, from a Geometry or a 4-tuple."""
    if box is None:
        return None
    if isinstance(box, (tuple, list)) and len(box) == 4:
        left, top, right, bottom = (float(v) for v in box)
        return (left, top, right, bottom)
    try:
        return (
            float(box.left_in), float(box.top_in),
            float(box.left_in) + float(box.width_in),
            float(box.top_in) + float(box.height_in),
        )
    except Exception:
        return None


@dataclass
class RolesResult:
    """Every slide's roles, keyed by slide number, plus why any are missing."""

    slides: dict[int, SlideRoles] = field(default_factory=dict)
    reason: str = ""

    def __bool__(self) -> bool:
        return any(entry.reviewed for entry in self.slides.values())

    def for_slide(self, number: int) -> SlideRoles:
        return self.slides.get(number) or SlideRoles(slide=number)


def read_roles(
    deck: DeckProfile,
    images: Sequence[tuple[int, Path]],
    model: str,
    thinking_budget: int,
    api_key_env: str = "GEMINI_API_KEY",
    concurrency: int = 6,
) -> RolesResult:
    """One answer per rendered slide. Never raises.

    An empty result is the honest outcome of a run with no key, no renderer or
    no quota, and every caller treats it as "read the file instead", which is
    what they all did before this existed.
    """
    result = RolesResult()
    by_number = {slide.number: slide for slide in deck.slides}
    work: list[tuple[int, Path, str, dict[str, Listed]]] = []

    for number, path in images:
        slide = by_number.get(number)
        if slide is None:
            continue
        listing, refs = build_shape_map(slide, deck.width_in, deck.height_in)
        if not refs:
            continue
        work.append((number, path, listing, refs))

    if not work:
        result.reason = "nothing rendered to read roles off"
        return result

    try:
        client = build_client(api_key_env)
    except Exception as exc:
        log.warning("no role pass: %s", exc)
        result.reason = str(exc)
        return result

    # Set when the account turns out to be spent, so the remaining slides do
    # not each ask and each be refused. One message, not one per slide.
    spent: list[Exception] = []

    def one(number, path, listing, refs) -> SlideRoles:
        if spent:
            return SlideRoles(slide=number, reason=str(spent[0]))
        try:
            return _ask(client, number, path, listing, refs, model,
                        thinking_budget)
        except Exhausted as exc:
            if not spent:
                spent.append(exc)
                log.error("role pass stopped: %s", exc)
            return SlideRoles(slide=number, reason=str(exc))
        except Exception as exc:      # never fatal: the file decides instead
            log.warning("no roles for slide %d: %s", number, exc)
            return SlideRoles(slide=number, reason=str(exc))

    workers = max(1, min(concurrency, len(work)))
    if workers == 1:
        answers = [one(*item) for item in work]
    else:
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="roles"
        ) as pool:
            answers = list(pool.map(lambda item: one(*item), work))

    result.slides = {entry.slide: entry for entry in answers}
    read = sum(1 for entry in answers if entry.reviewed)
    if not read:
        reasons = [entry.reason for entry in answers if entry.reason]
        result.reason = reasons[0] if reasons else "the model returned nothing"
    log.info(
        "the model said what the shapes are on %d of %d rendered slide(s)",
        read, len(work),
    )
    return result


def _schema(refs: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "shapes": {
                "type": "array",
                "description": "One entry per shape in the list, no more.",
                "items": {
                    "type": "object",
                    "properties": {
                        "shape": {
                            "type": "string",
                            "enum": list(refs),
                            "description": "The ref of the shape, from the list.",
                        },
                        "role": {
                            "type": "string",
                            "enum": list(ROLES),
                            "description": "What this shape is.",
                        },
                    },
                    "required": ["shape", "role"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["shapes"],
        "additionalProperties": False,
    }


def _ask(
    client: Any,
    number: int,
    path: Path,
    listing: str,
    refs: dict[str, Listed],
    model: str,
    thinking_budget: int,
) -> SlideRoles:
    contents: list[Any] = [
        f"This is slide {number} as PowerPoint renders it.",
        file_part(client, path, "image/png"),
        "The shapes on it, with where each one sits on the page:\n" + listing,
        "Answer with one entry per shape above, using its ref.",
    ]
    data, _response = generate_json(
        client,
        model=model,
        contents=contents,
        system_instruction=INSTRUCTIONS,
        schema=_schema(list(refs)),
        thinking_budget=thinking_budget,
        max_output_tokens=_answer_tokens(len(refs)) + max(0, thinking_budget),
    )
    return roles_from_response(data, number, refs)


def roles_from_response(
    payload: dict[str, Any], number: int, refs: dict[str, Listed]
) -> SlideRoles:
    """The model's answer, dropping anything it cannot address.

    A ref that was never sent is the model having invented a shape, and the
    nearest real shape to an invented one is a different shape on somebody's
    client deck. A second answer for one ref is dropped for the same reason
    `ai.designqa` drops it: which of two contradicting words is meant is not
    knowable here.
    """
    seen: set[str] = set()
    out: list[ShapeRole] = []
    for raw in payload.get("shapes") or []:
        ref = str(raw.get("shape") or "").strip()
        listed = refs.get(ref)
        if listed is None or ref in seen:
            continue
        seen.add(ref)
        role = str(raw.get("role") or "").strip().lower()
        if role not in ROLES:
            role = "other"
        out.append(ShapeRole(
            ref=ref,
            role=role,
            name=listed.name,
            shape_id=listed.shape_id,
            path=listed.path,
            box=_rect(listed.shape.geometry),
            text=normalize_copy(listed.shape.text),
        ))
    return SlideRoles(slide=number, shapes=tuple(out), reviewed=True)


def normalize_copy(text: Optional[str]) -> str:
    """A shape's copy as an address: case-folded, whitespace collapsed.

    One function so that the side writing the address and the side reading it
    cannot drift apart. PowerPoint normalises nothing on its own, but a round
    trip through it does turn a soft return into a line feed and back, and two
    stages disagreeing about that is a protection that silently stops working.
    """
    return " ".join(str(text or "").split()).casefold()
