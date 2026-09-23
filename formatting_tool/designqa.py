"""The design check: what a slide looks like, and the one thing that fixes it.

A second pipeline, deliberately beside `pipeline.run` rather than inside it.
The validation pipeline measures a deck against a master and a brand file and
reports what is provably wrong with it -- a colour off the palette, a box off
the grid, a typeface nobody approved. It needs both files and it answers in
rules.

This one needs neither. It takes a deck on its own, renders every slide through
PowerPoint, and asks a model the question the rules cannot reach: does this
look right. What comes back is a verdict per shape and a list of what is wrong
with the slide as a whole, and the executable part of both is bounded
arithmetic (`apply.qafix`): a type step, a centring, a widening, and the moves
that put a set of shapes back in line.

WHAT MAKES A FINDING FIXABLE IS WHO SUPPLIES THE NUMBERS. The model is reading
a picture, so it is asked for judgements a picture supports -- this caption is
too small, these five circles should share a top edge -- and never for a
coordinate. Wherever the answer to "by how much" can be counted off the file,
the finding becomes a tick box; wherever it cannot, it becomes a task and is
written into the deck as a comment. That line, not the kind of defect, is what
separates the two halves of the report.

TWO QUESTIONS, NOT ONE. The per-slide pass sees one slide at a time, and there
is a whole class of defect it cannot see from there: a title 4% lower than on
every other slide looks perfectly placed on its own, and a deck of individually
faultless slides that do not match each other is exactly what reads as
assembled rather than designed. So the slides are also tiled onto a sheet and
compared with each other, once, in `ai.designqa.review_consistency`. Those
findings are notes -- a mismatch has two sides and which of them is wrong is a
designer's call, not a step this tool may take.

WHY IT IS SEPARATE. The two answer to different evidence and fail differently.
A rule finding is true whether or not anyone agrees with it; a design verdict
is somebody's reading of a picture, and the honest place for it is a page where
every note is offered to a designer rather than mixed into a report that is
otherwise provable. And the check has one hard requirement the rules do not: no
renderer, no check. There is nothing to look at.

Nothing here raises for a slide it cannot do. A deck that renders in part is
reviewed in part, a model that declines a slide leaves that slide's reason on
the report, and a run with no renderer at all comes back saying so with no
slides on it -- which is what the page shows instead of a wall of silence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .ai.client import AIConfig
from .ai.designqa import (
    _ARRANGE_MIN,
    DeckIssue,
    SlideIssue,
    SlideReview,
    review_consistency,
    review_slides,
)
from .extract import derive_master_spec, read_deck
from .guidelines import BrandGuidelines
from .models import (
    Category,
    DeckProfile,
    FixAction,
    Issue,
    Severity,
    ShapeProfile,
    Source,
    walk_shapes,
)

log = logging.getLogger(__name__)


def _op_of(verdict: Any) -> str:
    """Which correction stands behind a verdict, for the page to name.

    The measured verb where there is one, the proposal's op otherwise. They
    are applied by different halves of the tool -- the verbs through
    PowerPoint because they need measuring, the proposals through
    `apply.apply_fixes` because that is where the guards are -- and a designer
    reading the list does not need to know which.
    """
    if not verdict.executable:
        return ""
    if verdict.action and verdict.action != "none":
        return verdict.action
    return verdict.fix.op if verdict.fix is not None else ""


# How far off the deck's usual position a shape has to be before moving it is
# worth doing, and how many slides have to agree before "usual" means anything.
# A quarter of a line of body copy is visible when slides are flicked through;
# two slides agreeing is not a convention, it is a coincidence.
_ALIGN_FLOOR_IN = 0.05
_ALIGN_QUORUM = 3

# The same idea for a set of shapes on one slide, and much tighter, because it
# is answering a different question. 0.05in is the point at which moving a
# title is worth the change; a row is either even or it is not, and a circle
# left 0.05in short of the line is the defect that was reported, still there,
# on a row the page has just said was levelled. Half a millimetre is also
# about six pixels of the render a designer checks the result on.
#
# Nothing is gained by going below what `qafix._align` already treats as no
# movement at all, so this is that number.
_ARRANGE_FLOOR_IN = 0.01

# How far centring a set across the slide may carry it, mirroring the limit
# `qafix._align` puts on any one move for the same reason the floor above
# mirrors its floor: a target computed here that the applier will refuse is a
# tick box that does nothing. A set that has to travel further than this is
# not sitting slightly off centre, it is sitting in a different part of the
# slide, and moving it there is a composition decision.
_CENTER_LIMIT_IN = 2.0

# The deterministic rules this check runs beside the model, and the list is
# short for one reason: THESE ARE THE ONES THAT NEED NO MASTER.
#
# The design check takes a deck on its own and asks whether it holds together,
# so its reference is derived from the deck itself (`_own_spec`). That is the
# right authority for "does this agree with itself" and the wrong one for "is
# this on brand": run the palette rules against it and every colour is on the
# palette by construction, or off it by an accident of what the theme happens
# to hold. On the test deck that is 156 `color.text.unused_by_master` and 135
# `color.text.off_palette` findings, none of which mean anything, drowning the
# ones that do.
#
# Everything here measures the deck against itself or against arithmetic: a
# contrast ratio, two boxes that overlap, copy that does not fit its box, a row
# whose members disagree with each other. Each one is provable from the file
# and each one already has a fixer, which is the other half of why they are
# worth running here -- see `_rule_tasks`.
#
# WHY THEY WERE NOT RUN BEFORE. Not a decision, an absence: this check was
# built as the model's half of the tool and never asked the rule layer
# anything. The cost was the whole of it. On a deck built to demonstrate
# contrast failures, the page reported one finding on the contrast slide -- a
# clipped caption -- while these rules find nine there, including all four of
# the pairings the slide was drawn to show.
#
# `space.overlap` IS HERE ON A CONDITION, and it is the only rule on this list
# that has one. It reports two RECTANGLES that overlap, and this page's standard
# is the opposite: the model is told, in as many words, that "two boxes whose
# rectangles overlap is not a collision; type touching type is". Offered
# unconditionally it is six rows on the test deck about captions whose boxes
# clip the next label by six hundredths of an inch and whose type never touches.
#
# But refusing it outright threw away the move. A badge sitting over the words
# of the button it belongs to is a real collision, the model sees it and says
# so, and `fix_overlap` knows exactly how far to nudge it -- the shortest way
# out along one axis, measured off the two rectangles. The model has the
# standard and the rule has the arithmetic, and neither is any use alone.
#
# So it is offered only where the model reported a collision on the same shape:
# the render supplies the judgement, the file supplies the number. See
# `_conditional_rules`.
MASTER_FREE_RULES = frozenset({
    "color.text.contrast",
    "space.text_collision",
    "space.text_overflow",
    "space.off_canvas",
    "space.repeat_out_of_line",
    "space.row_out_of_line",
    "space.series_crowded",
    "space.series_uneven",
    "space.matrix_gutter",
    "space.band_width",
    "space.satellite_offset",
    "typography.whitespace",
    "typography.orphan_widow",
    "typography.heading_balance",
    "typography.terminal_punctuation",
    "typography.anchor_blocks_fit",
    "size.autofit_scale",
    "space.text_insets",
})

# The brand rules, offered only when this check was handed a real master.
#
# THE OBJECTION TO THEM WAS NEVER THE RULES, IT WAS THE REFERENCE. Measured
# against a spec derived from the deck itself they are noise: every colour is
# on the palette by construction, or off it by an accident of what the theme
# happens to hold, and on one test deck that was 156 findings about colours the
# master never writes text in and 135 about colours off a palette inferred from
# the file being audited.
#
# Handed the master the deck was restyled onto, they are the question somebody
# looking at that deck is actually asking: are these the brand's colours. The
# deck check knows that master, and hands this page the deck it made with it.
#
# `color.shape.off_palette` IS THE ONE THAT REACHES THE ICONS. An icon from
# PowerPoint's library is a picture, and what the ribbon calls a Graphics Fill
# is not `a:solidFill` on the shape -- the colour lives inside the SVG the
# picture draws from, which `svgicon` reads and `fixers._recolor_icon` writes.
# Nothing else in this tool recolours an icon, and on a deck of twelve
# attribute cards the icons are most of what a reader sees.
WITH_MASTER_RULES = frozenset({
    "color.text.off_palette",
    "color.shape.off_palette",
})

# The rules whose corrections would rewrite a chart, and so are not offered
# for a shape the model said is part of one. Every one of them moves or resizes
# a shape to agree with a set, which is precisely what a bar must not do: its
# height IS the number. A chart's labels being small or its plot sitting low is
# still reported as a note -- see `MASTER_FREE_RULES` -- it just stops being a
# tick box that drags a bar.
_NOT_ON_A_CHART = frozenset({
    "space.repeat_out_of_line",
    "space.row_out_of_line",
    "space.series_crowded",
    "space.series_uneven",
    "space.matrix_gutter",
    "space.satellite_offset",
    "space.band_width",
    "space.text_collision",
    "space.overlap",
    "size.autofit_scale",
    "typography.heading_balance",
})

# Rules offered only where the model saw the same defect on the same shape.
# The value is the verdict kinds that count as having seen it.
_CONDITIONAL_RULES = {"space.overlap": {"overlap"}}

# Which rule finding says the same thing as which verdict from the model, for
# the de-duplication in `DesignQaReport.tasks`. Both layers look at the same
# slide and they overlap in exactly three places; everywhere else they are
# complementary, which is the point of running both.
#
# THE RULE WINS EVERY TIE, and not because it is cleverer. It carries a number
# nobody has to be trusted for and a fixer that can act on it, and the model's
# version of the same finding carries a sentence. Two rows about one defect,
# one of them tickable, is a page asking a designer to work out which is which.
_SAME_DEFECT = {
    "low_contrast": {"color.text.contrast"},
    "overlap": {"space.text_collision", "space.overlap"},
    "cut_off": {"space.text_overflow"},
}


@dataclass(frozen=True)
class Task:
    """One piece of work, whether or not this tool can do it.

    EVERY FINDING BECOMES ONE. That is the point of the type: a verdict the
    applier can act on and an observation about an empty column are the same
    thing to the person who has to hand the deck over -- something to do before
    it goes -- and keeping them in two shapes is what let the second kind read
    as decoration. What differs is `fixable`, which says whether ticking it
    does the work or whether a designer does.

    `id` is how the page addresses it: `<slide>:<ref>` for a shape, and
    `slide:<n>:<i>` or `deck:<i>` for the findings that are about a slide or
    about the deck rather than about one shape.
    """

    id: str
    kind: str                     # shape | slide | deck
    what: str                     # the instruction, in the imperative
    why: str = ""                 # what was seen, which is not the same thing
    slide: Optional[int] = None
    slides: list[int] = field(default_factory=list)
    shape: str = ""
    shape_id: Optional[int] = None
    issue: str = ""
    fixable: bool = False
    op: str = ""                  # the correction, when there is one
    # Where to anchor a comment about it, as fractions of the slide. The shape
    # it is about, or nothing for a finding about the whole slide.
    box: Optional[tuple[float, float, float, float]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "what": self.what, "why": self.why,
            "slide": self.slide, "slides": list(self.slides), "shape": self.shape,
            "shape_id": self.shape_id, "issue": self.issue,
            "fixable": self.fixable, "op": self.op,
            "box": list(self.box) if self.box else None,
        }


@dataclass
class DesignQaReport:
    """One deck's design check, as the page and the tests read it."""

    deck: str
    generated_at: str
    model: str
    width_in: float = 0.0
    height_in: float = 0.0
    reviews: list[SlideReview] = field(default_factory=list)
    # How the slides disagree with each other, which no per-slide verdict can
    # see: a title 4% lower than on every other slide looks perfectly placed
    # on its own. See `ai.designqa.review_consistency`.
    deck_issues: list[DeckIssue] = field(default_factory=list)
    consistency_reason: str = ""
    # Why there is less here than a deck's worth: no renderer, no API key, a
    # quota spent halfway down. Never silent -- a check that did not happen
    # and a deck with nothing wrong with it look identical on a page, and one
    # of them is a lie.
    reason: str = ""
    # The deck as it was read, kept off the dict. Applying happens minutes
    # after the check and re-reading the deck then would risk describing a
    # different file.
    profile: Optional[DeckProfile] = None
    # What the deterministic rules found on the same deck, filtered to the ones
    # that need no master (`MASTER_FREE_RULES`). Kept off the dict for now the
    # way `profile` is: the page reads them as tasks, which is the only shape
    # this list has ever needed to take.
    rule_issues: list[Issue] = field(default_factory=list)
    # Whether `spec` is a real master's or one derived from the deck. It
    # decides whether the brand rules mean anything here -- see
    # `WITH_MASTER_RULES` -- and it is carried rather than inferred from the
    # spec, because a spec derived from a deck and a spec read off a master are
    # the same type and tell the same story about themselves.
    has_master: bool = False
    # THE DECK AS ITS OWN AUTHORITY. Every proposal the model makes is checked
    # against a brand reference before it is written, and refused outright
    # when there is none -- `apply.fixers.fix_ai_action` is explicit that an
    # unchecked proposal is a guess with a brand label on it. This page has no
    # master to offer: it takes one deck and asks whether it holds together.
    #
    # So the reference is derived from the deck itself: its own theme palette,
    # its own typefaces, and the size range its own slides already use for
    # each role. That is the right authority for THIS question. It says a
    # caption may be set to a size this deck uses for captions and not to one
    # nobody chose, which is what "make it match" means. It is not a brand
    # system and does not pretend to be one -- a deck that is wrong throughout
    # will happily vouch for being wrong consistently, and that is what the
    # deck check next door is for.
    spec: Optional[Any] = None

    @property
    def verdicts(self) -> list[Any]:
        return [v for review in self.reviews for v in review.verdicts]

    @property
    def offered_rule_issues(self) -> list[Issue]:
        """The deterministic findings this page puts in front of a designer.

        `rule_issues` is everything the rule layer found, because that is what
        `apply_fixes` needs as a baseline to tell its own damage from the
        deck's. This is the half that is offered as work: `MASTER_FREE_RULES`
        always, and `WITH_MASTER_RULES` on top of it when this check was handed
        the master the deck was restyled onto rather than left to infer one.
        """
        seen = self._collisions_seen()
        offered = MASTER_FREE_RULES | (
            WITH_MASTER_RULES if self.has_master else frozenset()
        )
        out = []
        for issue in self.rule_issues:
            rule = issue.rule_id or ""
            if rule in _NOT_ON_A_CHART and self._on_a_chart(issue):
                continue
            if rule in offered:
                out.append(issue)
            elif rule in _CONDITIONAL_RULES and (
                (issue.slide, issue.shape_id) in seen.get(rule, set())
            ):
                out.append(issue)
        return out

    def _on_a_chart(self, issue: Issue) -> bool:
        """Whether this finding is about a shape drawn inside a data graphic.

        THE RULES THAT MOVE A SET ARE THE ONES A CHART MOST LOOKS LIKE. A bar
        chart is a run of like-sized boxes on a regular grid with one of them
        a different height, which is the literal definition `space.series_*`
        and `space.repeat_out_of_line` match on -- and every correction they
        would make to it changes what the picture says the numbers are. No
        reading of the file can tell that run from a row of cards; the model
        looking at the render can, and `SlideReview.charts` is where it said
        so.
        """
        if issue.slide is None:
            return False
        review = next(
            (r for r in self.reviews if r.slide == issue.slide), None
        )
        if review is None or not review.charts:
            return False
        return review.inside_a_chart(self._box_of(issue))

    def _collisions_seen(self) -> dict:
        """Where the model agreed there is a defect, per conditional rule.

        `space.overlap` measures rectangles and this page judges renders, so
        the rule's arithmetic is offered only where the model looking at the
        picture said the same thing about the same shape. Neither half is any
        use alone: the model cannot say how far to nudge, and the rectangles
        cannot say whether the type actually touches.
        """
        found: dict = {}
        for rule, kinds in _CONDITIONAL_RULES.items():
            where = {
                (verdict.slide, verdict.shape_id)
                for review in self.reviews
                for verdict in review.verdicts
                if verdict.issue in kinds and verdict.shape_id is not None
            }
            found[rule] = where
        return found

    @property
    def tasks(self) -> list[Task]:
        """Every finding, as a piece of work. Nothing is left as an observation.

        THE ORDER IS THE ORDER SOMEBODY WOULD WORK IN: the deck-wide
        mismatches first, because fixing a slide that then still does not match
        the others is doing the job twice, then each slide's own findings in
        slide order.

        A task is `fixable` when the applier has a correction for it. That is a
        statement about this tool, not about the finding: "the right half of
        the slide is empty" is no less real for having no arithmetic, and it
        appears in this list beside the ones that do.

        TWO LAYERS, ONE LIST. The rules' findings go in beside the model's and
        are not marked as coming from somewhere else, because which half of the
        tool noticed a defect is this tool's bookkeeping and not a designer's
        problem. Where the two describe the same defect on the same shape the
        rule's is kept -- see `_SAME_DEFECT` -- since it carries the number and
        the fix and the model's carries a sentence.
        """
        out: list[Task] = []
        for index, issue in enumerate(self.deck_issues):
            out.append(self._deck_task(index, issue))
        covered = self._covered_by_rules()
        for review in self.reviews:
            for verdict in review.verdicts:
                if verdict.status != "issue":
                    continue
                if (verdict.slide, verdict.shape_id, verdict.issue) in covered:
                    continue
                out.append(Task(
                    id=f"{verdict.slide}:{verdict.ref}",
                    kind="shape",
                    what=verdict.task or verdict.note or "look at this",
                    why=verdict.note,
                    slide=verdict.slide,
                    shape=verdict.shape,
                    shape_id=verdict.shape_id,
                    issue=verdict.issue,
                    fixable=verdict.executable,
                    op=_op_of(verdict),
                    box=verdict.box,
                ))
            for index, issue in enumerate(review.slide_issues):
                # Fixable only once the moves have been counted. An
                # arrangement whose shapes are already in line produces no
                # steps, and offering a tick that would do nothing is the one
                # thing worse than not offering one -- see `_arrange_steps`.
                steps = _arrange_steps(self, review.slide, index, issue)
                out.append(Task(
                    id=f"slide:{review.slide}:{index}",
                    kind="slide",
                    what=issue.task or issue.note,
                    why=issue.note if issue.task else "",
                    slide=review.slide,
                    issue=issue.arrangement if steps else "",
                    fixable=bool(steps),
                    op=(steps[0].op if steps else ""),
                ))
        out.extend(self._rule_tasks())
        return out

    def _rule_tasks(self) -> list[Task]:
        """The deterministic findings as work, in slide order.

        Last on the list rather than first, and that is about reading rather
        than about importance. A designer works down this page with the slide
        open; the model's findings are the ones that arrived from looking at
        it, and these are the ones that arrived from measuring it. Either order
        is defensible and mixing them by slide would be worse than both.

        `fixable` is asked of the applier rather than assumed. Every rule in
        `MASTER_FREE_RULES` has a fixer registered, and a fixer still declines
        a particular finding -- a shape it cannot find, a move with nowhere to
        go -- which is exactly what `fixer_for` answers.
        """
        from .apply import fixer_for  # noqa: PLC0415 - pulls in python-pptx

        out: list[Task] = []
        for issue in sorted(self.offered_rule_issues, key=lambda i: (i.slide or 0)):
            out.append(Task(
                id=f"rule:{issue.id}",
                kind="shape",
                what=issue.suggestion or issue.message,
                why=issue.message if issue.suggestion else "",
                slide=issue.slide,
                shape=issue.shape or "",
                shape_id=issue.shape_id,
                issue=issue.rule_id or "",
                fixable=fixer_for(issue) is not None,
                op=issue.rule_id or "",
                box=self._box_of(issue),
            ))
        return out

    def _box_of(self, issue: Issue) -> Optional[tuple]:
        """Where a rule finding sits, as fractions of the slide.

        The model's verdicts carry this already -- it is the rectangle the
        model was given -- and a rule finding carries a slide and a shape id
        instead, so the page would have nothing to draw on the render and
        nothing to anchor a comment to. Looked up rather than left out: half
        the value of the page is that a finding points at the thing it is
        about.
        """
        if self.profile is None or not issue.slide or issue.shape_id is None:
            return None
        if self.width_in <= 0 or self.height_in <= 0:
            return None
        for slide in self.profile.slides:
            if slide.number != issue.slide:
                continue
            for shape in walk_shapes(slide.shapes):
                if shape.shape_id != issue.shape_id or shape.geometry is None:
                    continue
                box = shape.geometry
                return (
                    round(box.left_in / self.width_in, 5),
                    round(box.top_in / self.height_in, 5),
                    round(box.width_in / self.width_in, 5),
                    round(box.height_in / self.height_in, 5),
                )
        return None

    def _covered_by_rules(self) -> set:
        """The model's verdicts a rule finding already says, keyed by shape.

        Both layers look at the same slide and they overlap in three places --
        see `_SAME_DEFECT`. Everywhere else they are complementary, which is
        the point of running both: on one slide of a test deck the model
        reported seven collisions of rendered type where `space.overlap`
        reported three overlapping boxes, and neither list is the other.
        """
        by_shape: dict[tuple, set] = {}
        for issue in self.offered_rule_issues:
            if issue.slide is None or issue.shape_id is None:
                continue
            by_shape.setdefault(
                (issue.slide, issue.shape_id), set()
            ).add(issue.rule_id or "")

        covered = set()
        for review in self.reviews:
            for verdict in review.verdicts:
                rules = _SAME_DEFECT.get(verdict.issue)
                if not rules or verdict.shape_id is None:
                    continue
                if rules & by_shape.get((verdict.slide, verdict.shape_id), set()):
                    covered.add((verdict.slide, verdict.shape_id, verdict.issue))
        return covered

    def _deck_task(self, index: int, issue: DeckIssue) -> Task:
        """One cross-slide mismatch as work, fixable where the deck itself can
        say what the majority does -- see `_deck_fixes`."""
        steps, proposals = _deck_fixes(self, index, issue)
        op = ""
        if steps:
            op = "align"
        elif proposals:
            op = proposals[0].fix.op if proposals[0].fix else ""
        return Task(
            id=f"deck:{index}",
            kind="deck",
            what=issue.task or issue.note,
            why=issue.note if issue.task else "",
            slides=list(issue.slides),
            slide=issue.slides[0] if issue.slides else None,
            issue=issue.kind,
            fixable=bool(steps or proposals),
            op=op,
        )

    @property
    def notes(self) -> list[str]:
        """The tasks nobody can tick, in one line each.

        Kept as a property of its own because it is what gets written into the
        deck: everything the applier cannot do, in the words a designer will
        read in the Comments pane.
        """
        return [
            (f"slide {task.slide}: " if task.slide else "") + task.what
            for task in self.tasks if not task.fixable
        ]

    @property
    def stats(self) -> dict[str, int]:
        return {
            "slides": len(self.reviews),
            "reviewed": sum(1 for r in self.reviews if r.reviewed),
            "shapes": len(self.verdicts),
            "issues": sum(1 for v in self.verdicts if v.status == "issue"),
            "actions": sum(1 for task in self.tasks if task.fixable),
            "notes": len(self.notes),
            "tasks": len(self.tasks),
            "mismatches": len(self.deck_issues),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "deck": self.deck,
            "generated_at": self.generated_at,
            "model": self.model,
            "width_in": self.width_in,
            "height_in": self.height_in,
            "slides": [review.to_dict() for review in self.reviews],
            "tasks": [task.to_dict() for task in self.tasks],
            "deck_issues": [issue.to_dict() for issue in self.deck_issues],
            "consistency_reason": self.consistency_reason,
            "notes": self.notes,
            "stats": self.stats,
            "reason": self.reason,
        }


