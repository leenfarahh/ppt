"""Ask the model what a slide LOOKS like, one slide at a time, from its render.

A different question from the one `ai.client` asks. That layer is handed the
deck as numbers, with a render attached, and reports findings against a brand
system: this colour is not on the palette, this box is 0.06in off the grid.
This one is handed a picture and a list of what is on it, and asks the single
question no rule and no measurement can answer -- does this slide look right
to somebody who is about to send it to a client.

WHAT THE MODEL SEES. The true PowerPoint render of the slide, exported through
COM at the size `DESIGN_QA_SIZE` names, and a shape map: only the shapes that
are actually on the slide, each named, given a role, and placed in
slide-relative percentages. The percentages are what let a verdict be anchored
to something nameable -- without them the model can describe a defect and not
say which shape it is on, which is a note nobody can act on.

A REF, NOT A NAME, IS WHAT COMES BACK. Shape names are not unique and in a real
deck are wildly not unique: sixteen shapes called "Pentagon 7" on one slide is
a thing that happens. So each listed shape carries a ref of its own, the ref is
the only value the schema will accept in `shape`, and a verdict maps back to
exactly one shape id. The model cannot name a shape that is not on the list,
because the schema does not have a word for one.

TWO BUCKETS COME BACK, AND BOTH ARE KEPT.

1. A verdict per listed shape, whose `action` is `shrink`, `grow` or `none`.
   That vocabulary is deliberately tiny: it is everything `apply.qafix` can do
   on its own, bounded, to a copy of the deck. Anything outside it is not an
   instruction.
2. `slide_issues`: whatever the vocabulary cannot express -- a timeline with an
   unused stop, an empty column, a layout lopsided on the page. These are notes
   for a designer, never actions.

Dropping the second bucket is how a bad slide gets through. A model that has
just said the slide is lopsided and been ignored has still said it, and the
page shows it.

Never fatal. A slide the model declines, a key that is not there, a quota that
is spent: each comes back as a `SlideReview` carrying the reason, and the rest
of the deck is still reviewed.
"""

from __future__ import annotations

import logging
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from shutil import rmtree
from typing import Any, Optional, Sequence

from ..models import MARGIN_CHROME, DeckProfile, ShapeProfile, SlideProfile
from .gemini import Exhausted, build_client, file_part, generate_json

log = logging.getLogger(__name__)

# The whole action vocabulary. Everything in it is something `apply.qafix` can
# do to a copy of the deck without a designer present; nothing else is.
#
# IT GREW BECAUSE "none" WAS DOING TOO MUCH WORK. An icon sitting low in its
# circle and a heading whose words break in half are both mechanical -- centre
# the child on its parent, widen the box until the break goes -- and both were
# coming back as `none` with a sentence, which put them in the pile a designer
# works through by hand. The rule for what belongs here is unchanged: the
# correction has to be arithmetic, bounded, and checkable afterwards. Judgement
# stays out, and what stays out becomes a task instead (see `task` below).
ACTIONS = ("shrink", "grow", "center", "widen", "none")

# What kind of defect a shape has. Whitelisted on the way back rather than
# trusted: `issue` describes a finding and `action` is what gets applied, so a
# word nobody defined here should read as "unlabelled", not as a new behaviour.
#
# `off_center` and `unfilled` were added because the first version could see
# neither. An icon sitting low inside its circle is the commonest defect in a
# deck built from repeated components, and a placeholder still showing "Click
# to add text" prints exactly as it looks -- and neither is an overlap, a
# clipping or a size, so the model had no word for them and said nothing.
ISSUES = (
    "overlap", "cut_off", "off_center", "too_small", "too_big", "crowded",
    "unfilled",
)

# Room for a verdict per shape, on top of whatever thinking was asked for.
# Gemini counts thinking against `max_output_tokens`, so the two budgets share
# one allowance and a flat number here would be the answer being squeezed out
# by the reasoning -- which arrives as a truncated string and no verdicts at
# all, for the slide that had the most to say. See `ai.layout` for the same
# trap with a smaller answer.
_ANSWER_TOKENS = 4096

# Shapes listed on one slide, groups and their contents together. A slide with
# more than this on it is a slide whose problem is not which of its boxes is
# 2pt too big, and a shape map that long buries the ones worth reading. The
# rest are still in the picture, so `slide_issues` can still speak about them.
_MAX_SHAPES = 60

# How far into a group the list goes, and how much of one.
#
# IT GOES IN AT ALL because that is where the defect usually is. A row of
# circles each holding an icon is one group per circle, and the icon sitting
# low inside its circle -- the single commonest fault in a deck built from
# repeated components -- is invisible to a list that names the group and stops.
# The first version of this listed top-level shapes only, reasoning that a
# group is a composition somebody made on purpose; that is true of the
# arrangement and not true of its parts.
#
# It stops at two levels and twelve children because an org chart of forty
# boxes is an arrangement rather than forty decisions, and listing it would
# spend the whole map on one drawing. `deck_reader` composes every child into
# slide coordinates, so a child's percentages mean the same thing as a
# top-level shape's -- see `_Frame` there for why that is not free.
_MAX_DEPTH = 2
_MAX_IN_GROUP = 12

