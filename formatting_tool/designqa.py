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
        """
        out: list[Task] = []
        for index, issue in enumerate(self.deck_issues):
            out.append(self._deck_task(index, issue))
        for review in self.reviews:
            for verdict in review.verdicts:
                if verdict.status != "issue":
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
                    op="align" if steps else "",
                ))
        return out

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
    report.spec = _own_spec(profile)

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
    aligned = {change.slide for change in (applied or ()) if change.op == "align"}
    # Proposals come back identified by the finding they were made on, since
    # that is how the shared applier reports an outcome.
    by_id = {getattr(change, "task_id", "") for change in (applied or ())}

    left: list[Task] = []
    for task in report.tasks:
        if not task.fixable or task.id not in ticked:
            left.append(task)
            continue
        if task.id in by_id:
            continue
        if task.kind == "slide":
            # An arrangement is several moves filed under one id, and any of
            # them landing is the row having been acted on -- which the check
            # against `by_id` above has already settled. Reaching here means
            # every move in the set was refused, so the task stands and says
            # so, rather than reading as done because it was ticked.
            left.append(task)
        elif task.kind == "deck":
            # A mismatch is answered by several corrections, one per shape,
            # each carrying the task's id as a prefix. Any of them landing is
            # the row having been acted on; the rest are reported as refusals
            # beside it.
            if not (any(number in aligned for number in task.slides)
                    or any(key.startswith(f"{task.id}:") for key in by_id)):
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
            steps.extend(_align_steps(report, issue))
    # Slide-level arrangements come off `report.reviews` rather than off the
    # narrowed copy above, which drops `slide_issues` because `steps_from`
    # has never read them.
    for review in report.reviews:
        for index, issue in enumerate(review.slide_issues):
            if wanted is None or f"slide:{review.slide}:{index}" in wanted:
                steps.extend(_arrange_steps(report, review.slide, index, issue))
    return steps


def _deck_fixes(
    report: DesignQaReport, index: int, issue: DeckIssue
) -> tuple[list, list[Issue]]:
    """What would settle a cross-slide mismatch: moves, or values, or nothing.

    THE MODEL SAYS WHICH SLIDES DISAGREE AND ON WHAT. THE DECK SAYS WHAT THE
    MAJORITY DOES. That split is the whole design of this function, and it is
    what makes a mismatch fixable at all: "set the body copy on slide 3 to
    match the rest" has a number behind it, and the number is one nobody has
    to be trusted for -- it is counted off the other slides.

    Three kinds of mismatch have an answer that can be counted:

      position    the shape belongs where most slides put it -- a move, taken
                  through PowerPoint because `align` is measured (`_align_steps`)
      type_scale  the text belongs at the size most slides set for its role
      color       the text belongs in the colour most slides give that role

    The rest come back empty and stay tasks. `spacing` has no single number to
    count, `content` is a missing element rather than a wrong value, and
    `other` is by definition not a category with an answer in it.

    What comes back for the last two is `Issue` objects carrying a `FixAction`,
    so they go through `apply.fixers.fix_ai_action` and its checks: a size has
    to sit in the range this deck uses for that role, a colour has to be one of
    this deck's own. A majority counted off a deck that disagrees with itself
    everywhere will not pass those, which is the correct outcome.
    """
    if report.profile is None:
        return [], []
    if issue.kind == "position":
        return _align_steps(report, issue), []
    if issue.kind == "type_scale":
        return [], _size_fixes(report, index, issue)
    if issue.kind == "color":
        return [], _color_fixes(report, index, issue)
    return [], []


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


def _align_steps(report: DesignQaReport, issue: DeckIssue) -> list:
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

    if issue.kind != "position" or report.profile is None:
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
    targets = []
    for line in _lines(issue.arrangement, placed):
        if len(line) < _ARRANGE_MIN[issue.arrangement]:
            continue
        targets.extend(
            _aligned(issue.arrangement, line)
            if issue.arrangement.startswith("align_")
            else _distributed(issue.arrangement, line)
        )

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
_ROW_ARRANGEMENTS = {"align_top", "align_bottom", "distribute_h"}


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


def _aligned(kind: str, placed: Sequence[tuple]) -> list[tuple]:
    """Where each shape goes to share an edge with the rest of its set.

    THE MEDIAN OF THE EDGE THEY SHOULD SHARE, for the reason `_align_steps`
    gives one level up: the shape being reported is the one that is out, and a
    mean lets it drag the line it is supposed to be joining towards itself.
    With four circles level and a fifth low, the median IS the line the four
    are on, which is why the model is told to name the whole set and not just
    the odd one out.

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
    if kind == "align_top":
        edge = _median(sorted(box.top_in for _, box in placed))
        return [(m, b, None, edge) for m, b in placed]
    if kind == "align_bottom":
        edge = _median(sorted(box.top_in + box.height_in for _, box in placed))
        return [(m, b, None, edge - b.height_in) for m, b in placed]
    if kind == "align_left":
        edge = _median(sorted(box.left_in for _, box in placed))
        return [(m, b, edge, None) for m, b in placed]
    edge = _median(sorted(box.left_in + box.width_in for _, box in placed))
    return [(m, b, edge - b.width_in, None) for m, b in placed]


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