def _own_spec(profile: DeckProfile) -> Optional[Any]:
    """What this deck vouches for: its palette, its typefaces, its type scale.

    Never fatal. A deck this cannot be derived from leaves the report without
    one, every proposal is then refused for want of something to check it
    against, and the findings stay on the list as work for a designer -- which
    is what happened to all of them before proposals existed.
    """
    try:
        spec = derive_master_spec(profile, BrandGuidelines())
        _add_its_own_colors(spec, profile)
        return spec
    except Exception:
        log.warning("could not read what %s vouches for; proposals will be "
                    "refused for want of anything to check them against",
                    profile.path, exc_info=True)
        return None


# How many slides have to use a colour before the deck is taken to mean it.
# Two is a pattern and one is a slide; a colour on a single slide is as likely
# to be the mistake being reported as the answer to it.
_VOUCHES_FOR_A_COLOR_ON = 2


def _add_its_own_colors(spec: Any, profile: DeckProfile) -> None:
    """Let the deck's own repeated text colours count as ones it vouches for.

    `derive_master_spec` fills the palette from the theme, which is right for a
    master: a master's theme IS the brand. It is too narrow here. A deck whose
    body copy is #17191C on every slide has plainly decided what colour its
    body copy is, and that colour is usually nowhere in the theme -- so a
    correction to "what the rest of the deck does" was being refused for not
    being a colour the deck declares, when the deck declares it by doing it.

    Only colours used on several slides, and only ever added: a theme entry is
    what the file says, and this is what the file does, and where the two
    disagree the theme's label is the one worth keeping.
    """
    seen: dict[str, set[int]] = {}
    for slide in profile.slides:
        for shape in slide.shapes:
            for color in _colors_of(shape):
                seen.setdefault(color, set()).add(slide.number)

    known = {
        str(value).strip().lstrip("#").upper() for value in spec.palette.values()
    }
    for color, slides in seen.items():
        if len(slides) >= _VOUCHES_FOR_A_COLOR_ON and color not in known:
            spec.palette[f"deck:{color}"] = color