# The smallest share of the slide a group's child earns a line for. A hairline
# rule and a 4px dot inside a component are decoration, and a map that lists
# them reads as noise around the one shape that matters.
_MIN_CHILD_SHARE = 0.0002

# How much of a shape's copy goes in the map. Enough to tell two boxes apart
# in the picture, not so much that the map becomes the slide's text.
_EXCERPT_CHARS = 60

INSTRUCTIONS = """\
You are a presentation designer doing the last look before a deck goes to a
client. You are shown one slide exactly as PowerPoint renders it, and a list of
the shapes on it with where each one sits on the page.

THE LIST IS A TREE. A shape inside a group is listed under it, indented, with
its parent's ref and a suffix: `s5` is the circle, `s5.1` and `s5.2` are what
is inside it. Every percentage is of the whole slide, at every level, so a
child and its parent can be compared directly -- which is how you tell that an
icon is sitting low or left inside the circle that holds it. Give the verdict
on the CHILD when it is the child that is wrong.

Work in two steps, in this order.

STEP 1 -- A VERDICT FOR EVERY LISTED SHAPE. Answer once per shape in the list,
using its ref. Look at the shape in the picture, at the position the list gives
it, and say whether it is right.

  status   `ok` when nothing is wrong with it, `issue` when something is.
  issue    what kind of defect it is, when there is one:
             overlap    it runs into another shape, or is run into
             cut_off    its text is clipped by its box, or a word is broken
                        across two lines because the box is too narrow for it
                        ("Proportionalit / y"). Both mean the box cannot show
                        the words it was given.
             off_center it is not centred in the thing that holds it: an icon
                        low or left inside its circle, a number off-centre in
                        its badge, a label not centred in its band. Compare
                        the child's percentages with its parent's -- centred
                        means the gaps on the two sides are equal.
             too_small  it is unreadable, or lost beside its neighbours
             too_big    it dominates the slide, or crowds what is beside it
             crowded    it has no room to breathe where it sits
             unfilled   a placeholder the design expects content in that has
                        none. The list marks these EMPTY, and you will see
                        nothing at all where they are: PowerPoint does not
                        export the "Click to add text" prompt, so an unfilled
                        region renders as blank space rather than as a mistake.
                        Trust the mark over the picture for this one. A layout
                        holding a region the deck never filled is a defect the
                        designer has to resolve, not a blank on purpose.
           `none` when status is `ok`.
  action   the ONE mechanical correction, which can only be:
             shrink    step this shape's type down one notch
             grow      step this shape's type up one notch
             center    move this shape so it sits centred in the thing that
                       holds it. Only for a shape listed inside another -- the
                       icon in its circle -- and only when being off-centre IS
                       the defect.
             widen     make this text box wider until its words stop breaking
                       in half. For `cut_off` caused by a box too narrow for
                       the copy, which is the usual cause.
             none      nothing mechanical would fix it
           Choose the action that fixes the defect you named, not the one that
           hides it. Type that collides with its neighbour is not fixed by
           making the type smaller; a caption that is unreadable is not fixed
           by widening its box.
  note     one sentence, in a designer's words, saying what you saw.
  task     one sentence saying what to DO about it, as an instruction to a
           designer: "widen the label column so the headings stop breaking
           mid-word", "move the timeline up and let the cards run full width".
           Write one for every `issue`, whatever the action is -- including
           `none`, where it is the only thing anyone can act on. Leave it empty
           only for an `ok` shape.

STEP 2 -- WHAT IS WRONG WITH THE SLIDE. Anything the per-shape vocabulary
cannot express goes in `slide_issues`: a timeline with a stop nothing uses, a
column left empty, a layout weighted to one side, a hierarchy that reads in the
wrong order, a slide carrying more than it can hold.

Each one is a pair. `note` says what is wrong, in a sentence. `task` says what
to do about it, as an instruction somebody can pick up and act on -- these are
the findings nothing here can correct, so the instruction is the entire value
of reporting them. "The right half is empty" is an observation; "run the cards
across the full width, or move the callout into the empty half" is a task.

WHAT NOT TO DO.

- Do not measure. You are looking at a picture: "0.06in off the grid" is not
  something anyone can see and is not your question. Rules already do that.
- Do not report a defect you cannot see in this render. Two boxes whose
  rectangles overlap is not a collision; type touching type is.
- Do not invent a shape. Every verdict names a ref from the list.
- Do not ask for a font step to fix a spacing problem. Making the type smaller
  so it stops colliding is how a deck ends up at nine sizes; say `none` and
  describe the collision.
- Do not pad. A clean slide is every shape `ok` and an empty `slide_issues`,
  and that is a good answer.
- Do not write a task nobody can act on. "Improve the visual hierarchy" is not
  a task; "set the three headings at 16pt and the body at 11pt" is.
- Do not report the same defect twice, once on a group and once on its child.
  Name the shape that is actually wrong: the icon that sits low, not the
  circle it is in.
"""


# The kinds of mismatch the deck-level pass may report. A vocabulary again,
# and for the same reason: a page that groups these has to know what it is
# grouping, and a kind nobody defined reads as "other" rather than as a new
# category nothing renders.
DECK_KINDS = (
    "position", "type_scale", "spacing", "color", "size", "alignment",
    "content", "other",
)

