"""The design check: what a slide looks like, and the one thing that fixes it.

A second pipeline, deliberately beside `pipeline.run` rather than inside it.
The validation pipeline measures a deck against a master and a brand file and
reports what is provably wrong with it -- a colour off the palette, a box off
the grid, a typeface nobody approved. It needs both files and it answers in
rules.

This one needs neither. It takes a deck on its own, renders every slide through
PowerPoint, and asks a model the question the rules cannot reach: does this
look right. What comes back is a verdict per shape and a list of what is wrong
with the slide as a whole, and exactly one of those verdicts is executable --
step this shape's type up or down, bounded, once (`apply.qafix`).

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
from .ai.designqa import DeckIssue, SlideReview, review_consistency, review_slides
from .extract import read_deck
from .models import DeckProfile, ShapeProfile

log = logging.getLogger(__name__)


# How far off the deck's usual position a shape has to be before moving it is
# worth doing, and how many slides have to agree before "usual" means anything.
# A quarter of a line of body copy is visible when slides are flicked through;
# two slides agreeing is not a convention, it is a coincidence.
_ALIGN_FLOOR_IN = 0.05
_ALIGN_QUORUM = 3


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
                    op=verdict.action if verdict.executable else "",
                    box=verdict.box,
                ))
            for index, issue in enumerate(review.slide_issues):
                out.append(Task(
                    id=f"slide:{review.slide}:{index}",
                    kind="slide",
                    what=issue.task or issue.note,
                    why=issue.note if issue.task else "",
                    slide=review.slide,
                ))
        return out

    def _deck_task(self, index: int, issue: DeckIssue) -> Task:
        """One cross-slide mismatch as work, fixable where arithmetic can say
        where the shape belongs -- see `_align_steps`."""
        steps = _align_steps(self, issue)
        return Task(
            id=f"deck:{index}",
            kind="deck",
            what=issue.task or issue.note,
            why=issue.note if issue.task else "",
            slides=list(issue.slides),
            slide=issue.slides[0] if issue.slides else None,
            issue=issue.kind,
            fixable=bool(steps),
            op="align" if steps else "",
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

    left: list[Task] = []
    for task in report.tasks:
        if not task.fixable or task.id not in ticked:
            left.append(task)
            continue
        if task.kind == "deck":
            if not any(number in aligned for number in task.slides):
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
    return steps


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