def review_deck(
    deck: Path,
    images: Sequence[tuple[int, Path]],
    ai: Optional[AIConfig] = None,
    profile: Optional[DeckProfile] = None,
    concurrency: int = 6,
    spec: Optional[Any] = None,
) -> DesignQaReport:
    """Review the slides that rendered, and say so when none did.

    `images` is what the renderer produced, as (slide number, PNG) pairs. It
    is passed in rather than made here because the caller keeps the pictures:
    a page shows the same files it sends to the model, and a check whose
    evidence is in a temporary directory that has already been swept is a
    check nobody can audit.

    `profile` is the deck as `read_deck` gives it. Passed in where the caller
    already has one -- reading a 25MB deck twice for one page is most of the
    wait.

    `spec` IS THE MASTER, WHERE THERE IS ONE, and it changes what this check is
    allowed to say. Handed nothing, it derives a reference from the deck itself
    and can only ask whether the deck agrees with itself -- which is why the
    brand rules are kept off `MASTER_FREE_RULES`: measured against a theme
    inferred from the file being audited, every colour is on the palette by
    construction or off it by accident.

    Handed a master's spec, that objection disappears. The deck check hands
    this page the deck it has just restyled, and it knows the master it
    restyled onto; passing that through is the difference between "these
    colours are consistent with themselves" and "these colours are the brand's"
    -- which is the question somebody looking at a restyled deck is actually
    asking. See `WITH_MASTER_RULES`.
    """
    ai = ai or AIConfig()
    report = DesignQaReport(
        deck=Path(deck).name,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        model=ai.model,
    )

    if profile is None:
        profile = read_deck(deck)
    report.profile = profile
    report.width_in, report.height_in = profile.width_in, profile.height_in
    report.spec = spec if spec is not None else _own_spec(profile)
    report.has_master = spec is not None
    report.rule_issues = _rule_findings(deck, profile, report.spec)

    if not images:
        report.reason = (
            "nothing rendered, so there was nothing to look at. The design "
            "check needs PowerPoint and pywin32 on Windows"
        )
        log.info("no design check for %s: nothing rendered", report.deck)
        return report

    report.reviews = review_slides(
        profile,
        images,
        model=ai.model,
        thinking_budget=ai.thinking_budget,
        api_key_env=ai.api_key_env,
        concurrency=concurrency,
    )

    # One reason for the run, taken from the slides, so a page does not have
    # to read seventeen identical failures to learn that the key is missing.
    if report.reviews and not any(r.reviewed for r in report.reviews):
        reasons = [r.reason for r in report.reviews if r.reason]
        report.reason = reasons[0] if reasons else "the model returned nothing"
        report.consistency_reason = "the slides were not looked at"
        return report

    # The second question, asked once for the whole deck. Skipped when the
    # first one could not be asked at all -- a key that is missing is missing
    # for both -- but not when it merely found nothing: a deck of individually
    # faultless slides that do not match each other is the case this exists
    # for.
    report.deck_issues, report.consistency_reason = review_consistency(
        profile,
        images,
        model=ai.model,
        thinking_budget=ai.thinking_budget,
        api_key_env=ai.api_key_env,
    )
    return report