CONSISTENCY_INSTRUCTIONS = """\
You are a presentation designer checking that a deck holds together. You are
shown its slides as PowerPoint renders them, tiled onto one or more sheets and
numbered, and a table of what each slide measures.

Report only DIFFERENCES BETWEEN SLIDES that a reader would notice: the thing
that makes a deck look assembled rather than designed. Each one names the
slides it is about.

  position    the same element sits somewhere else -- a title 4% lower on one
              slide, a footer that moves, a logo that drifts
  type_scale  the same kind of text is set at different sizes: body copy at
              11pt on four slides and 9pt on the fifth, two headings at two
              sizes
  spacing     different margins, gutters or column widths for the same kind of
              content; one slide tight to the edge where the rest breathe
  size        a repeated element drawn at a different size -- icons, badges,
              cards, image frames
  alignment   content starting on different grid lines from slide to slide
  color       the same role in a different colour: a heading, a rule, a fill
  content     a slide missing something its peers have, or carrying something
              they do not -- a heading with no body, an empty column, a
              leftover prompt
  other       a real inconsistency none of the above names

WHAT NOT TO REPORT.

- Slides that are DIFFERENT ON PURPOSE. A cover, a section divider and a
  closing slide are not the content slides and are not meant to match them.
  A full-bleed image slide is not a text slide with a wrong margin.
- Anything you can see on one slide alone. A crooked icon on slide 4 is that
  slide's problem and is already being reported; you are here for the pair of
  slides that disagree.
- Differences too small to see. You are comparing pictures; a 1% difference in
  a margin is not something a reader notices, and saying so wastes the list.
- Guesses about what is not in the picture.

Each one is a pair. `note` says which slides differ and how, naming the one
that looks like the odd one out. `task` says what to do about it, as an
instruction: "move the title on slide 7 up to where it sits on the rest", "set
the body copy on slide 12 to 11pt to match". An empty list is a real answer and
a good one.
"""


# --------------------------------------------------------------------------- #
# What comes back
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ShapeVerdict:
    """One shape, as the model read it off the render."""

    slide: int
    ref: str
    shape: str                    # the name the deck gives it
    shape_id: Optional[int]
    role: str
    status: str                   # ok | issue
    issue: str                    # one of ISSUES, or "" for none
    action: str                   # one of ACTIONS
    note: str                     # what is wrong
    # What to do about it, as an instruction. Every issue has one, including
    # the ones nothing here can apply -- for those it is the whole of what the
    # finding is worth, and leaving them as observations is what made the page
    # a list of complaints rather than a list of work.
    task: str = ""
    # The shape this one sits inside, where it sits inside anything. Carried
    # because `center` is meaningless without it: centred is a relationship
    # between a child and its parent, and the applier has to be told which
    # parent rather than guess from overlap.
    parent: str = ""
    parent_id: Optional[int] = None
    # Where the shape and its parent sit in the shape tree. See `Listed`: the
    # id is what a verdict is addressed by and the path is what survives a
    # deck whose ids PowerPoint has renumbered.
    path: tuple[int, ...] = ()
    parent_path: tuple[int, ...] = ()
    # Where the shape sits, as fractions of the slide: left, top, width,
    # height. Carried so the page can draw the verdict on the render rather
    # than beside it. It is the same rectangle the model was given, which is
    # what makes the box on the picture and the sentence under it the same
    # claim -- a page deriving its own would be marking a different shape.
    box: Optional[tuple[float, float, float, float]] = None

    @property
    def executable(self) -> bool:
        """Whether `apply.qafix` can act on this one.

        Three conditions, separate on purpose. An action outside the vocabulary
        is the model reaching past what it was given. A verdict with no shape
        id cannot be applied to a particular shape, and applying it by name
        would edit whichever "Pentagon 7" came first. And `center` without a
        parent is not an instruction at all -- centred on what? -- so it
        becomes a task rather than a step.
        """
        if self.shape_id is None or self.action not in ACTIONS or self.action == "none":
            return False
        return self.parent_id is not None if self.action == "center" else True

    def to_dict(self) -> dict[str, Any]:
        return {
            "slide": self.slide,
            "ref": self.ref,
            "shape": self.shape,
            "shape_id": self.shape_id,
            "role": self.role,
            "status": self.status,
            "issue": self.issue,
            "action": self.action,
            "note": self.note,
            "task": self.task,
            "parent": self.parent,
            "parent_id": self.parent_id,
            "executable": self.executable,
            "box": list(self.box) if self.box else None,
            "path": list(self.path),
        }


@dataclass
class SlideReview:
    """One slide's two buckets, or the reason there are none."""

    slide: int
    verdicts: list[ShapeVerdict] = field(default_factory=list)
    slide_issues: list[SlideIssue] = field(default_factory=list)
    reviewed: bool = False
    reason: str = ""

    @property
    def issues(self) -> list[ShapeVerdict]:
        return [v for v in self.verdicts if v.status == "issue"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "slide": self.slide,
            "shapes": [v.to_dict() for v in self.verdicts],
            "slide_issues": [issue.to_dict() for issue in self.slide_issues],
            "reviewed": self.reviewed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Listed:
    """A shape on the map: what it is, and where to find it again.

    THE PATH EXISTS BECAUSE THE ID DOES NOT SURVIVE THE ROUND TRIP. A shape id
    is unique within a slide in a well-formed file, and this tool reads it with
    python-pptx and applies corrections through PowerPoint -- two readings of
    the same file that only agree while the file is well formed. A deck whose
    shapes were pasted in from another deck can carry the same id twice, and
    PowerPoint renumbers the duplicates silently when it opens the file. On one
    such deck the group holding an icon and a text box two shapes away had both
    been given 910 by the writer; the correction landed on the text box, which
    is exactly the class of mistake this tool must never make.

    The path is the position in the shape tree, 1-based, outermost first:
    `(7,)` is the seventh shape on the slide, `(7, 2)` the second thing inside
    it. Document order is the one thing both readings do agree on.
    """

    shape: ShapeProfile
    path: tuple[int, ...]

    @property
    def name(self) -> str:
        return self.shape.name

    @property
    def shape_id(self) -> Optional[int]:
        return self.shape.shape_id


@dataclass(frozen=True)
class SlideIssue:
    """Something wrong with the slide as a whole, and what to do about it."""

    note: str
    task: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"note": self.note, "task": self.task}


@dataclass(frozen=True)
class DeckIssue:
    """One way two or more slides disagree with each other."""

    kind: str                     # one of DECK_KINDS
    slides: list[int]
    note: str
    task: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "slides": list(self.slides),
            "note": self.note, "task": self.task,
        }


# --------------------------------------------------------------------------- #
# The shape map
# --------------------------------------------------------------------------- #

def build_shape_map(
    slide: SlideProfile, width_in: float, height_in: float
) -> tuple[str, dict[str, Listed]]:
    """The shapes on one slide, in slide-relative percentages, with refs.

    Percentages rather than inches because the question is where on the page a
    thing sits, and because a 4:3 deck and a 16:9 one then read the same way.
    The model is looking at a picture; the only frame of reference it shares
    with this list is the edge of the slide.

    A TREE, NOT A LIST. A group is listed, and then so are the shapes inside
    it, indented under it and carrying its ref with a suffix -- `s5`, then
    `s5.1`, `s5.2`. What that buys is the verdict that could not be given
    before: an icon is not centred INSIDE the circle that holds it, which is a
    sentence about the child and about its parent at once, and neither half of
    it can be said if only one of the two has a ref.

    Every percentage is of the slide, children included. `deck_reader`
    composes a group's children into slide coordinates already; a child's own
    numbers are in a private coordinate system the group declares, and listing
    those would put every icon somewhere it is not.
    """
    lines: list[str] = []
    refs: dict[str, Listed] = {}
    if width_in <= 0 or height_in <= 0:
        return "", refs

    def place(
        shape: ShapeProfile,
        ref: str,
        depth: int,
        path: tuple[int, ...],
        parent: str = "",
    ) -> None:
        refs[ref] = Listed(shape=shape, path=path)
        box = shape.geometry
        inside = f" inside {parent!r}" if parent else ""
        lines.append(
            f"{'  ' * depth}{ref}. {shape.name!r} ({_role_of(shape)}){inside}: "
            # One decimal, which is finer than it looks necessary. Centring is
            # judged by comparing a child's gaps with its parent's edges, and
            # an icon 1.5% of the slide out of place -- 23px on the render, an
            # obvious wobble to the eye -- rounds to the same whole number as
            # a centred one. Whole percentages made the numbers agree with
            # each other while the picture disagreed with both.
            f"left {box.left_in / width_in * 100:.1f}%, "
            f"top {box.top_in / height_in * 100:.1f}%, "
            f"width {box.width_in / width_in * 100:.1f}%, "
            f"height {box.height_in / height_in * 100:.1f}%"
            f"{_type_note(shape)}{_excerpt(shape)}"
        )
        if depth + 1 >= _MAX_DEPTH:
            return
        listed = 0
        # Enumerated over ALL the children rather than over the ones worth
        # listing, because the path has to address the shape tree as
        # PowerPoint will walk it, not as this map prints it.
        worth = {id(child) for child in _worth_listing(shape, width_in, height_in)}
        for position, child in enumerate(shape.children, 1):
            if id(child) not in worth:
                continue
            if len(refs) >= _MAX_SHAPES:
                return
            listed += 1
            place(child, f"{ref}.{listed}", depth + 1, path + (position,),
                  shape.name)

    top = 0
    for position, shape in enumerate(slide.shapes, 1):
        if len(refs) >= _MAX_SHAPES:
            log.debug(
                "slide %d has more than %d shapes; the rest are not listed",
                slide.number, _MAX_SHAPES,
            )
            break
        box = shape.geometry
        if box is None or box.width_in <= 0 or box.height_in <= 0:
            continue
        # Footers, slide numbers and dates are the master's furniture. They
        # are on every slide, they are nobody's design decision here, and a
        # verdict on one is a verdict on the template.
        if shape.placeholder_token in MARGIN_CHROME:
            continue
        top += 1
        place(shape, f"s{top}", 0, (position,))

    return "\n".join(lines), refs