def _rule_findings(
    deck: Path, profile: DeckProfile, spec: Optional[Any]
) -> list[Issue]:
    """The deterministic findings this check is entitled to make.

    THE SAME DECK, MEASURED TWICE AND ON DIFFERENT EVIDENCE. The model reads a
    picture and answers questions a file cannot: whether type actually touches
    type, whether a caption disappears into a photograph, whether the hierarchy
    reads in the right order. The rules read the file and answer questions a
    picture cannot: the exact ratio of a pairing, the exact overlap of two
    boxes. Running one and not the other was never a decision -- this check was
    built as the model's half and never asked the rule layer anything -- and
    what it cost was every finding that already had arithmetic and a fix
    behind it.

    EVERY RULE RUNS AND ONLY `MASTER_FREE_RULES` IS OFFERED, and the gap
    between those two is not waste. What is offered has to be narrow, because
    the spec here is derived from the deck itself and the brand rules would be
    measuring the deck against its own theme -- hundreds of findings that mean
    nothing, drowning the ones that do.

    What is MEASURED has to be complete, because this list is also the
    baseline `apply_fixes` compares its own work against. That applier runs a
    second round over the findings its corrections introduced, and it works
    out which those are by asking what the deck had beforehand -- the list it
    was handed. Handed only the narrow set, it read every palette and margin
    finding on the deck as newly introduced and corrected them unasked: on the
    test deck, 56 rows ticked and a second round of 121 corrections nobody
    chose, 27 of them recolours measured against a theme inferred from the
    file being audited. Which is the one thing this page must never do.

    A METRICS PROVIDER, because this check has a renderer by definition -- it
    cannot run without one -- and four of these rules are silent without one.
    Whether copy fits its box and where its lines break is not in a .pptx, and
    PowerPoint is already open.

    Never fatal. A rule layer that cannot run costs its half of the report and
    not the run: the model's findings are worth having on their own, and that
    is what this page was until now.
    """
    if spec is None:
        return []
    try:
        from .linemetrics import default_provider  # noqa: PLC0415
        from .rules import (  # noqa: PLC0415
            RuleContext,
            build_default_rules,
            run_rules,
        )

        found = run_rules(
            RuleContext(deck=profile, spec=spec),
            build_default_rules(default_provider(deck)),
        )
    except Exception:
        log.warning("could not run the deterministic rules on %s; the design "
                    "check is the model's findings alone", profile.path,
                    exc_info=True)
        return []

    for issue in found:
        issue.deck = Path(deck).name
        issue.id = issue.fingerprint()
    return found


def issues_for(
    report: DesignQaReport, chosen: Optional[Sequence[str]] = None
) -> list[Issue]:
    """The proposals, as the findings `apply.apply_fixes` already knows how to
    carry out.

    THE POINT OF THIS FUNCTION is that it is short. Everything a proposal needs
    -- the validation, the geometric guards, the second-round recheck, the
    undo -- was written for the rule layer's AI pass and is reached by handing
    it an `Issue` with a `fix` on it. What this check adds is the eyes; what it
    does not need is a second applier.

    The id is the task id, so ticking a row on the page and selecting a finding
    in the applier are the same act. `Source.AI` is what routes it to
    `fix_ai_action` rather than to a rule's fixer.

    Confidence is 1.0 and that is not a claim about the model. `fix_ai_action`
    reads it only to gate removals, which this check never proposes; leaving it
    None would make the number mean "unknown" in a report that does not measure
    it.
    """
    wanted = None if chosen is None else set(chosen)
    out: list[Issue] = []
    for review in report.reviews:
        for verdict in review.verdicts:
            key = f"{verdict.slide}:{verdict.ref}"
            if wanted is not None and key not in wanted:
                continue
            if verdict.fix is None or not verdict.fix.valid:
                continue
            # A measured verb wins where the model asked for both: its target
            # is arithmetic this tool did, and the proposal's is a number the
            # model chose.
            if verdict.action and verdict.action != "none":
                continue
            issue = Issue(
                category=Category.OTHER,
                severity=Severity.WARNING,
                message=verdict.task or verdict.note,
                source=Source.AI,
                slide=verdict.slide,
                shape=verdict.shape,
                shape_id=verdict.shape_id,
                deck=report.deck,
                confidence=1.0,
                fix=verdict.fix,
            )
            issue.id = key
            out.append(issue)

    for index, mismatch in enumerate(report.deck_issues):
        if wanted is not None and f"deck:{index}" not in wanted:
            continue
        _steps, proposals = _deck_fixes(report, index, mismatch)
        out.extend(proposals)

    # The deterministic findings, handed over exactly as the rule layer made
    # them. They are already `Source.RULE` with a `rule_id`, which is what
    # routes each one to its own fixer in `apply.fixers.FIXERS` rather than
    # through `fix_ai_action` -- so nothing here has to know what any of them
    # do. The id is prefixed for the page and unprefixed for the applier,
    # because `apply_fixes` selects on the id the issue carries.
    for issue in report.offered_rule_issues:
        if wanted is None or f"rule:{issue.id}" in wanted:
            out.append(issue)
    return out


def outstanding(
    report: DesignQaReport,
    chosen: Optional[Sequence[str]] = None,
    applied: Optional[Sequence[Any]] = None,
) -> list[Task]:
    """The tasks a round leaves behind: everything still to be done by hand.

    Three things end up here, and lumping them together is deliberate -- they
    are the same thing to whoever picks the deck up next.

      - what nothing here can correct, which is most of it
      - what could have been corrected and was not ticked
      - what was ticked and refused, because the copy would have spilled or
        there was no room to widen into

    The third is the one worth naming. A step that was asked for and did not
    happen is exactly the finding a page can lose: it is off the tick list, it
    is not in the changes, and without this it would be nowhere.
    """
    ticked = set(chosen or ())
    done = {
        (change.slide, change.shape_id, change.op) for change in (applied or ())
    }
    # EVERY CORRECTION SAYS WHICH ROW IT ANSWERS. It has to: `align` is applied
    # by two different findings now -- a title moved to where the deck puts it,
    # and a shape moved into line with the row beside it -- and this used to
    # tell them apart by asking whether ANY align had landed on ANY of a
    # mismatch's slides. Levelling a row on slide 7 therefore answered "did the
    # title on slide 7 move?" with yes, and the mismatch dropped off this list
    # without being corrected or written into the deck. Which is worst exactly
    # when it matters: a title's own move is refused when it is further out
    # than an alignment allows, and that is when the mismatch is worth having.
    by_id = {getattr(change, "task_id", "") for change in (applied or ())}

    left: list[Task] = []
    for task in report.tasks:
        if not task.fixable or task.id not in ticked:
            left.append(task)
            continue
        if task.id in by_id:
            continue
        if task.id.startswith("rule:"):
            # A deterministic finding is applied by `apply_fixes` and reported
            # under the id the ISSUE carries, which is this task's id without
            # the prefix the page addresses it by. Matched on that rather than
            # on shape and op: a rule issue has no `fix`, so the op the applier
            # reports for it is empty and the shape-and-op match every other
            # row here uses would call every one of them outstanding.
            if task.id[len("rule:"):] not in by_id:
                left.append(task)
        elif task.kind in ("slide", "deck"):
            # Both are several corrections filed under one id, and any of them
            # landing is the row having been acted on -- which the check
            # against `by_id` above has already settled. A proposal made on a
            # mismatch carries the id as a PREFIX, because one mismatch can
            # raise a correction per shape. Reaching past both means every
            # correction in the set was refused, so the task stands and says
            # so rather than reading as done because it was ticked.
            if not any(key.startswith(f"{task.id}:") for key in by_id):
                left.append(task)
        elif (task.slide, task.shape_id, task.op) not in done:
            left.append(task)
    return left


def comments_for(report: DesignQaReport, tasks: Sequence[Task]) -> list:
    """These tasks as PowerPoint comments, anchored where they are about.

    A comment on the shape it concerns rather than in a corner of the slide:
    the designer opens the pane, clicks the note, and PowerPoint takes them to
    the thing it is about. A finding about the whole slide, or about the deck,
    has nothing to point at and sits at the top left.
    """
    from .apply.notes import Comment  # noqa: PLC0415 - COM underneath

    points_per_inch = 72.0
    width_pt = (report.width_in or 13.333) * points_per_inch
    height_pt = (report.height_in or 7.5) * points_per_inch

    out = []
    for task in tasks:
        slide = task.slide or (task.slides[0] if task.slides else None)
        if not slide:
            continue
        left_pt, top_pt = 12.0, 12.0
        if task.box:
            left_pt = round(task.box[0] * width_pt, 1)
            top_pt = round(task.box[1] * height_pt, 1)
        out.append(Comment(
            slide=slide, body=_comment_body(task),
            left_pt=left_pt, top_pt=top_pt,
        ))
    return out


def _comment_body(task: Task) -> str:
    """What the comment says: the work first, then what prompted it.

    The instruction leads because that is what the reader has to act on. The
    observation follows in brackets, so a designer who disagrees with the
    finding can see what it rests on and dismiss it on the evidence rather
    than on the instruction alone.
    """
    where = f"{task.shape}: " if task.shape else ""
    head = f"{where}{task.what}"
    if task.kind == "deck" and task.slides:
        head = f"{head} (slides {', '.join(str(n) for n in task.slides)})"
    return f"{head}\n\n{task.why}" if task.why and task.why != task.what else head