def _worth_listing(
    shape: ShapeProfile, width_in: float, height_in: float
) -> list[ShapeProfile]:
    """The children of a group that earn a line of the map.

    Empty for anything that is not a group, for one too crowded to be a set of
    decisions, and for the specks inside one. A group whose children are not
    listed is still listed itself, so the model can still say the whole thing
    sits wrong; what it loses is the ability to say which part of it does.
    """
    children = [
        child for child in shape.children
        if child.geometry is not None
        and child.geometry.width_in > 0
        and child.geometry.height_in > 0
        and (child.geometry.width_in * child.geometry.height_in)
        / (width_in * height_in) >= _MIN_CHILD_SHARE
    ]
    if not children or len(children) > _MAX_IN_GROUP:
        return []
    return children


def _role_of(shape: ShapeProfile) -> str:
    """What kind of thing this is, in the words a designer would use."""
    # Before `is_picture`, because most icons in a real deck ARE pictures --
    # an SVG dropped in from an icon set -- and "image" invites a verdict
    # about cropping where "icon" invites the one that matters, which is
    # whether it sits centred in the thing holding it.
    if shape.graphic_colors:
        return "icon"
    if shape.is_picture:
        return "image"
    if shape.table is not None:
        return "table"
    if shape.is_group:
        return "group"
    role = getattr(shape.role, "value", str(shape.role))
    if role and role != "unknown":
        return role
    token = shape.placeholder_token
    if token:
        return token.lower()
    return "shape" if not shape.text.strip() else "text"


def _type_note(shape: ShapeProfile) -> str:
    """The type sizes actually set on the shape, where the file states them.

    Often it states none: a placeholder inherits its size from the layout, and
    nothing in the .pptx says what that resolves to. Silence is the honest
    answer -- the applier reads the effective size from PowerPoint itself, and
    a guess here would only mislead the model about how much room it has.
    """
    sizes = sorted({
        run.size_pt
        for paragraph in shape.paragraphs
        for run in paragraph.runs
        if run.size_pt and run.text.strip()
    })
    if not sizes:
        return ""
    if len(sizes) == 1:
        return f", {sizes[0]:g}pt"
    return f", {sizes[0]:g}-{sizes[-1]:g}pt"


def _excerpt(shape: ShapeProfile) -> str:
    """The shape's copy, or the fact that it has none.

    THE EMPTY PLACEHOLDER IS WHY THIS SAYS ANYTHING AT ALL about a shape with
    no text. PowerPoint does not export the "Click to add text" prompt, so an
    unfilled placeholder renders as nothing whatever and the model looking at
    the picture sees an empty region with no way to know the design expected
    content in it. The file knows. It is marked here, and the model is told to
    read the mark.
    """
    text = re.sub(r"\s+", " ", shape.text).strip()
    if not text:
        return ", EMPTY" if shape.placeholder_type else ""
    if len(text) > _EXCERPT_CHARS:
        text = text[: _EXCERPT_CHARS - 1].rstrip() + "…"
    return f", {text!r}"


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #

def _schema(refs: Sequence[str]) -> dict[str, Any]:
    """The response contract, built per slide so `shape` can be an enum.

    The refs of THIS slide are the only values the field accepts, which is what
    makes "the model cannot steer anything outside its sandbox" structural
    rather than a rule enforced afterwards.

    `issue` carries "none" rather than being nullable. A nullable enum fights
    the structured-output dialect -- `nullable` and `enum` on one field is read
    as an enum that may be null, and what comes back is an empty string that is
    not in the enum -- so the absence is a member of the vocabulary and is
    whitelisted back to "" on the way in.
    """
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
                        "status": {"type": "string", "enum": ["ok", "issue"]},
                        "issue": {
                            "type": "string",
                            "enum": [*ISSUES, "none"],
                            "description": "'none' when the shape is ok.",
                        },
                        "action": {
                            "type": "string",
                            "enum": list(ACTIONS),
                            "description": (
                                "The one mechanical correction, or 'none'. "
                                "'none' is the ordinary answer."
                            ),
                        },
                        "note": {
                            "type": "string",
                            "description": (
                                "One sentence saying what you saw. Empty is "
                                "allowed only on an 'ok' shape."
                            ),
                        },
                        "task": {
                            "type": "string",
                            "description": (
                                "One sentence saying what to do about it, as "
                                "an instruction. Required for every issue, "
                                "including those whose action is 'none'."
                            ),
                        },
                    },
                    "required": [
                        "shape", "status", "issue", "action", "note", "task",
                    ],
                    "additionalProperties": False,
                },
            },
            "slide_issues": {
                "type": "array",
                "description": (
                    "Slide-level problems no per-shape verdict can express, "
                    "each with what to do about it. Empty when there are none."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "note": {"type": "string",
                                 "description": "What is wrong, in a sentence."},
                        "task": {"type": "string",
                                 "description": "What to do about it, as an instruction."},
                    },
                    "required": ["note", "task"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["shapes", "slide_issues"],
        "additionalProperties": False,
    }


def review_slides(
    deck: DeckProfile,
    images: Sequence[tuple[int, Path]],
    model: str,
    thinking_budget: int,
    api_key_env: str = "GEMINI_API_KEY",
    concurrency: int = 6,
) -> list[SlideReview]:
    """One review per rendered slide. Never raises.

    `images` is what the renderer actually produced, so a deck that rendered
    in part is reviewed in part rather than not at all. A slide with nothing
    listable on it is skipped with a reason rather than sent: there is nothing
    for a verdict to be about, and the call would be paid for anyway.
    """
    by_number = {slide.number: slide for slide in deck.slides}
    reviews: dict[int, SlideReview] = {}
    work: list[tuple[int, Path, str, dict[str, Listed]]] = []

    for number, path in images:
        slide = by_number.get(number)
        if slide is None:
            reviews[number] = SlideReview(
                slide=number, reason="this slide is not in the deck that was read"
            )
            continue
        listing, refs = build_shape_map(slide, deck.width_in, deck.height_in)
        if not refs:
            reviews[number] = SlideReview(
                slide=number, reason="nothing on this slide to review"
            )
            continue
        work.append((number, path, listing, refs))

    if not work:
        return _ordered(reviews)

    try:
        client = build_client(api_key_env)
    except Exception as exc:
        log.warning("no design check: %s", exc)
        for number, _path, _listing, _refs in work:
            reviews[number] = SlideReview(slide=number, reason=str(exc))
        return _ordered(reviews)

    # Set when the account turns out to be spent, so the remaining slides do
    # not each ask and each be refused. One message, not one per slide. See
    # `ai.layout`, where the same 429 cost half an hour of sleeping.
    spent: list[Exception] = []

    def one(number, path, listing, refs) -> SlideReview:
        if spent:
            return SlideReview(slide=number, reason=str(spent[0]))
        try:
            return _ask(
                client, number, path, listing, refs,
                (deck.width_in, deck.height_in), model, thinking_budget,
            )
        except Exhausted as exc:
            if not spent:
                spent.append(exc)
                log.error("design check stopped: %s", exc)
            return SlideReview(slide=number, reason=str(exc))
        except Exception as exc:      # never fatal: the other slides stand
            log.warning("no design verdict for slide %d: %s", number, exc)
            return SlideReview(slide=number, reason=str(exc))

    workers = max(1, min(concurrency, len(work)))
    if workers == 1:
        for item in work:
            review = one(*item)
            reviews[review.slide] = review
    else:
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="designqa"
        ) as pool:
            for review in pool.map(lambda item: one(*item), work):
                reviews[review.slide] = review

    looked = sum(1 for r in reviews.values() if r.reviewed)
    log.info("the model looked at %d of %d rendered slide(s)", looked, len(images))
    return _ordered(reviews)


def _ordered(reviews: dict[int, SlideReview]) -> list[SlideReview]:
    """Back into slide order.

    The calls finish in whatever order the network allows, and a page that
    listed them that way would read differently on every run.
    """
    return [reviews[n] for n in sorted(reviews)]


def _ask(
    client: Any,
    number: int,
    path: Path,
    listing: str,
    refs: dict[str, "Listed"],
    size: tuple[float, float],
    model: str,
    thinking_budget: int,
) -> SlideReview:
    contents: list[Any] = [
        f"This is slide {number} as PowerPoint renders it.",
        file_part(client, path, "image/png"),
        "The shapes on it, with where each one sits on the page:\n" + listing,
        "Answer with one entry per shape above, using its ref, then the "
        "slide-level issues.",
    ]
    data, _response = generate_json(
        client,
        model=model,
        contents=contents,
        system_instruction=INSTRUCTIONS,
        schema=_schema(list(refs)),
        thinking_budget=thinking_budget,
        max_output_tokens=_ANSWER_TOKENS + max(0, thinking_budget),
    )
    return review_from_response(data, number, refs, size)


# --------------------------------------------------------------------------- #
# Across the deck
# --------------------------------------------------------------------------- #

# Slides on one sheet, and how wide each tile is drawn. Three across at 512px
# is 1536px of sheet -- under the 1568px long edge past which the whole thing
# is downsized before anything looks at it, so the tile a model sees is the
# tile written here. Twelve to a sheet keeps it to four rows.
_SHEET_COLUMNS = 3
_SHEET_TILE_W = 512
_PER_SHEET = 12

# How much of a deck goes into one comparison. Three sheets is thirty-six
# slides, which is a long deck; past that the tiles a model can hold in one
# answer stop being the limit and the answer does. A longer deck is compared
# in its first thirty-six slides and says so.
_MAX_SHEETS = 3

# Room for the answer. A list of mismatches is a dozen sentences at most.
_DECK_ANSWER_TOKENS = 2048

_DECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deck_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(DECK_KINDS)},
                    "slides": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "The slide numbers this is about, as printed under "
                            "the tiles. Usually two or more."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "One sentence: which slides, what differs, and "
                            "which looks like the odd one out."
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "One sentence saying what to do about it, as an "
                            "instruction naming the slide to change."
                        ),
                    },
                },
                "required": ["kind", "slides", "note", "task"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["deck_issues"],
    "additionalProperties": False,
}


def review_consistency(
    deck: DeckProfile,
    images: Sequence[tuple[int, Path]],
    model: str,
    thinking_budget: int,
    api_key_env: str = "GEMINI_API_KEY",
) -> tuple[list[DeckIssue], str]:
    """How the slides disagree with each other. Returns (issues, reason).

    A SECOND QUESTION, NOT A BIGGER FIRST ONE. The per-slide pass sees one
    slide at a time and cannot answer this by construction: a title 4% lower
    than on every other slide looks perfectly placed on its own, and that is
    exactly the defect that makes a deck read as assembled rather than
    designed. So the slides are tiled onto sheets and asked about together.

    ONE CALL, NOT ONE PER PAIR. Comparing seventeen slides pairwise is a
    hundred and thirty-six questions; tiling them is one, and a sheet is what
    a designer looks at to answer this anyway.

    THE TABLE IS SENT WITH THE SHEET because a tile 512px wide cannot be
    measured. What each slide measures -- where its title sits, what type
    sizes are on it, where its content starts and ends -- comes from the file,
    exactly, and the picture is what says which of those differences a reader
    would actually notice.

    Never raises. The reason comes back instead, and the rest of the report
    stands: this is the part of the check a deck can do without.
    """
    numbers = [n for n, _path in images]
    if len(numbers) < 2:
        return [], "there is only one slide here, so there is nothing to compare it with"

    trimmed = list(images)[: _PER_SHEET * _MAX_SHEETS]
    dropped = len(images) - len(trimmed)

    directory = Path(tempfile.mkdtemp(prefix="formatting-tool-deck-"))
    try:
        sheets = _sheets(trimmed, directory)
        if not sheets:
            return [], (
                "the slides could not be tiled onto a sheet, so they were not "
                "compared with each other (Pillow is needed: pip install Pillow)"
            )
        try:
            client = build_client(api_key_env)
        except Exception as exc:
            return [], str(exc)

        try:
            issues = _ask_consistency(
                client, deck, trimmed, sheets, model, thinking_budget
            )
        except Exception as exc:
            log.warning("the slides were not compared with each other: %s", exc)
            return [], str(exc)
    finally:
        rmtree(directory, ignore_errors=True)

    log.info("%d mismatch(es) between slides", len(issues))
    reason = (
        f"the last {dropped} slide(s) were not included in the comparison"
        if dropped else ""
    )
    return issues, reason


def _sheets(images: Sequence[tuple[int, Path]], directory: Path) -> list[Path]:
    """The slides tiled onto as many sheets as they need. Never raises."""
    from .layoutsheet import tile_images  # noqa: PLC0415 - Pillow, lazily

    out: list[Path] = []
    for index in range(0, len(images), _PER_SHEET):
        batch = images[index:index + _PER_SHEET]
        path = directory / f"sheet{len(out) + 1}.png"
        try:
            placed = tile_images(
                path,
                [(f"Slide {n}", image) for n, image in batch],
                columns=_SHEET_COLUMNS,
                tile_w=_SHEET_TILE_W,
            )
        except Exception:
            log.warning("could not tile the slides onto a sheet", exc_info=True)
            return []
        if placed:
            out.append(path)
    return out


def _measurements(deck: DeckProfile, numbers: Sequence[int]) -> str:
    """What each slide measures, in the terms a mismatch is stated in.

    Off the file rather than off the picture, because these are the things a
    render cannot be measured for and the file states exactly: where the title
    box is, what type sizes are on the slide, where its content begins and
    ends. The picture decides which of the differences matter; this decides
    what they are.
    """
    lines: list[str] = []
    by_number = {slide.number: slide for slide in deck.slides}
    for number in numbers:
        slide = by_number.get(number)
        if slide is None:
            continue
        content = [
            shape for shape in slide.shapes
            if shape.geometry is not None
            and shape.geometry.width_in > 0
            and shape.placeholder_token not in MARGIN_CHROME
        ]
        if not content:
            lines.append(f"slide {number}: nothing on it")
            continue

        parts = [f"slide {number}"]
        title = next(
            (s for s in content if getattr(s.role, "value", "") == "title"), None
        )
        if title is not None and deck.width_in and deck.height_in:
            box = title.geometry
            parts.append(
                f"title at left {box.left_in / deck.width_in * 100:.0f}%, "
                f"top {box.top_in / deck.height_in * 100:.0f}%, "
                f"width {box.width_in / deck.width_in * 100:.0f}%"
                f"{_type_note(title)}"
            )
        else:
            parts.append("no title")

        if deck.width_in and deck.height_in:
            left = min(s.geometry.left_in for s in content)
            right = max(s.geometry.left_in + s.geometry.width_in for s in content)
            parts.append(
                f"content from left {left / deck.width_in * 100:.0f}% "
                f"to {right / deck.width_in * 100:.0f}%"
            )

        sizes = sorted({
            run.size_pt
            for shape in content
            for paragraph in shape.paragraphs
            for run in paragraph.runs
            if run.size_pt and run.text.strip()
        })
        if sizes:
            parts.append(
                "type at " + ", ".join(f"{size:g}pt" for size in sizes[:6])
            )
        parts.append(f"{len(content)} shape(s)")
        lines.append("; ".join(parts))
    return "\n".join(lines)