def steps_for(report: DesignQaReport, chosen: Optional[Sequence[str]] = None) -> list:
    """The corrections a report supports, optionally narrowed to ticked ones.

    `chosen` names tasks by their id -- `<slide>:<ref>` for a shape, `deck:<i>`
    for a cross-slide mismatch -- which is how the page addresses them: a shape
    id is not unique across slides and a name is not unique on one.

    Narrowing happens HERE rather than in the applier, so the applier is handed
    steps it is allowed to take and nothing else. Anything the page sends that
    is not a fixable task on this report is dropped without comment -- it names
    something that is not on the list it was given.
    """
    from .apply.qafix import steps_from  # noqa: PLC0415 - COM-side module

    wanted = None if chosen is None else set(chosen)
    reviews = [
        SlideReview(
            slide=review.slide,
            verdicts=[
                v for v in review.verdicts
                if wanted is None or f"{v.slide}:{v.ref}" in wanted
            ],
            reviewed=review.reviewed,
        )
        for review in report.reviews
    ]
    steps = steps_from(reviews)

    for index, issue in enumerate(report.deck_issues):
        if wanted is None or f"deck:{index}" in wanted:
            steps.extend(_align_steps(report, index, issue))
    # Slide-level arrangements come off `report.reviews` rather than off the
    # narrowed copy above, which drops `slide_issues` because `steps_from`
    # has never read them.
    for review in report.reviews:
        for index, issue in enumerate(review.slide_issues):
            if wanted is None or f"slide:{review.slide}:{index}" in wanted:
                steps.extend(_arrange_steps(report, review.slide, index, issue))
    return steps


# The two words the consistency pass has for one defect: a shape that sits
# somewhere else on one slide than on the rest. `position` describes it as a
# drift and `alignment` as a grid line, the model picks whichever suits the
# sentence, and both are answered by the same median. See `_deck_fixes`.
_POSITION_KINDS = frozenset({"position", "alignment"})


def _deck_fixes(
    report: DesignQaReport, index: int, issue: DeckIssue
) -> tuple[list, list[Issue]]:
    """What would settle a cross-slide mismatch: moves, or values, or nothing.

    THE MODEL SAYS WHICH SLIDES DISAGREE AND ON WHAT. THE DECK SAYS WHAT THE
    MAJORITY DOES. That split is the whole design of this function, and it is
    what makes a mismatch fixable at all: "set the body copy on slide 3 to
    match the rest" has a number behind it, and the number is one nobody has
    to be trusted for -- it is counted off the other slides.

    Four kinds of mismatch have an answer that can be counted:

      position    the shape belongs where most slides put it -- a move, taken
                  through PowerPoint because `align` is measured (`_align_steps`)
      alignment   the same measurement under the other word the consistency
                  pass has for it. "The title sits 4% lower on slide 7" and
                  "slide 7 starts on a different grid line from the rest" are
                  one deck and one answer: where the majority puts the shape.
                  Splitting them was a vocabulary accident -- both words are
                  offered to the model, it picks whichever fits the sentence it
                  is writing, and only one of them reached any arithmetic.
      type_scale  the text belongs at the size most slides set for its role
      color       the text belongs in the colour most slides give that role

    The rest come back empty and stay tasks. `spacing` has no single number to
    count, `size` names a repeated element without saying which shape it is,
    `content` is a missing element rather than a wrong value, and `other` is by
    definition not a category with an answer in it.

    What comes back for the last two is `Issue` objects carrying a `FixAction`,
    so they go through `apply.fixers.fix_ai_action` and its checks: a size has
    to sit in the range this deck uses for that role, a colour has to be one of
    this deck's own. A majority counted off a deck that disagrees with itself
    everywhere will not pass those, which is the correct outcome.
    """
    if report.profile is None:
        return [], []
    if issue.kind in _POSITION_KINDS:
        return _align_steps(report, index, issue), []
    if issue.kind == "type_scale":
        return [], _capped(report, issue, _size_fixes(report, index, issue))
    if issue.kind == "color":
        return [], _capped(report, issue, _color_fixes(report, index, issue))
    return [], []


# How much of the text on the slides a mismatch names it may rewrite before the
# correction stops being the finding the model made.
#
# A CROSS-SLIDE MISMATCH IS ABOUT A REPEATED ELEMENT. "The body copy on slide 3
# is smaller than on slides 1 and 2" is a sentence about one kind of text in one
# place, and the correction for it touches that text. What `_by_role` actually
# produces is every text-bearing shape on every named slide, grouped by role and
# corrected towards a majority counted over the whole deck -- which is the same
# answer when a deck is regular and a very different one when it is not.
#
# Measured on a deck built to demonstrate mismatched type: ONE `type_scale`
# finding produced 107 proposals, every one setting text to 12pt, and one
# `color` finding produced 96, every one to the same navy. Applied, four
# headings drawn at 22, 26, 18 and 20pt all became 12pt, three button labels
# became the colour of the buttons they sat on, and the deck came back flatter
# than it went in. Nothing about any one of those proposals was wrong; the set
# was.
#
# So a correction that would rewrite more than this share of the text on the
# slides it names is refused as a set, and the finding stays a task carrying
# the model's own sentence. A deck really is sometimes wrong throughout -- and
# that is the deck check's job, which has a master to measure against, rather
# than this page's, which infers its reference from the file it is auditing.
_REWRITE_SHARE = 0.34

# And a floor under it, so a slide with four text shapes on it is not exempt
# from the rule by arithmetic: a third of three is one.
_REWRITE_FLOOR = 4


def _capped(
    report: DesignQaReport, issue: DeckIssue, proposals: list[Issue]
) -> list[Issue]:
    """The proposals, or none of them when there are too many to be the finding.

    ALL OR NOTHING, and deliberately. Taking the first few would correct an
    arbitrary subset of a set the model described as one thing, which is worse
    than correcting none: the deck comes back half rewritten and the finding
    reads as done.
    """
    if not proposals:
        return []
    total = sum(
        1
        for slide in (report.profile.slides if report.profile else ())
        if slide.number in set(issue.slides)
        for shape in slide.shapes
        if shape.text.strip()
    )
    allowed = max(_REWRITE_FLOOR, round(total * _REWRITE_SHARE))
    if len(proposals) <= allowed:
        return proposals
    log.info(
        "%s: the %s mismatch on slide(s) %s would rewrite %d of the %d text "
        "shapes there, which is not one repeated element; left as a task",
        report.deck, issue.kind,
        ", ".join(str(n) for n in issue.slides), len(proposals), total,
    )
    return []


def _size_fixes(
    report: DesignQaReport, index: int, issue: DeckIssue
) -> list[Issue]:
    """Set the odd slides' text to the size the rest of the deck uses.

    Per role, because a deck has several type sizes on purpose and the
    majority of all of them together is a number nobody chose. A role the
    named slides do not share with the rest of the deck has no majority to
    move towards and is left alone.
    """
    out: list[Issue] = []
    for role, shapes in _by_role(report.profile, issue.slides).items():
        target = _most_common(_sizes_across(report.profile, role))
        if target is None:
            continue
        for slide_number, shape in shapes:
            sizes = _sizes_of(shape)
            if not sizes or all(abs(size - target) < 0.51 for size in sizes):
                continue
            out.append(_proposal_issue(
                report, index, slide_number, shape,
                FixAction(op="set_font_size", shape=shape.name,
                          shape_id=shape.shape_id, size_pt=target),
                f"set this to {target:g}pt, which is what the rest of the deck "
                f"uses for {role} text",
            ))
    return out


def _color_fixes(
    report: DesignQaReport, index: int, issue: DeckIssue
) -> list[Issue]:
    """Recolour the odd slides' text to the colour the rest of the deck gives
    that role. Literal colours only: a run bound to a theme slot is already
    saying "whatever the theme says", and changing it would be answering a
    different question."""
    out: list[Issue] = []
    for role, shapes in _by_role(report.profile, issue.slides).items():
        target = _most_common(_colors_across(report.profile, role))
        if target is None:
            continue
        for slide_number, shape in shapes:
            colors = _colors_of(shape)
            if not colors or all(color == target for color in colors):
                continue
            out.append(_proposal_issue(
                report, index, slide_number, shape,
                FixAction(op="recolor_text", shape=shape.name,
                          shape_id=shape.shape_id, hex=target),
                f"recolour this to #{target}, which is what the rest of the "
                f"deck uses for {role} text",
            ))
    return out


def _proposal_issue(
    report: DesignQaReport,
    index: int,
    slide_number: int,
    shape: ShapeProfile,
    fix: FixAction,
    message: str,
) -> Issue:
    """One derived correction, as a finding the shared applier can carry out.

    The id carries the task it answers, so ticking one row on the page selects
    every correction the mismatch needs, and `outstanding` can tell afterwards
    that the row is done.
    """
    issue = Issue(
        category=Category.OTHER,
        severity=Severity.WARNING,
        message=message,
        source=Source.AI,
        slide=slide_number,
        shape=shape.name,
        shape_id=shape.shape_id,
        deck=report.deck,
        confidence=1.0,
        fix=fix,
    )
    issue.id = f"deck:{index}:{slide_number}:{shape.shape_id}"
    return issue