def _ask_consistency(
    client: Any,
    deck: DeckProfile,
    images: Sequence[tuple[int, Path]],
    sheets: Sequence[Path],
    model: str,
    thinking_budget: int,
) -> list[DeckIssue]:
    numbers = [n for n, _path in images]
    contents: list[Any] = [
        f"These are {len(numbers)} slides of {deck.path}, tiled onto "
        f"{len(sheets)} sheet(s). Each tile is captioned with its slide number."
    ]
    for sheet in sheets:
        contents.append(file_part(client, sheet, "image/png"))
    contents.append("What each slide measures:\n" + _measurements(deck, numbers))
    contents.append(
        "Report the differences between these slides that a reader would "
        "notice. Name the slides by the numbers on the tiles."
    )

    data, _response = generate_json(
        client,
        model=model,
        contents=contents,
        system_instruction=CONSISTENCY_INSTRUCTIONS,
        schema=_DECK_SCHEMA,
        thinking_budget=thinking_budget,
        max_output_tokens=_DECK_ANSWER_TOKENS + max(0, thinking_budget),
    )
    return deck_issues_from_response(data, numbers)


def deck_issues_from_response(
    payload: dict[str, Any], numbers: Sequence[int]
) -> list[DeckIssue]:
    """The mismatches, dropping any that names a slide nobody looked at.

    A slide number is the only handle these have: they are notes, so nothing
    validates them later, and one naming slide 40 of a deck that was compared
    up to 36 would send a designer to a slide the model never saw.
    """
    known = set(numbers)
    out: list[DeckIssue] = []
    for raw in payload.get("deck_issues") or []:
        note = str(raw.get("note") or "").strip()
        if not note:
            continue
        kind = str(raw.get("kind") or "other").strip().lower()
        if kind not in DECK_KINDS:
            kind = "other"
        named = [
            int(n) for n in raw.get("slides") or []
            if isinstance(n, int) and n in known
        ]
        if not named:
            continue
        out.append(DeckIssue(
            kind=kind, slides=sorted(set(named)), note=note,
            task=str(raw.get("task") or "").strip(),
        ))
    return out


# --------------------------------------------------------------------------- #
# Mapping back onto shapes
# --------------------------------------------------------------------------- #

def review_from_response(
    payload: dict[str, Any],
    number: int,
    refs: dict[str, "Listed"],
    size: tuple[float, float] = (0.0, 0.0),
) -> SlideReview:
    """The model's answer as verdicts, dropping anything unaddressable.

    Dropped rather than repaired: a verdict naming a ref that was never sent
    is the model having invented a shape, and the nearest real shape to an
    invented one is not a near miss, it is a different shape on somebody's
    client deck.

    One verdict per shape. A second answer for the same ref is dropped, since
    which of two contradicting verdicts is the real one is not knowable here.
    """
    review = SlideReview(slide=number, reviewed=True)
    seen: set[str] = set()

    for raw in payload.get("shapes") or []:
        ref = str(raw.get("shape") or "").strip()
        listed = refs.get(ref)
        if listed is None or ref in seen:
            continue
        seen.add(ref)
        shape = listed.shape

        status = "issue" if str(raw.get("status")).strip() == "issue" else "ok"
        action = str(raw.get("action") or "none").strip().lower()
        if action not in ACTIONS:
            action = "none"
        issue = str(raw.get("issue") or "").strip().lower()
        if issue not in ISSUES:
            issue = ""
        # A font step on a shape the model says is fine is not a correction,
        # it is a change nobody asked for.
        if status == "ok":
            action, issue = "none", ""

        parent_ref = ref.rsplit(".", 1)[0] if "." in ref else ""
        parent = refs.get(parent_ref)
        review.verdicts.append(
            ShapeVerdict(
                slide=number,
                ref=ref,
                shape=listed.name,
                shape_id=listed.shape_id,
                role=_role_of(shape),
                status=status,
                issue=issue,
                action=action,
                note=str(raw.get("note") or "").strip(),
                task=str(raw.get("task") or "").strip() if status == "issue" else "",
                parent=parent.name if parent is not None else "",
                parent_id=parent.shape_id if parent is not None else None,
                path=listed.path,
                parent_path=parent.path if parent is not None else (),
                box=_box_of(shape, size),
            )
        )

    for entry in payload.get("slide_issues") or []:
        # A bare string is still read, because that is what the first version
        # of this asked for and a model that answers in the old shape should
        # not lose the finding; it arrives as a note with no task, which the
        # page then shows as work with nothing said about it.
        if isinstance(entry, str):
            note, task = entry.strip(), ""
        else:
            note = str(entry.get("note") or "").strip()
            task = str(entry.get("task") or "").strip()
        if note or task:
            review.slide_issues.append(SlideIssue(note=note or task, task=task))
    return review


def _box_of(
    shape: ShapeProfile, size: tuple[float, float]
) -> Optional[tuple[float, float, float, float]]:
    """The shape's rectangle as fractions of the slide, or None.

    None where the slide has no size to divide by, which is a deck that could
    not be read rather than a shape in an odd place. A page given None draws
    the verdict without a box, which is worse than a box and much better than
    a box somewhere arbitrary.
    """
    width_in, height_in = size
    box = shape.geometry
    if box is None or width_in <= 0 or height_in <= 0:
        return None
    return (
        round(box.left_in / width_in, 5),
        round(box.top_in / height_in, 5),
        round(box.width_in / width_in, 5),
        round(box.height_in / height_in, 5),
    )