def _by_role(
    profile: DeckProfile, slides: Sequence[int]
) -> dict[str, list[tuple[int, ShapeProfile]]]:
    """The text-bearing shapes on these slides, grouped by the role they play."""
    out: dict[str, list[tuple[int, ShapeProfile]]] = {}
    wanted = set(slides)
    for slide in profile.slides:
        if slide.number not in wanted:
            continue
        for shape in slide.shapes:
            if not shape.text.strip():
                continue
            role = getattr(shape.role, "value", "") or "unknown"
            out.setdefault(role, []).append((slide.number, shape))
    return out


# Counted over EVERY slide, including the ones the mismatch names, and that is
# not an oversight. A mismatch usually names all the slides it is about --
# "the body copy on slide 3 is smaller than on slides 1 and 2" names three --
# so counting only the ones left out counts nothing at all, which is what this
# did first and why every mismatch came back unfixable. The majority of the
# whole deck is the right answer either way: the odd slide is outvoted by
# construction, and it cannot outvote the rest.
def _sizes_across(profile: DeckProfile, role: str) -> list[float]:
    return [
        size
        for slide in profile.slides
        for shape in slide.shapes
        if (getattr(shape.role, "value", "") or "unknown") == role
        for size in _sizes_of(shape)
    ]


def _colors_across(profile: DeckProfile, role: str) -> list[str]:
    return [
        color
        for slide in profile.slides
        for shape in slide.shapes
        if (getattr(shape.role, "value", "") or "unknown") == role
        for color in _colors_of(shape)
    ]


def _sizes_of(shape: ShapeProfile) -> list[float]:
    return [
        run.size_pt
        for paragraph in shape.paragraphs
        for run in paragraph.runs
        if run.size_pt and run.text.strip()
    ]


def _colors_of(shape: ShapeProfile) -> list[str]:
    return [
        run.color_hex.upper()
        for paragraph in shape.paragraphs
        for run in paragraph.runs
        if run.color_hex and run.text.strip() and not run.color_is_theme
    ]


def _most_common(values: Sequence[Any]) -> Optional[Any]:
    """What most of the deck does, or None when there is no most.

    A tie is not a majority: two slides at 11pt and two at 12pt say the deck
    has not decided, and picking one would be this tool deciding for it.
    """
    if not values:
        return None
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda pair: -pair[1])
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _align_steps(report: DesignQaReport, index: int, issue: DeckIssue) -> list:
    """The moves that would settle a cross-slide position mismatch.

    THE MODEL SAYS WHICH SLIDES DISAGREE; ARITHMETIC SAYS WHERE THE SHAPE
    BELONGS. Asking the model where to put it would be asking a picture for a
    measurement, which is the one thing this whole layer refuses to do. The
    target is the median position of the same shape across the deck -- median
    rather than mean because one slide that is wildly out should not drag the
    answer towards itself, which is exactly the slide being reported.

    TITLES ONLY, and that is a real limit rather than an oversight. A title is
    the one shape a deck has on nearly every slide, in a role this tool can
    identify without being told, so "where the rest of the deck puts it" means
    something. For a logo that drifts or a footer that moves there is no such
    set to measure against, and the finding stays a task.

    Empty whenever the deck cannot answer: no profile, fewer than three slides
    with titles, or a slide already where the median says it should be. The
    task is then not fixable and reads as one, which is the honest result.
    """
    from .apply.qafix import Step  # noqa: PLC0415 - COM-side module

    if issue.kind not in _POSITION_KINDS or report.profile is None:
        return []

    titles = {
        slide.number: title
        for slide in report.profile.slides
        if (title := _title_of(slide)) is not None
    }
    if len(titles) < _ALIGN_QUORUM:
        return []

    lefts = sorted(t.geometry.left_in for t in titles.values())
    tops = sorted(t.geometry.top_in for t in titles.values())
    left, top = _median(lefts), _median(tops)

    steps = []
    for number in issue.slides:
        title = titles.get(number)
        if title is None:
            continue
        if (abs(title.geometry.left_in - left) < _ALIGN_FLOOR_IN
                and abs(title.geometry.top_in - top) < _ALIGN_FLOOR_IN):
            continue
        steps.append(Step(
            op="align",
            slide=number,
            shape_id=title.shape_id,
            shape=title.name,
            path=_path_of(report.profile, number, title),
            left_in=round(left, 3),
            top_in=round(top, 3),
            task_id=f"deck:{index}",
            note=issue.note,
        ))
    return steps


# How a within-slide arrangement is described once it has been made, for the
# sentence the page shows. The cross-slide `align` has its own wording and
# keeps it as the default on `Step`.
_ARRANGE_WORDS = {
    "align_top": "level with the others, on their top edge",
    "align_bottom": "level with the others, on their bottom edge",
    "align_left": "in line with the others, on their left edge",
    "align_right": "in line with the others, on their right edge",
    "distribute_h": "an equal gap from the shapes either side of it",
    "distribute_v": "an equal gap from the shapes above and below it",
    "center_h": "centred across the width of the slide with the rest of its set",
    "same_width": "the width most of its set already is",
    "same_height": "the height most of its set already is",
}


def _arrange_steps(
    report: DesignQaReport, number: int, index: int, issue: SlideIssue
) -> list:
    """The moves that would settle a set of shapes on one slide.

    THE SAME SPLIT AS `_align_steps`, one level down. There the model says
    which slides disagree and the deck says where the shape belongs; here it
    says which shapes on a slide should relate and how, and the slide says
    where. Neither asks a picture for a measurement, which is the rule this
    whole layer is built on.

    Empty whenever the slide cannot answer, and an empty list is what makes
    the task read as one for a designer:

      - the finding is not a relation, or names too few shapes for one
      - a named shape is no longer findable in the profile, or has no geometry
      - every named shape is already where the arrangement puts it
      - the shapes would have to overlap to be evenly spaced, which means the
        set is not the row it was taken for

    Note the last one. A distribution whose members are wider than the space
    they sit in is arithmetic that still produces an answer -- equal NEGATIVE
    gaps -- and it would pile the shapes on top of each other tidily. The
    honest reading is that the model named the wrong set, so nothing moves.
    """
    from .apply.qafix import Step  # noqa: PLC0415 - COM-side module

    if report.profile is None or not issue.addressable:
        return []

    placed: list[tuple[Any, Any]] = []
    for member in issue.members:
        if member.shape_id is None:
            continue
        shape = _shape_at(report.profile, number, member.path)
        if shape is None or shape.geometry is None:
            continue
        placed.append((member, shape.geometry))
    if len(placed) < _ARRANGE_MIN[issue.arrangement]:
        return []

    # Each row on its own terms. See `_lines` for why the set is split here
    # rather than taken as one.
    # A SIZE IS NOT A POSITION, so it is not split into rows first. Six cards
    # in two rows of three should all be one width; taking each row's own
    # median would answer a question nobody asked and leave the two rows
    # disagreeing with each other.
    if issue.arrangement == "same_type_size":
        return _type_steps(report, number, index, issue)
    if issue.arrangement == "same_text_format":
        return _format_steps(report, number, index, issue)
    if issue.arrangement in _SIZE_ARRANGEMENTS:
        return _resize_steps(report, number, index, issue, placed)

    targets = []
    for line in _lines(issue.arrangement, placed):
        if len(line) < _ARRANGE_MIN[issue.arrangement]:
            continue
        if _shares_the_other_edge(issue.arrangement, line):
            continue
        if issue.arrangement.startswith("align_"):
            targets.extend(_aligned(issue.arrangement, line))
        elif issue.arrangement == "center_h":
            # The profile's width rather than the report's: they are the same
            # number, and this function already refuses to run without the
            # profile, so taking it from there cannot be reached with one set
            # and not the other.
            targets.extend(_centered(line, report.profile.width_in))
        else:
            targets.extend(_distributed(issue.arrangement, line))

    steps = []
    for member, box, left, top in targets:
        # Only the axis the arrangement is about carries a number; the other
        # is None and the applier leaves it alone. See `_aligned`.
        travel = max(
            abs(box.left_in - left) if left is not None else 0.0,
            abs(box.top_in - top) if top is not None else 0.0,
        )
        if travel < _ARRANGE_FLOOR_IN:
            continue
        steps.append(Step(
            op="align",
            slide=number,
            shape_id=member.shape_id,
            shape=member.shape,
            path=member.path,
            left_in=None if left is None else round(left, 3),
            top_in=None if top is None else round(top, 3),
            measured_off=_ARRANGE_WORDS[issue.arrangement],
            task_id=f"slide:{number}:{index}",
            note=issue.note,
        ))
    return steps


# The arrangements whose set runs ACROSS the slide. The rest run down it, and
# the only thing that changes between the two is which axis is which.
_ROW_ARRANGEMENTS = {"align_top", "align_bottom", "distribute_h", "center_h"}


def _lines(kind: str, placed: Sequence[tuple]) -> list[list[tuple]]:
    """The named set, split into the rows -- or columns -- it actually holds.

    THE SET THE MODEL NAMES IS NOT ALWAYS ONE LINE, AND THAT IS NOT ITS
    MISTAKE TO FIX. Shown ten circles in two rows of five with one circle
    sitting low, a model reports what a person would: the circles do not line
    up. Asking it to work out that this is really a statement about five of
    them is asking a picture to be partitioned, which is measurement -- the
    one thing this layer never asks for. The file can partition it exactly.

    Taking the set whole is what the first version did, and it wrecked the
    slide. The median top of ten circles in two rows falls BETWEEN the rows,
    so every circle is a little over an inch from it, every move passes the
    two-inch sanity check in `qafix._align` on its own, and both rows end up
    collapsed on one line. No per-move guard can catch that, because no single
    move is unreasonable. Only the set is.

    Refusing the whole set was the next version, and it was safe and useless:
    the commonest way for a model to describe this slide became the one way
    to get nothing done.

    So the set is split and each line corrected on its own terms. A row is
    shapes that OVERLAP VERTICALLY -- a horizontal line can be drawn through
    all of them -- and a column is shapes that overlap horizontally. Ten
    circles in two rows split into two rows of five, the low circle rejoins
    the four it belongs with, and the row below it is measured separately and
    does not move at all.

    Greedy, in order, and it needs no tolerance to be chosen: a shape opens a
    new line when it starts past where every shape in the current one ends.
    The shapes' own sizes set the scale, which is the right scale -- how far
    out of line a shape can drift before it stops being part of the row
    depends on how big the row is. A circle 0.3in low still shares most of its
    height with its neighbours and stays in. One dropped clear of them does
    not, and is left alone rather than dragged back.
    """
    row = kind in _ROW_ARRANGEMENTS
    start = (lambda b: b.top_in) if row else (lambda b: b.left_in)
    extent = (lambda b: b.height_in) if row else (lambda b: b.width_in)

    lines: list[list[tuple]] = []
    current: list[tuple] = []
    edge = 0.0
    for member, box in sorted(placed, key=lambda pair: start(pair[1])):
        if current and start(box) >= edge:
            lines.append(current)
            current = []
        current.append((member, box))
        end = start(box) + extent(box)
        edge = end if len(current) == 1 else min(edge, end)
    if current:
        lines.append(current)
    return lines


# The edge OPPOSITE the one each alignment asks for. Used to refuse an
# arrangement, never to make one: see `_shares_the_other_edge`.
_OTHER_EDGE = {
    "align_top": lambda b: b.top_in + b.height_in,
    "align_bottom": lambda b: b.top_in,
    "align_left": lambda b: b.left_in + b.width_in,
    "align_right": lambda b: b.left_in,
}

# How close two edges have to be before a set counts as already sharing one.
# A row drawn once and copied across a slide agrees to the thousandth of an
# inch; a set that merely looks close does not. Loose enough to survive the
# rounding a deck carries, tight enough that it never describes a row anybody
# would call ragged.
_SHARED_EDGE_IN = 0.02


def _shares_the_other_edge(kind: str, placed: Sequence[tuple]) -> bool:
    """Whether the set already lines up on the edge opposite the one asked for.

    A SET CANNOT SHARE BOTH EDGES OF AN AXIS UNLESS ITS SHAPES ARE THE SAME
    SIZE ALONG IT. So a row that already agrees on its tops and is asked to
    agree on its bottoms can only be granted by pulling every box off the top
    line it is on, and what comes back is a row out of line in the other
    direction. The shapes differ in HEIGHT, and no move changes a height.

    Which is what a real row of four column headings did. Four text boxes at
    the same top and the same width, one of them 0.28in taller because its
    heading wraps to a second line where the other three sit on one. The model
    read the render, saw three headings sharing a baseline and the fourth not,
    and asked for `align_bottom` -- a fair reading of a picture, and the only
    reading available to something that cannot see the boxes. Applied, it
    lifted the two-line box 0.28in clear of the row's top edge and up over the
    icon above it, breaking the one alignment the row actually had.

    THE RULE LAYER ALREADY HAS THE ANSWER FOR THAT ROW AND IT IS NOT A MOVE.
    `typography.heading_balance` squares a row of parallel headings up by
    breaking the short ones across as many lines as the longest takes: the
    raggedness is in the copy, so the copy is where it is corrected, and every
    box stays exactly where it was drawn. A check that answers the same slide
    with a move is undoing the layout to chase the symptom.

    So the line is refused here rather than corrected. It stays on the list as
    work, which is the honest place for it while nothing on this side counts
    lines.
    """
    other = _OTHER_EDGE.get(kind)
    if other is None:
        return False
    edges = [other(box) for _, box in placed]
    return max(edges) - min(edges) <= _SHARED_EDGE_IN


def _anchor(kind: str, placed: Sequence[tuple]):
    """The member the rest of the set is brought into line with.

    THE FIRST BOX, READ THE WAY THE SET IS READ, and not the median the first
    version took. The median is the better statistic and it was the wrong
    answer, because the line it produces is a line NOBODY DREW: a row of three
    column headings whose tops are 2.36, 2.36 and 2.35in has a median of
    2.36in and a set of five whose tops straddle a title has a median
    somewhere between them, and on a real deck that median sat above the
    heading band and walked the whole left column up into the title. A set is
    levelled against one of its own members or it is levelled against an
    abstraction, and only the first of those can be checked by looking at the
    slide.

    FIRST MEANS FIRST ALONG THE LINE, which is the axis the arrangement does
    NOT change: a row being levelled is read left to right, so its anchor is
    its leftmost member; a column being lined up is read top to bottom, so its
    anchor is its topmost. `_lines` has already split the set into the rows or
    columns it really holds, so this is asked of one line at a time and a grid
    anchors each of its rows on that row's own first card.
    """
    along = (lambda box: box.left_in) if kind in _ROW_ARRANGEMENTS else (
        lambda box: box.top_in
    )
    return min(placed, key=lambda pair: along(pair[1]))[1]


def _aligned(kind: str, placed: Sequence[tuple]) -> list[tuple]:
    """Where each shape goes to share an edge with the rest of its set.

    THE FIRST BOX'S EDGE, taken by `_anchor`, which explains why it is not the
    median it used to be: a median is an edge no shape on the slide actually
    has, and correcting a set onto one moves every member of it -- including
    the ones that were right -- to a line the designer never drew.

    ONLY THE AXIS THE ARRANGEMENT IS ABOUT CARRIES A NUMBER. The other comes
    back None, and the applier leaves that axis exactly where it is.

    This is not tidiness, it is the difference between the feature working and
    the feature undoing itself. The first version returned the shape's CURRENT
    position for the axis it was not changing, which looks harmless: writing a
    value back where it already was should do nothing. It does nothing only
    while that value is still current. A slide with two findings on it -- level
    this row, and space it evenly -- produces two rounds of steps, both
    measured off the deck as it was read. The first lifts a circle 0.16in. The
    second, carrying the circle's ORIGINAL top as the axis it "was not
    changing", puts it straight back down. Observed on a real deck, in this
    order, in one round:

        Oval 7  level with the others, on their top edge  (-0.00in, -0.16in)
        Oval 7  an equal gap from the shapes either side  (-0.12in, +0.16in)

    Two corrections, both applied, both reported, and the net vertical
    movement is zero. Nothing refused it because nothing was wrong with either
    step on its own.
    """
    anchor = _anchor(kind, placed)
    if kind == "align_top":
        edge = anchor.top_in
        return [(m, b, None, edge) for m, b in placed]
    if kind == "align_bottom":
        edge = anchor.top_in + anchor.height_in
        return [(m, b, None, edge - b.height_in) for m, b in placed]
    if kind == "align_left":
        edge = anchor.left_in
        return [(m, b, edge, None) for m, b in placed]
    edge = anchor.left_in + anchor.width_in
    return [(m, b, edge - b.width_in, None) for m, b in placed]


# The arrangements that change a shape's SIZE rather than its position, and
# the only ones here that do. Everything else moves things.
_SIZE_ARRANGEMENTS = frozenset({"same_width", "same_height"})

# How far a shape may be resized to join its set. A copy of a component that
# drifted is within a few per cent of its siblings; something a third out is
# not a copy that drifted, it is a different element, and making it match would
# be redrawing the slide rather than tidying it.
_RESIZE_LIMIT = 0.34

# And the floor under it, in inches, so a set of small shapes is not resized on
# a rounding difference nobody can see.
_RESIZE_FLOOR_IN = 0.02


def _type_steps(
    report: DesignQaReport, number: int, index: int, issue: SlideIssue
) -> list:
    """Set a named group of headings to the size most of them already are.

    THE SAME ARITHMETIC AS THE CROSS-SLIDE `type_scale` FIX AND A DIFFERENT
    BLAST RADIUS, which is the whole reason this exists separately. That one
    takes every text shape on the slides a mismatch names, grouped by role, and
    moves them towards a majority counted over the deck -- and on a deck that
    disagrees with itself it rewrites the deck, which is why `_capped` now
    refuses it past a share. This one corrects the shapes the model POINTED AT
    and takes the majority from those shapes alone. The set is named, so the
    blast radius is what somebody looked at.

    THE MOST COMMON SIZE, not the median. A size is a chosen value rather than
    a measurement: four headings at 22, 26, 18 and 20pt have no majority and
    their median is 21, which is a size nobody typed and no other heading on
    the deck uses. Where there is a majority it is the answer; where there is
    not, the set is left alone and a designer picks.
    """
    from .apply.qafix import Step  # noqa: PLC0415 - COM-side module

    sizes: dict = {}
    for member in issue.members:
        if member.shape_id is None:
            continue
        shape = _shape_at(report.profile, number, member.path)
        if shape is None:
            continue
        found = _sizes_of(shape)
        # One size per shape. A heading whose runs disagree with each other is
        # a different defect and not one a majority across the set can settle.
        if len(set(found)) == 1:
            sizes[member.shape_id] = (member, found[0])

    if len(sizes) < _ARRANGE_MIN["same_type_size"]:
        return []
    target = _most_common([size for _member, size in sizes.values()])
    if target is None:
        return []
    # A majority, not a plurality. Four sizes on four headings has a "most
    # common" only by tie-break, and setting a row to a size one of them
    # happens to be is a coin toss somebody has to look at anyway.
    agreed = sum(1 for _m, size in sizes.values() if abs(size - target) < 0.51)
    if agreed * 2 <= len(sizes):
        return []

    steps = []
    for member, size in sizes.values():
        if abs(size - target) < 0.51:
            continue
        steps.append(Step(
            op="set_size",
            slide=number,
            shape_id=member.shape_id,
            shape=member.shape,
            path=member.path,
            size_pt=target,
            measured_off=f"the {target:g}pt most of its set is set at",
            task_id=f"slide:{number}:{index}",
            note=issue.note,
        ))
    return steps


def _format_steps(
    report: DesignQaReport, number: int, index: int, issue: SlideIssue
) -> list:
    """Set the text of each named sibling the way the reference is set.

    THE MODEL PICKS THE REFERENCE, NOT A COUNT, and that is what this adds to
    `_type_steps`. Two cards whose body copy is 12pt in one and 7pt in the
    other have no majority, so the counting arrangements are silent on the
    commonest version of this defect there is. The model is looking at the
    slide and can say which of the two reads as intended; the file then says
    what that one is set at, and PowerPoint copies it and checks the copy
    still fits (`qafix._match_format`).

    A member whose runs already state exactly what the reference states is
    left out, so a set that agrees produces no step rather than a refusal.
    """
    from .apply.qafix import Step  # noqa: PLC0415 - COM-side module

    reference = issue.reference
    if reference is None or reference.shape_id is None or report.profile is None:
        return []
    source = _shape_at(report.profile, number, reference.path)
    if source is None:
        return []
    wanted = _format_of(source)

    steps = []
    for member in issue.members:
        if member.shape_id is None or member.ref == reference.ref:
            continue
        shape = _shape_at(report.profile, number, member.path)
        if shape is None:
            continue
        if wanted is not None and _format_of(shape) == wanted:
            continue
        steps.append(Step(
            op="match_format",
            slide=number,
            shape_id=member.shape_id,
            shape=member.shape,
            path=member.path,
            parent_id=reference.shape_id,
            parent=reference.shape,
            parent_path=reference.path,
            measured_off=f"{reference.shape!r}, the one its set should look like",
            shared_fit=len(issue.members) == 2,
            task_id=f"slide:{number}:{index}",
            note=issue.note,
        ))
    return steps


def _format_of(shape: ShapeProfile) -> Optional[frozenset]:
    """What a shape's text is set at, when every run says; None otherwise.

    None where a run inherits anything, because an unstated value cannot be
    compared from the file -- only PowerPoint knows what it draws.
    """
    seen = set()
    for paragraph in shape.paragraphs:
        for run in paragraph.runs:
            if not run.text.strip():
                continue
            if not run.size_pt or not run.font_name or not run.color_hex:
                return None
            seen.add((run.size_pt, run.font_name, run.color_hex.upper()))
    return frozenset(seen) if seen else None


def _resize_steps(
    report: DesignQaReport,
    number: int,
    index: int,
    issue: SlideIssue,
    placed: Sequence[tuple],
) -> list:
    """The resizes that would make a set of shapes one size.

    THE MEDIAN, for the reason every other arrangement here takes one: the
    shapes that agree are what says what the size should be, and a mean lets
    the odd one drag the answer towards itself. Six cards at 3.6, 3.6, 3.4,
    3.6, 3.75 and 3.4 are a set of 3.6in cards with three that drifted, and the
    median says so where the mean says 3.55 and resizes all six.

    ONLY THE AXIS THE ARRANGEMENT NAMES. `same_width` does not touch a height,
    because a row of cards holding different amounts of copy is allowed to be
    different heights and usually should be.

    THE SHAPE'S POSITION IS ITS LEFT AND TOP, which is what resizing from the
    top-left means and is what PowerPoint does. A card that grows grows to the
    right; the row's left edges, which were its one good alignment, are the
    thing this must not disturb.
    """
    from .apply.qafix import Step  # noqa: PLC0415 - COM-side module

    wide = issue.arrangement == "same_width"
    sizes = sorted(
        (box.width_in if wide else box.height_in) for _member, box in placed
    )
    target = _median(sizes)
    if target <= 0:
        return []

    steps = []
    for member, box in placed:
        was = box.width_in if wide else box.height_in
        if was <= 0 or abs(was - target) < _RESIZE_FLOOR_IN:
            continue
        if abs(was - target) > target * _RESIZE_LIMIT:
            # Not a copy that drifted. Refused for the SET rather than for this
            # shape alone: a row corrected with one member left out is a row
            # that still does not match, reported as done.
            return []
        steps.append(Step(
            op="resize",
            slide=number,
            shape_id=member.shape_id,
            shape=member.shape,
            path=member.path,
            width_in=round(target, 3) if wide else None,
            height_in=None if wide else round(target, 3),
            measured_off=(
                f"the {'width' if wide else 'height'} most of its set already "
                "is"
            ),
            task_id=f"slide:{number}:{index}",
            note=issue.note,
        ))
    return steps


def _centered(placed: Sequence[tuple], width_in: float) -> list[tuple]:
    """The set moved as one piece until it sits centred across the slide.

    THE ONLY ARRANGEMENT MEASURED OFF THE SLIDE RATHER THAN OFF THE SET, and
    the only one where every member moves by the same amount. That is what
    makes it a different thing from `_distributed` rather than a variation on
    it: distributing rearranges the inside of a row and leaves its ends where
    they are, and this leaves the inside of the row exactly as drawn and moves
    the whole of it. Three cards with a designed gap between them stay three
    cards with that gap.

    Which is the case it exists for. A row built on a four-column grid and
    filled with three cards sits left, with an empty column beside it that
    reads as a mistake rather than as space. Nothing about the three cards is
    wrong -- their sizes, their gaps and their alignment are all as drawn --
    so no relation between them describes the defect, and it arrived as prose
    until there was a word for it.

    Nothing moves where the slide has no width to centre in, which is a deck
    that could not be read, and nothing moves where centring would carry the
    set further than `qafix._align` will take a shape. That guard is a move
    this tool declines to make on its own, and running into it here rather
    than there is what keeps the refusal honest: the sentence `_align` gives
    is about a shape measured against the wrong neighbours, which is not what
    happened. Stopped here, the finding stays a task and a designer reads the
    model's own instruction, which said to centre them.
    """
    if width_in <= 0:
        return []
    order = sorted(placed, key=lambda pair: pair[1].left_in)
    first, last = order[0][1], order[-1][1]
    span = (last.left_in + last.width_in) - first.left_in
    if span <= 0 or span > width_in:
        return []

    shift = (width_in - span) / 2 - first.left_in
    if abs(shift) > _CENTER_LIMIT_IN:
        return []
    # The cross axis is None, as everywhere else here: centring a row across
    # the slide says nothing about how far down the slide it sits. See
    # `_aligned` for the round that distinction cost.
    return [(member, box, box.left_in + shift, None) for member, box in order]


def _distributed(kind: str, placed: Sequence[tuple]) -> list[tuple]:
    """Equal gaps across the set, with the two outermost shapes left alone.

    THE ENDS ANCHOR IT because they are what the set's extent means. Moving
    them would change how much of the slide the row occupies, and how wide a
    row of cards should be is a composition decision somebody made; how the
    space inside it is divided is not.

    EQUAL GAPS BETWEEN BOXES, not equal spacing of centres. The two are the
    same only when every shape is the same size, and when they are not, evenly
    spaced centres is the arrangement that looks wrong -- a wide card and a
    narrow one reading as unevenly spaced because the white between them is.
    """
    horizontal = kind == "distribute_h"
    start = (lambda b: b.left_in) if horizontal else (lambda b: b.top_in)
    extent = (lambda b: b.width_in) if horizontal else (lambda b: b.height_in)

    order = sorted(placed, key=lambda pair: start(pair[1]))
    first, last = order[0][1], order[-1][1]
    span = (start(last) + extent(last)) - start(first)
    gap = (span - sum(extent(box) for _, box in order)) / (len(order) - 1)
    if gap < 0:
        return []

    out, cursor = [], start(first)
    for member, box in order:
        # The cross axis is None, not the shape's current position. See
        # `_aligned` for the round this distinction cost.
        out.append((
            member, box,
            cursor if horizontal else None,
            None if horizontal else cursor,
        ))
        cursor += extent(box) + gap
    return out


def _shape_at(profile: Any, number: int, path: Sequence[int]) -> Optional[ShapeProfile]:
    """The shape a path addresses, walking into groups.

    The counterpart of `_path_of`, which only ever had to describe a top-level
    shape because only titles were aligned. An arrangement names whatever the
    model saw, and what it saw is often one icon inside each of five groups.
    """
    if not path:
        return None
    for slide in profile.slides:
        if slide.number != number:
            continue
        shapes, found = slide.shapes, None
        for position in path:
            if not 1 <= position <= len(shapes):
                return None
            found = shapes[position - 1]
            shapes = found.children
        return found
    return None


def _path_of(profile: Any, number: int, wanted: ShapeProfile) -> tuple:
    """Where this shape sits in its slide's tree, 1-based.

    The same addressing the shape map uses -- see `ai.designqa.Listed` for why
    an id is not enough to find a shape again through PowerPoint.
    """
    for slide in profile.slides:
        if slide.number != number:
            continue
        for position, shape in enumerate(slide.shapes, 1):
            if shape is wanted:
                return (position,)
    return ()


def _title_of(slide: Any) -> Optional[ShapeProfile]:
    for shape in slide.shapes:
        if getattr(shape.role, "value", "") == "title":
            return shape
    return None


def _median(values: Sequence[float]) -> float:
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2
