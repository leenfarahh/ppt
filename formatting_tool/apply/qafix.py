"""The corrections the design check is allowed to make on its own.

Four, and the list is short for a reason that has not changed: each one has to
be ARITHMETIC, BOUNDED and CHECKABLE AFTERWARDS. A model looking at a render is
good at "this caption is too small to read" and bad at "this box is 0.31in too
far left", so it is given the actions its judgement supports and no way to
express any other. Everything else it notices becomes a task -- see
`designqa.DesignQaReport.tasks` -- and tasks are written into the deck as
comments rather than dropped.

    shrink / grow   step the type one notch, x0.85 or x1.15, floored at 9pt and
                    capped at 40pt, rolled back if the copy stops fitting.
    center          move a shape so its centre matches the centre of the shape
                    that holds it. Moves only; never resizes; refuses when the
                    shape is not inside anything.
    widen           widen a text box until PowerPoint stops breaking its words
                    in half, in steps, stopping at a neighbour, at the slide
                    edge, or at a cap, and rolled back if the break survives.
    narrow          its mirror: pull a text box in until its copy stops running
                    behind whatever sits over its right-hand end, stopping at
                    that shape or at a floor, and rolled back if the copy stops
                    fitting the shorter measure.
    send_to_back    put a shape behind everything else on its slide. The one
                    correction here that moves nothing.
    align           move a shape to a position measured off other shapes. Two
                    findings arrive as this one op. A title that sits 4% lower
                    than on every other slide is measured off the deck; a
                    circle that breaks the top edge of its row, or a row whose
                    gaps are uneven, is measured off the rest of the set on its
                    own slide. Either way the target is computed before this
                    module runs (`designqa._align_steps` and `_arrange_steps`)
                    and never asked for: the model says which shapes disagree,
                    arithmetic says where they belong. `measured_off` carries
                    which of the two it was, for the sentence shown afterwards.

ONE ROUND, NO RE-CHECK. A loop that keeps correcting until the model is happy
converges on a deck nobody chose, so every step is taken once and the result is
two pictures the designer accepts or rejects.

DRIVEN THROUGH POWERPOINT, not python-pptx, and that is deliberate. A
placeholder usually states no size of its own: it inherits one from the layout,
`run.font.size` is then None, and that is most of the shapes on a tidy deck.
PowerPoint answers with the size it is actually drawing, and setting it writes
the explicit value. The same open presentation then measures what the change
did -- where the text now breaks, whether it still fits its box -- on the
engine that will render the deck for the client.

The original upload is never touched: the deck is copied to `out` first and
every edit happens on the copy.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .. import powerpoint

log = logging.getLogger(__name__)

# One type step, up or down. Roughly a sixth either way: big enough to see on a
# render, small enough that a wrong call is not a disfigured slide.
GROW_FACTOR = 1.15
SHRINK_FACTOR = 0.85

# The bounds on type. 40pt is display type on a 16:9 slide and there is no
# reading problem a bigger step solves; 9pt is the floor a client deck is still
# legible at, and shrinking past it hides an overflow rather than fixing one.
CEILING_PT = 40.0
FLOOR_PT = 9.0

# Type is set in half points. Rounding finer produces sizes no designer would
# type and that no two shapes ever share.
_STEP_PT = 0.5

# How much the drawn text may exceed its box before a type step is taken back,
# as a share of the box. Not zero: PowerPoint reports the bounds of the text
# including its line leading, so copy that fits exactly measures a hair over.
_SPILL_SLACK = 0.02

# Widening: how much wider each attempt makes the box, how many attempts, and
# the ceiling on the total. A box that needs to be two-thirds wider than it was
# drawn is not a box with a wrapping problem, it is a layout somebody has to
# look at, and widening it that far would walk across the slide.
_WIDEN_STEP = 1.12
_WIDEN_TRIES = 5
_WIDEN_CAP = 1.6

# The gap left between a widened box and whatever is beside it, in points. A
# box grown flush against its neighbour reads as a collision even when the
# rectangles do not touch.
_CLEARANCE_PT = 4.0

# How far `align` will move a shape. The target is measured off the rest of the
# deck, so a large move means the shape being aligned is not the shape the
# measurement describes -- a title on a cover, a heading that belongs where it
# is -- and moving it would be worse than leaving it.
_ALIGN_LIMIT_IN = 2.0
_POINTS_PER_INCH = 72.0

# Long enough for a big deck on a slow disk, short enough that a wedged
# PowerPoint fails the request rather than the page.
_TIMEOUT_S = 300

_MSO_GROUP = 6

# What PowerPoint answers for a property whose value is not the same throughout
# the range -- a shape whose paragraphs are set at three sizes. There is no
# single size to step from, so the shape is left alone and said to be.
_MIXED = -2.0


# --------------------------------------------------------------------------- #
# What goes in and what comes out
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Step:
    """One correction to make, named the only way that is unambiguous.

    `op` is from `ai.designqa.ACTIONS` plus `align`, which no model asks for --
    it is derived from a mismatch the model named and a measurement taken off
    the file (see `designqa.steps_for`).
    """

    op: str                       # shrink | grow | center | widen | align
    slide: int                    # 1-based
    shape_id: int                 # the id the report addressed it by
    shape: str                    # the name, which is how a lookup is checked
    # Where the shape sits in the shape tree, 1-based, outermost first. See
    # `_find` for why the id alone is not enough to find it again.
    path: tuple[int, ...] = ()
    parent_path: tuple[int, ...] = ()
    # `center` needs the shape that holds this one; `align` needs where to put
    # it, in inches from the top left of the slide. Both are refused without.
    #
    # AN ALIGN MAY NAME ONE AXIS AND LEAVE THE OTHER None, and that is how a
    # within-slide arrangement says what it is about: levelling a row sets
    # tops and must not touch where along the row anything sits. An axis left
    # None is not written, which is different from writing back the value it
    # already had -- by the time a later step in the same round is applied,
    # that value may be the thing an earlier step just corrected.
    parent_id: Optional[int] = None
    parent: str = ""
    left_in: Optional[float] = None
    top_in: Optional[float] = None
    # `resize` needs a size instead, and on ONE axis: a row of cards made one
    # width is still allowed to be different heights, and usually should be.
    # The axis left None is not written, the same way an align's is not.
    width_in: Optional[float] = None
    height_in: Optional[float] = None
    # `set_size` needs a type size, in points. Not a step up or down like
    # `grow` and `shrink`: the number is the one the rest of the named set is
    # already set at, counted in `designqa._type_steps`.
    size_pt: Optional[float] = None
    note: str = ""                # what the model said, carried for the page
    # What `align` measured its target off, in the words the page will show.
    # The default is the cross-slide case the op was written for; a within-
    # slide arrangement sets its own, because "where the rest of the deck puts
    # it" is not true of a circle lined up with the four beside it.
    measured_off: str = "where the rest of the deck puts it"
    # Which row on the page this answers, for a finding that names no single
    # shape and so cannot be matched back by one. Set for an arrangement,
    # empty for everything else, which the page matches by shape and op.
    task_id: str = ""

    @property
    def direction(self) -> str:
        return "grow" if self.op == "grow" else "shrink"


@dataclass(frozen=True)
class Change:
    """One correction that was made, in the words the page will show.

    `task_id` says which row on the page it answers. Set for a correction that
    came from a proposal, since those are applied by `apply.apply_fixes` and
    reported by the finding they were made on; empty for a measured verb,
    which the page matches by shape and op.
    """

    op: str
    slide: int
    shape_id: int
    shape: str
    detail: str
    task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op, "slide": self.slide, "shape_id": self.shape_id,
            "shape": self.shape, "detail": self.detail, "task_id": self.task_id,
        }


@dataclass(frozen=True)
class SkippedStep:
    op: str
    slide: int
    shape_id: int
    shape: str
    reason: str
    task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op, "slide": self.slide, "shape_id": self.shape_id,
            "shape": self.shape, "reason": self.reason, "task_id": self.task_id,
        }


@dataclass
class QaFixResult:
    """What one round did, and where it wrote it.

    `output` is None when nothing was written at all -- no PowerPoint on this
    host, or a deck it would not open -- and `reason` says which. A result with
    an output and an empty `applied` is a different thing: every step was
    refused for a reason that is in `skipped`, and the copy is the deck
    unchanged.
    """

    output: Optional[Path] = None
    applied: list[Change] = field(default_factory=list)
    skipped: list[SkippedStep] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": [c.to_dict() for c in self.applied],
            "skipped": [s.to_dict() for s in self.skipped],
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# The arithmetic, which is the part worth testing
# --------------------------------------------------------------------------- #

def next_size(current: float, grow: bool) -> Optional[float]:
    """The size one bounded step away, or None when there is no step to take.

    None rather than the unchanged size, so a caller cannot write a "change"
    that changed nothing. The three ways there is no step: the size is already
    at the bound it is being pushed towards, the step would not survive
    rounding to the nearest half point, or the size PowerPoint reported is not
    a size at all.
    """
    if current is None or current <= 0:
        return None
    factor = GROW_FACTOR if grow else SHRINK_FACTOR
    wanted = current * factor
    wanted = min(wanted, CEILING_PT) if grow else max(wanted, FLOOR_PT)
    stepped = round(wanted / _STEP_PT) * _STEP_PT
    # A shape already past the bound -- 44pt display type asked to grow -- is
    # pulled to the bound rather than pushed further, which for that shape
    # means shrinking. That is not the step anyone asked for.
    if grow and stepped <= current:
        return None
    if not grow and stepped >= current:
        return None
    return round(stepped, 1)


def centred_on(
    child: tuple[float, float, float, float],
    parent: tuple[float, float, float, float],
) -> Optional[tuple[float, float]]:
    """Where a child has to sit to be centred in its parent, or None.

    None when it is already centred to within half a point, which is below
    what any render shows and is not worth writing to a client's deck.

    Boxes are (left, top, width, height) in whatever unit both share; the
    answer comes back in that unit.
    """
    left, top, width, height = child
    p_left, p_top, p_width, p_height = parent
    if p_width <= 0 or p_height <= 0:
        return None
    wanted_left = p_left + (p_width - width) / 2
    wanted_top = p_top + (p_height - height) / 2
    if abs(wanted_left - left) < 0.5 and abs(wanted_top - top) < 0.5:
        return None
    return round(wanted_left, 2), round(wanted_top, 2)


_WORD_BREAK = re.compile(r"\s+")


def breaks_mid_word(text: str, lines: Sequence[str]) -> bool:
    """Whether the renderer broke a word across two of these lines.

    THE DEFECT THIS NAMES is "Proportionalit / y": a box too narrow for its
    longest word, which PowerPoint fills by breaking the word rather than
    leaving the line short. It is invisible to everything that reads the file
    -- a .pptx stores a paragraph and a box, and where the lines fall is the
    renderer's decision -- and it is the commonest thing wrong with a column of
    labels.

    Read by walking the original text alongside the lines the renderer
    produced: a line that ends in the middle of a word is one whose last
    character is followed, in the original, by something that is not a space.
    Nothing here trusts the line text itself to say so, because a line broken
    mid-word looks exactly like a short word.
    """
    flat = _WORD_BREAK.sub(" ", text).strip()
    if not flat or len(lines) < 2:
        return False
    cursor = 0
    for line in lines[:-1]:
        piece = _WORD_BREAK.sub(" ", str(line)).strip()
        if not piece:
            continue
        found = flat.find(piece, cursor)
        if found < 0:
            return False            # cannot line the two up; claim nothing
        cursor = found + len(piece)
        if cursor < len(flat) and flat[cursor] != " ":
            return True
    return False


def steps_from(reviews: Iterable[Any]) -> list[Step]:
    """The executable half of a review, and nothing else.

    A verdict is only a step when the model asked for an action this module
    has, AND named a shape that was on the list it was given, AND -- for
    `center` -- named one that sits inside something. Everything else is a
    task, and `DesignQaReport.tasks` keeps every one of them.
    """
    steps: list[Step] = []
    for review in reviews:
        for verdict in getattr(review, "verdicts", []):
            if not verdict.executable:
                continue
            # A verdict is executable when it carries EITHER a measured verb
            # or a proposal, and the two are applied by different halves of
            # the tool. Only the verbs belong here; a proposal that reached
            # this loop became a Step whose op is "none", which the applier
            # then refused -- a refusal on the page for a correction the other
            # half had already made.
            if verdict.action not in (
                "shrink", "grow", "center", "widen", "narrow", "send_to_back",
            ):
                continue
            steps.append(
                Step(
                    op=verdict.action,
                    slide=verdict.slide,
                    shape_id=verdict.shape_id,
                    shape=verdict.shape,
                    path=verdict.path,
                    parent_path=verdict.parent_path,
                    parent_id=verdict.parent_id,
                    parent=verdict.parent,
                    note=verdict.note,
                    # THE ROW THIS ANSWERS, on every step and not only on the
                    # arrangements. It was left empty here because the page
                    # could match a measured verb back to its row by shape and
                    # op, which is true right up until somebody wants to take
                    # ONE change back: an undo names a row, and a change that
                    # carries no row cannot be named.
                    task_id=f"{verdict.slide}:{verdict.ref}",
                )
            )
    return steps


# --------------------------------------------------------------------------- #
# The round
# --------------------------------------------------------------------------- #

def apply_steps(deck: Path, out: Path, steps: Sequence[Step]) -> QaFixResult:
    """Take every step once, on a copy of the deck. Never raises.

    A failure here is a page that cannot offer a corrected deck, which is a
    disappointment; an exception here is a run that loses the review as well,
    which is the work. So everything comes back as a result carrying a reason.
    """
    deck, out = Path(deck), Path(out)
    if not steps:
        return QaFixResult(reason="nothing to apply")
    if not powerpoint.available():
        return QaFixResult(
            reason=(
                "no PowerPoint on this host, so nothing can be corrected: it "
                "needs PowerPoint and pywin32 (pip install pywin32) on Windows"
            )
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    # The same file means the corrections that go through `apply.apply_fixes`
    # have already written it, and this round edits what they produced rather
    # than the upload. Copying over it would undo them.
    if deck.resolve() != out.resolve():
        try:
            shutil.copy2(deck, out)
        except OSError as exc:
            return QaFixResult(reason=f"could not copy the deck: {exc}")

    result = QaFixResult(output=out)
    try:
        powerpoint.run(lambda app: _apply_all(app, out, steps, result),
                       timeout=_TIMEOUT_S)
    except Exception as exc:
        log.warning("could not correct %s", deck.name, exc_info=True)
        return QaFixResult(reason=f"PowerPoint would not apply the steps: {exc}")

    log.info(
        "design check: %d correction(s) applied, %d refused",
        len(result.applied), len(result.skipped),
    )
    return result


# The name this module answered to when a font step was the only thing it
# could do. Kept so nothing that imports it breaks on the day the vocabulary
# grew; it is the same call.
apply_font_steps = apply_steps


def _apply_all(
    app: Any, deck: Path, steps: Sequence[Step], result: QaFixResult
) -> None:
    """Every step, on one open presentation, then saved.

    One open and one save for the whole round. Reopening per shape costs a
    second a fix, and a deck opened and saved eleven times is eleven chances
    for PowerPoint to hold a lock on the file the page is about to serve.
    """
    presentation = powerpoint.retry(lambda: app.Presentations.Open(
        str(deck.resolve()), ReadOnly=False, WithWindow=False
    ))
    try:
        count = int(presentation.Slides.Count)
        # Which (shape, axis) pairs a correction has already settled this
        # round. Two findings that both want to decide where one shape sits
        # horizontally -- space this row evenly, and line these columns up --
        # cannot both be honoured, and the second silently winning is how a
        # round produces a slide nobody chose. See `_align`.
        settled: set[tuple[int, str]] = set()
        for step in steps:
            if not 1 <= step.slide <= count:
                result.skipped.append(_refused(step, "that slide is not in the deck"))
                continue
            slide = presentation.Slides(step.slide)
            shape = _find(slide, step.path, step.shape_id, step.shape)
            if shape is None:
                result.skipped.append(_refused(
                    step,
                    "that shape could not be found again on the slide, so "
                    "nothing was touched",
                ))
                continue
            _apply_one(presentation, slide, shape, step, result, settled)
        powerpoint.retry(lambda: presentation.Save())
        powerpoint.retry(lambda: presentation.Close())
    except Exception:
        powerpoint.close(presentation)
        raise


def _apply_one(
    presentation, slide, shape, step: Step, result: QaFixResult,
    settled: Optional[set] = None,
) -> None:
    if step.op in ("grow", "shrink"):
        _step_type(shape, step, result)
    elif step.op == "center":
        _center(slide, shape, step, result)
    elif step.op == "widen":
        _widen(presentation, slide, shape, step, result)
    elif step.op == "narrow":
        _narrow(presentation, slide, shape, step, result)
    elif step.op == "send_to_back":
        _send_to_back(slide, shape, step, result)
    elif step.op == "align":
        _align(slide, shape, step, result,
               settled if settled is not None else set())
    elif step.op == "resize":
        _resize(shape, step, result)
    elif step.op == "set_size":
        _set_size(shape, step, result)
    else:
        result.skipped.append(_refused(step, f"there is no {step.op!r} correction"))


# -- type ------------------------------------------------------------------- #

def _step_type(shape: Any, step: Step, result: QaFixResult) -> None:
    """One shape's type, stepped and kept -- or stepped and put back."""
    try:
        text_range = shape.TextFrame.TextRange
        if not str(text_range.Text).strip():
            result.skipped.append(_refused(step, "there is no text on this shape"))
            return
        current = float(text_range.Font.Size)
    except Exception:
        result.skipped.append(
            _refused(step, "PowerPoint would not say what size this text is")
        )
        return

    if current == _MIXED or current <= 0:
        result.skipped.append(_refused(
            step,
            "this shape is set at several sizes, so there is no one size to "
            "step from",
        ))
        return

    wanted = next_size(current, step.op == "grow")
    if wanted is None:
        bound = f"{CEILING_PT:g}pt" if step.op == "grow" else f"{FLOOR_PT:g}pt"
        result.skipped.append(_refused(
            step,
            f"the type is already at {current:g}pt and a step "
            f"{step.direction} would pass the {bound} bound",
        ))
        return

    try:
        text_range.Font.Size = wanted
    except Exception as exc:
        result.skipped.append(_refused(step, f"PowerPoint refused the size: {exc}"))
        return

    spilled = _spilled(shape)
    if spilled:
        # Put back, not left. A grown heading that now clips is the defect the
        # check exists to find, and writing one because the check asked for it
        # is worse than the check having said nothing.
        def restore() -> None:
            text_range.Font.Size = current

        powerpoint.quietly(restore)
        result.skipped.append(
            _refused(step, f"stepping {step.direction} made the copy {spilled}")
        )
        return

    direction = "up" if wanted > current else "down"
    result.applied.append(_made(
        step, f"stepped the type {direction} from {current:g}pt to {wanted:g}pt"
    ))


# -- centring --------------------------------------------------------------- #

def _center(slide: Any, shape: Any, step: Step, result: QaFixResult) -> None:
    """Put a shape in the middle of the thing that holds it.

    Moves and never resizes: an icon drawn at the wrong size is a different
    finding from an icon in the wrong place, and doing both at once would make
    a component nobody designed.
    """
    group = _find(slide, step.parent_path, step.parent_id, step.parent)
    if group is None:
        result.skipped.append(
            _refused(step, "the shape that holds this one is not on the slide")
        )
        return
    try:
        child_box = (float(shape.Left), float(shape.Top),
                     float(shape.Width), float(shape.Height))
    except Exception:
        result.skipped.append(
            _refused(step, "PowerPoint would not say where this shape is")
        )
        return

    holder, parent_box = _holder_of(group, shape, child_box)
    if parent_box is None:
        result.skipped.append(
            _refused(step, "PowerPoint would not say where these shapes are")
        )
        return

    wanted = centred_on(child_box, parent_box)
    if wanted is None:
        result.skipped.append(
            _refused(step, "it is already centred in the shape that holds it")
        )
        return

    left, top = wanted
    try:
        shape.Left, shape.Top = left, top
    except Exception as exc:
        result.skipped.append(_refused(step, f"PowerPoint refused the move: {exc}"))
        return

    moved = []
    if abs(left - child_box[0]) >= 0.5:
        moved.append(f"{(left - child_box[0]) / _POINTS_PER_INCH:+.2f}in across")
    if abs(top - child_box[1]) >= 0.5:
        moved.append(f"{(top - child_box[1]) / _POINTS_PER_INCH:+.2f}in down")
    where = " and ".join(moved) or "into place"
    result.applied.append(_made(
        step, f"centred it in {holder} -- moved it {where}"
    ))


def _holder_of(
    group: Any, shape: Any, child_box: tuple[float, float, float, float]
) -> tuple[str, Optional[tuple[float, float, float, float]]]:
    """What to centre the shape in: the thing it sits on, or its whole group.

    THE GROUP'S OWN BOX IS THE WRONG ANSWER for the case this exists to fix. A
    component is a circle with an icon on top of it, and the group's box is the
    union of the two -- so an icon that hangs off the circle drags the box it
    is being centred in towards itself, and centring on that leaves it still
    off the circle. What a designer means by "centre the icon" is: centre it on
    the disc it sits on.

    So the sibling that contains the child's centre and is the largest of those
    that do is the holder, and the group is the fallback for a component with
    no such shape -- two icons side by side, a bare label in a group.

    AND THE HOLDER IS NOT ALWAYS A GROUP. A deck usually draws the same
    component as two shapes at the top level -- a circle, and an icon on top of
    it -- and `ai.designqa._holder_of` reads that relationship off the file
    where no group states it. What arrives here is then the circle itself,
    which has no `GroupItems` to look through and needs none: its own box is
    the box to centre on, which is the fallback below.
    """
    centre_x = child_box[0] + child_box[2] / 2
    centre_y = child_box[1] + child_box[3] / 2
    best: Optional[tuple[float, str, tuple[float, float, float, float]]] = None
    try:
        own_id = int(shape.Id)
    except Exception:
        own_id = None

    try:
        items = group.GroupItems if int(group.Type) == _MSO_GROUP else None
    except Exception:
        items = None

    for sibling in (_com_each(items) if items is not None else ()):
        try:
            if own_id is not None and int(sibling.Id) == own_id:
                continue
            box = (float(sibling.Left), float(sibling.Top),
                   float(sibling.Width), float(sibling.Height))
        except Exception:
            continue
        if not (box[0] <= centre_x <= box[0] + box[2]
                and box[1] <= centre_y <= box[1] + box[3]):
            continue
        area = box[2] * box[3]
        if area <= child_box[2] * child_box[3]:
            continue        # something smaller than the icon does not hold it
        if best is None or area > best[0]:
            best = (area, _name_of(sibling), box)

    if best is not None:
        return best[1], best[2]
    try:
        return _name_of(group), (
            float(group.Left), float(group.Top),
            float(group.Width), float(group.Height),
        )
    except Exception:
        return _name_of(group), None


# -- z-order ---------------------------------------------------------------- #

# msoSendToBack. The only z-order constant used here: nothing is brought
# forward, because "this should be in front" is a composition somebody chose
# and "this photograph is covering the slide" is not.
_MSO_SEND_TO_BACK = 1


def _send_to_back(slide: Any, shape: Any, step: Step, result: QaFixResult) -> None:
    """Put a shape behind everything else on its slide.

    THE ONE CORRECTION HERE THAT MOVES NOTHING. A full-bleed photograph drawn
    over the cards it was meant to sit under is an overlap in the render and
    nothing at all in the geometry -- both shapes are exactly where the layout
    puts them, and the only thing wrong is which of them was added last. Every
    other answer to it is worse: nudging the picture clear of the cards walks
    a full-bleed image off the slide, and shrinking either one makes a
    composition nobody drew.

    ALL THE WAY BACK RATHER THAN ONE STEP. A step behind is a guess about how
    many things this should be behind, and the slide cannot settle it; the back
    of the deck is where a background belongs and is the only position that
    needs no such guess.

    Top-level shapes only. Inside a group, z-order is the group's internal
    stacking and sending a child to the back of it does not put it behind
    anything a reader can see -- the group as a whole is still wherever it was.
    """
    if len(step.path) > 1:
        result.skipped.append(_refused(
            step,
            "this shape is inside a group, where the stacking order is the "
            "group's own; the group is what sits in front of the slide",
        ))
        return

    try:
        if int(shape.ZOrderPosition) == 1:
            result.skipped.append(
                _refused(step, "it is already at the back of the slide")
            )
            return
    except Exception:
        pass

    try:
        shape.ZOrder(_MSO_SEND_TO_BACK)
    except Exception as exc:
        result.skipped.append(
            _refused(step, f"PowerPoint refused to restack it: {exc}")
        )
        return

    result.applied.append(Change(
        op=step.op,
        slide=step.slide,
        shape_id=step.shape_id,
        shape=step.shape,
        detail="sent it behind everything else on the slide",
        task_id=step.task_id,
    ))


# -- widening --------------------------------------------------------------- #

def _widen(presentation, slide, shape, step: Step, result: QaFixResult) -> None:
    """Widen a box until its words stop breaking in half.

    STEPS AND RE-READS rather than computing the width the copy needs. How wide
    a word draws depends on the typeface, the size, the kerning and the
    language, and every attempt to work it out from the characters is a guess
    that is wrong for Arabic. PowerPoint will say where it broke the lines, so
    the box is widened a little and asked again.

    Stops at whichever comes first: the break going away, the neighbour on its
    right, the edge of the slide, or the cap. The break surviving all of that
    is a column too narrow for its copy, which is a layout decision and becomes
    a task instead.
    """
    try:
        text_range = shape.TextFrame.TextRange
        text = str(text_range.Text)
        if not text.strip():
            result.skipped.append(_refused(step, "there is no text on this shape"))
            return
        original = float(shape.Width)
    except Exception:
        result.skipped.append(
            _refused(step, "PowerPoint would not say what this shape holds")
        )
        return

    if not breaks_mid_word(text, _lines_of(text_range)):
        result.skipped.append(_refused(
            step, "its words are not breaking in half, so widening it would "
                  "only make the box bigger",
        ))
        return

    room = _room_to_the_right(presentation, slide, shape)
    ceiling = min(original * _WIDEN_CAP, original + max(0.0, room))
    if ceiling <= original + 1:
        result.skipped.append(_refused(
            step, "there is no room to widen it: what is beside it would be "
                  "in the way",
        ))
        return

    width = original
    for _attempt in range(_WIDEN_TRIES):
        width = min(width * _WIDEN_STEP, ceiling)
        try:
            shape.Width = width
        except Exception as exc:
            _restore_width(shape, original)
            result.skipped.append(_refused(step, f"PowerPoint refused the width: {exc}"))
            return
        if not breaks_mid_word(text, _lines_of(text_range)):
            result.applied.append(_made(
                step,
                f"widened the box from {original / _POINTS_PER_INCH:.2f}in to "
                f"{width / _POINTS_PER_INCH:.2f}in, so its words stop breaking",
            ))
            return
        if width >= ceiling:
            break

    # Nothing gained, so nothing kept. A box left half-widened is a change the
    # designer did not ask for and cannot see the point of.
    _restore_width(shape, original)
    result.skipped.append(_refused(
        step,
        "its words still break in half at the widest this box can go without "
        "reaching what is beside it; the column needs to be laid out wider",
    ))


def _restore_width(shape: Any, width: float) -> None:
    def restore() -> None:
        shape.Width = width

    powerpoint.quietly(restore)


def _lines_of(text_range: Any) -> list[str]:
    """The lines PowerPoint actually drew, or none it will admit to."""
    try:
        count = int(text_range.Lines().Count)
        return [str(text_range.Lines(i + 1).Text) for i in range(count)]
    except Exception:
        return []


def _room_to_the_right(presentation: Any, slide: Any, shape: Any) -> float:
    """How much wider this box can be drawn before it reaches something.

    The nearest left edge among the shapes it shares a horizontal band with,
    or the edge of the slide, less a clearance. Shapes that do not overlap it
    vertically are not in its way however close they look in a list.
    """
    try:
        left, top = float(shape.Left), float(shape.Top)
        right, bottom = left + float(shape.Width), top + float(shape.Height)
        limit = float(presentation.PageSetup.SlideWidth)
    except Exception:
        return 0.0

    for other in _com_each(slide.Shapes):
        try:
            if int(other.Id) == int(shape.Id):
                continue
            o_left, o_top = float(other.Left), float(other.Top)
            o_bottom = o_top + float(other.Height)
        except Exception:
            continue
        if o_left < right or o_top >= bottom or o_bottom <= top:
            continue
        limit = min(limit, o_left)
    return max(0.0, limit - _CLEARANCE_PT - right)


# -- narrowing -------------------------------------------------------------- #

# How much narrower each attempt makes the box, and the floor on the total. A
# box cut to half its width is not a box that was running behind something, it
# is a column somebody has to redraw, and taking it that far would leave the
# copy in a gutter.
_NARROW_STEP = 0.92
_NARROW_TRIES = 6
_NARROW_FLOOR = 0.55


def _narrow(presentation, slide, shape, step: Step, result: QaFixResult) -> None:
    """Pull a text box in until its copy stops running behind what is beside it.

    THE MIRROR OF `_widen`, AND THE CASE IT COULD NOT ANSWER. Widening fixes a
    box too narrow for a word; this fixes a box too WIDE for the space it was
    drawn in -- a paragraph set at full width with an image sitting over its
    right-hand end, so the last words of every line are behind the picture. The
    box and the picture are both where the layout puts them and neither is in
    the wrong place; the measure is simply longer than the room.

    IT STOPS AT THE THING IT IS CLEARING rather than at a width computed from
    the copy, for the reason `_widen` gives in full: how wide a word draws
    depends on the typeface, the size, the kerning and the language, and every
    attempt to derive it from the characters is a guess that is wrong for
    Arabic. So the box is pulled in a little at a time and PowerPoint is asked
    again where the text now ends.

    AND IT IS PUT BACK IF THE COPY STOPS FITTING. Narrowing a box makes its
    text taller, so a paragraph that fitted at full width can overflow the
    bottom at three-quarters of it -- trading a collision a reader can still
    read around for one they cannot. Measured after each step, the same way a
    type step is.
    """
    try:
        text_range = shape.TextFrame.TextRange
        if not str(text_range.Text).strip():
            result.skipped.append(_refused(step, "there is no text on this shape"))
            return
        original = float(shape.Width)
        left = float(shape.Left)
    except Exception:
        result.skipped.append(
            _refused(step, "PowerPoint would not say what this shape holds")
        )
        return

    blocker = _blocking_right(slide, shape)
    if blocker is None:
        result.skipped.append(_refused(
            step,
            "nothing is sitting over the right-hand end of this box, so "
            "narrowing it would only make the measure shorter",
        ))
        return

    # Where the box has to end to clear it. Computed once: this is a fact about
    # the two rectangles, and it is the only number this correction needs.
    wanted = max(blocker - _CLEARANCE_PT - left, original * _NARROW_FLOOR)
    if wanted >= original - 1:
        result.skipped.append(
            _refused(step, "this box already clears what is beside it")
        )
        return

    width = original
    for _attempt in range(_NARROW_TRIES):
        width = max(width * _NARROW_STEP, wanted)
        try:
            shape.Width = width
        except Exception as exc:
            _restore_width(shape, original)
            result.skipped.append(
                _refused(step, f"PowerPoint refused the width: {exc}")
            )
            return
        spilled = _spilled(shape)
        if spilled:
            _restore_width(shape, original)
            result.skipped.append(_refused(
                step,
                f"narrowing it to clear what is beside it {spilled}, so the "
                "copy has to be shortened or the block moved instead",
            ))
            return
        if width <= wanted:
            result.applied.append(_made(
                step,
                f"narrowed the box from {original / _POINTS_PER_INCH:.2f}in to "
                f"{width / _POINTS_PER_INCH:.2f}in, so its copy wraps before "
                "what is beside it",
            ))
            return

    _restore_width(shape, original)
    result.skipped.append(_refused(
        step, "this box would have to lose more than its width allows to "
              "clear what is beside it",
    ))


def _blocking_right(slide: Any, shape: Any) -> Optional[float]:
    """The left edge of the nearest shape sitting OVER this box's right end.

    Not `_room_to_the_right`, which measures to the nearest thing the box could
    grow into. This is the opposite question: what is already inside the box's
    own rectangle, on its right, and drawn after it. A shape that merely sits
    beside the box is not in the way -- the copy is not running behind it.
    """
    try:
        left, top = float(shape.Left), float(shape.Top)
        right, bottom = left + float(shape.Width), top + float(shape.Height)
        own = int(shape.Id)
    except Exception:
        return None

    found = None
    for other in _com_each(slide.Shapes):
        try:
            if int(other.Id) == own:
                continue
            o_left, o_top = float(other.Left), float(other.Top)
            o_right = o_left + float(other.Width)
            o_bottom = o_top + float(other.Height)
        except Exception:
            continue
        # Overlapping this box vertically, starting inside it, and reaching at
        # least as far right as it does: a thing the copy runs under, rather
        # than one the box happens to touch a corner of.
        if o_top >= bottom or o_bottom <= top:
            continue
        if not (left < o_left < right) or o_right < right - _CLEARANCE_PT:
            continue
        found = o_left if found is None else min(found, o_left)
    return found


# -- aligning --------------------------------------------------------------- #

def _align(
    slide: Any, shape: Any, step: Step, result: QaFixResult, settled: set,
) -> None:
    """Move a shape to where the shapes it belongs with put it.

    The target is arithmetic done before this ran -- the position most of the
    deck agrees on, or the line the rest of a row sits on -- so all that is
    left here is the move and the guards on it.

    ONE AXIS OR BOTH. A cross-slide align names both; a within-slide
    arrangement names the one it is about and leaves the other None, and an
    axis that is None is not written at all. Writing back the value an axis
    already had is not a no-op in a round with more than one correction in it:
    the value may be what the previous step just fixed.

    TWO CORRECTIONS DO NOT GET TO DISAGREE ABOUT ONE AXIS. A row spaced evenly
    and a column lined up both decide where a shape sits horizontally, and
    they will not agree. Applied in the order they happen to arrive, the
    second wins and the first is reported as done anyway -- a page claiming
    two corrections and a slide carrying one. So the first to settle an axis
    keeps it and the second is refused with a reason, which is the honest
    outcome: arithmetic can compute either answer and cannot choose between
    them.

    The distance guard matters for the same reason it always did: a large move
    means the shape is not doing the same job as the ones it was measured
    against, and putting a cover's title where a content slide's title goes is
    worse than leaving it alone.
    """
    if step.left_in is None and step.top_in is None:
        result.skipped.append(_refused(step, "there is nowhere named to move it to"))
        return
    try:
        was_left, was_top = float(shape.Left), float(shape.Top)
    except Exception:
        result.skipped.append(
            _refused(step, "PowerPoint would not say where this shape is")
        )
        return

    # KEYED ON THE WHOLE ADDRESS, not on the id. A shape id is unique within a
    # slide and this set spans the round, so `(id, axis)` had slide 1's shape 2
    # and slide 7's shape 2 as one shape -- levelling a row on one slide then
    # refused to level the row on the other, in words about a disagreement
    # that did not exist. The path is included for the case `_find` is built
    # around: a deck pasted together can carry the same id twice on one slide.
    here = (step.slide, step.path, step.shape_id)
    for axis, wanted in (("across", step.left_in), ("down", step.top_in)):
        if wanted is None or (here, axis) not in settled:
            continue
        result.skipped.append(_refused(
            step,
            f"another correction in this round has already settled where this "
            f"shape sits {axis}, and the two do not agree",
        ))
        return

    left = was_left if step.left_in is None else step.left_in * _POINTS_PER_INCH
    top = was_top if step.top_in is None else step.top_in * _POINTS_PER_INCH
    moved_in = max(
        abs(left - was_left), abs(top - was_top)
    ) / _POINTS_PER_INCH
    if moved_in > _ALIGN_LIMIT_IN:
        result.skipped.append(_refused(
            step,
            f"that would move it {moved_in:.2f}in, which is further than an "
            "alignment: this shape is not doing the same job as the ones it "
            "was measured against",
        ))
        return
    if moved_in < 0.01:
        result.skipped.append(
            _refused(step, f"it is already {step.measured_off}")
        )
        return

    landed_on = _would_land_on(slide, shape, left, top)
    if landed_on:
        result.skipped.append(_refused(
            step,
            f"that would move it onto {landed_on}, which it is clear of "
            f"where it is: an alignment that buries a neighbour is not the "
            f"tidier of the two slides",
        ))
        return

    try:
        shape.Left, shape.Top = left, top
    except Exception as exc:
        result.skipped.append(_refused(step, f"PowerPoint refused the move: {exc}"))
        return

    if step.left_in is not None:
        settled.add((here, "across"))
    if step.top_in is not None:
        settled.add((here, "down"))

    # Only the axis that actually moved is described. A row being levelled
    # reports how far down it came and says nothing about across, because it
    # did nothing across.
    moves = []
    if step.left_in is not None:
        moves.append(f"{(left - was_left) / _POINTS_PER_INCH:+.2f}in across")
    if step.top_in is not None:
        moves.append(f"{(top - was_top) / _POINTS_PER_INCH:+.2f}in down")
    result.applied.append(_made(
        step, f"moved it to {step.measured_off} ({', '.join(moves)})"))


# How much of the moving shape has to end up over a neighbour before the move
# counts as burying it. A hair of overlap between two boxes is routine on a
# real slide -- a heading's box runs a few hundredths into the rule under it
# and nothing touches -- and refusing on that would refuse most alignments.
# A twelfth of the shape is not a hair.
_LANDS_ON_SHARE = 0.08

# And the same question asked of what was already true. A shape that arrived
# overlapping its neighbour must not be refused for the overlap it came with;
# the question this guard asks is only whether the move makes a NEW one.
_ALREADY_ON_SHARE = 0.02


def _would_land_on(
    slide: Any, shape: Any, left: float, top: float
) -> str:
    """The neighbour this move would bury, named, or "" when it buries none.

    THE ALIGNMENT THAT WALKED A COLUMN INTO THE TITLE. A set is levelled
    against its own members and nothing in that arithmetic knows what else is
    on the slide, so a row of column headings brought onto the line of the
    first of them is free to arrive on top of the title above it -- which is
    what happened on a real deck, and it is not a defect in the arithmetic. It
    is a question the arithmetic was never asked.

    So it is asked here, where both positions are knowable: the shape's
    rectangle where it is, its rectangle where it would go, and every other
    shape on the slide. A neighbour it is already over stays allowed -- a
    label on a band is over the band by design, and refusing that would refuse
    every alignment inside a component. What is refused is arriving on top of
    something it is currently clear of.

    Top-level shapes only. Inside a group the members overlap each other by
    construction, and a child being centred in its parent is exactly the
    correction this must not stand in the way of.

    A slide that will not answer comes back "" and the move goes ahead, on the
    same terms as every other guard here: a check that cannot run must not
    become a check that refuses everything.
    """
    here = _rect_of(shape)
    if here is None:
        return ""
    width, height = here[2] - here[0], here[3] - here[1]
    if width <= 0 or height <= 0:
        return ""
    there = (left, top, left + width, top + height)
    if _rects_equal(here, there):
        return ""

    area = width * height
    try:
        me = int(shape.Id)
    except Exception:
        me = None

    for other in _com_each(slide.Shapes):
        try:
            if me is not None and int(other.Id) == me:
                continue
        except Exception:
            continue
        box = _rect_of(other)
        if box is None:
            continue
        after = _overlap_area(there, box)
        if after / area < _LANDS_ON_SHARE:
            continue
        before = _overlap_area(here, box)
        if before / area >= _ALREADY_ON_SHARE:
            continue        # it was already over this one; not this move's doing
        return _name_of(other)
    return ""


def _rect_of(shape: Any) -> Optional[tuple[float, float, float, float]]:
    """(left, top, right, bottom) in points, or None where COM will not say."""
    try:
        left, top = float(shape.Left), float(shape.Top)
        width, height = float(shape.Width), float(shape.Height)
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return (left, top, left + width, top + height)


def _rects_equal(one: tuple, other: tuple) -> bool:
    return all(abs(a - b) < 0.5 for a, b in zip(one, other))


def _overlap_area(one: tuple, other: tuple) -> float:
    across = min(one[2], other[2]) - max(one[0], other[0])
    down = min(one[3], other[3]) - max(one[1], other[1])
    return max(0.0, across) * max(0.0, down)


def _set_size(shape: Any, step: Step, result: QaFixResult) -> None:
    """Set a shape's type to the size the rest of its set is at.

    NOT A STEP, WHICH IS WHY IT IS NOT `grow` OR `shrink`. Those move type one
    notch because the model can see that something is too big without being
    able to say how big it should be. Here the set says: four headings at one
    level of the hierarchy, three of them at 22pt, and the number is counted
    rather than judged.

    Rolled back if the copy stops fitting, the same as a step. A heading set
    larger to match its row can overflow the card it sits in, and a row that
    agrees about type with one heading spilling out of its box is not the
    tidier of the two outcomes.
    """
    if step.size_pt is None:
        result.skipped.append(_refused(step, "there is no size named to set"))
        return
    try:
        text_range = shape.TextFrame.TextRange
        if not str(text_range.Text).strip():
            result.skipped.append(_refused(step, "there is no text on this shape"))
            return
        current = float(text_range.Font.Size)
    except Exception:
        result.skipped.append(
            _refused(step, "PowerPoint would not say what size this text is")
        )
        return

    if current == _MIXED:
        result.skipped.append(_refused(
            step, "this shape is set at more than one size, so there is no "
                  "one size to change",
        ))
        return
    if abs(current - step.size_pt) < _STEP_PT:
        result.skipped.append(_refused(step, f"it is already {step.measured_off}"))
        return

    try:
        text_range.Font.Size = step.size_pt
    except Exception as exc:
        result.skipped.append(_refused(step, f"PowerPoint refused the size: {exc}"))
        return

    spilled = _spilled(shape)
    if spilled:
        powerpoint.quietly(lambda: setattr(text_range.Font, "Size", current))
        result.skipped.append(_refused(
            step, f"setting it to {step.size_pt:g}pt {spilled}, so the box or "
                  "the copy has to give first",
        ))
        return

    result.applied.append(_made(
        step,
        f"set the type from {current:g}pt to {step.size_pt:g}pt, "
        f"to match {step.measured_off}",
    ))


def _resize(shape: Any, step: Step, result: QaFixResult) -> None:
    """Give a shape the size the rest of its set already is.

    THE ONE CORRECTION HERE THAT CHANGES A SIZE. Everything else moves things,
    and the difference matters: a move can be looked at and undone by eye,
    while a resize changes how much copy fits and can push text out of a box
    that held it. So the target is computed off the set before this runs
    (`designqa._resize_steps`), bounded there to a third, and the result is
    measured here.

    FROM THE TOP LEFT, which is what PowerPoint does and what the arrangement
    means: a card that grows grows to the right. The row's left edges are
    usually its one good alignment and are the thing this must not disturb.

    PUT BACK IF THE COPY STOPS FITTING, like a type step and a narrowing. A
    card made narrower to match its set can no longer hold its heading, and a
    row that matches with one card's text spilling out of it is not the tidier
    of the two outcomes.
    """
    if step.width_in is None and step.height_in is None:
        result.skipped.append(_refused(step, "there is no size named to give it"))
        return
    try:
        was_width, was_height = float(shape.Width), float(shape.Height)
    except Exception:
        result.skipped.append(
            _refused(step, "PowerPoint would not say how big this shape is")
        )
        return

    width = was_width if step.width_in is None else step.width_in * _POINTS_PER_INCH
    height = was_height if step.height_in is None else step.height_in * _POINTS_PER_INCH
    if (abs(width - was_width) < 1.0) and (abs(height - was_height) < 1.0):
        result.skipped.append(
            _refused(step, f"it is already {step.measured_off}")
        )
        return

    try:
        shape.Width, shape.Height = width, height
    except Exception as exc:
        result.skipped.append(_refused(step, f"PowerPoint refused the size: {exc}"))
        return

    spilled = _spilled(shape)
    if spilled:
        powerpoint.quietly(lambda: setattr(shape, "Width", was_width))
        powerpoint.quietly(lambda: setattr(shape, "Height", was_height))
        result.skipped.append(_refused(
            step, f"matching the rest of its set {spilled}, so the copy has to "
                  "be shortened before the shapes can agree",
        ))
        return

    axis = "wide" if step.width_in is not None else "tall"
    before = was_width if step.width_in is not None else was_height
    after = width if step.width_in is not None else height
    result.applied.append(_made(
        step,
        f"made it {after / _POINTS_PER_INCH:.2f}in {axis}, from "
        f"{before / _POINTS_PER_INCH:.2f}in, to match {step.measured_off}",
    ))


# -- shared ----------------------------------------------------------------- #

def _spilled(shape: Any) -> str:
    """How the drawn text no longer fits its box, or "" when it does.

    Measured rather than predicted: how much copy fits in a box depends on
    where every line breaks, and where a line breaks is not in the file. This
    asks the engine that just drew it.

    A shape that will not answer is read as fitting. The alternative is
    refusing every step on every shape PowerPoint is quiet about, which is how
    a correction layer becomes one that never corrects anything.
    """
    try:
        if int(shape.TextFrame.AutoSize) != 0:
            # The box resizes itself to its text, so the text cannot spill it.
            # What moved instead is the box, and the caller can see that on the
            # render.
            return ""
    except Exception:
        pass
    try:
        text_range = shape.TextFrame.TextRange
        height, width = float(shape.Height), float(shape.Width)
        drawn_h, drawn_w = float(text_range.BoundHeight), float(text_range.BoundWidth)
    except Exception:
        return ""
    if height <= 0 or width <= 0:
        return ""
    if drawn_h > height * (1 + _SPILL_SLACK):
        return "taller than its box"
    if drawn_w > width * (1 + _SPILL_SLACK):
        return "wider than its box"
    return ""


def _made(step: Step, detail: str) -> Change:
    return Change(
        op=step.op, slide=step.slide, shape_id=step.shape_id,
        shape=step.shape, detail=detail, task_id=step.task_id,
    )


def _refused(step: Step, reason: str) -> SkippedStep:
    return SkippedStep(
        op=step.op, slide=step.slide, shape_id=step.shape_id,
        shape=step.shape, reason=reason, task_id=step.task_id,
    )


def _name_of(shape: Any) -> str:
    try:
        return str(shape.Name)
    except Exception:
        return "the shape around it"


def _find(
    slide: Any,
    path: Sequence[int],
    shape_id: Optional[int],
    name: str,
) -> Optional[Any]:
    """The shape a step names, by where it sits and what it is called.

    BY PATH FIRST, AND THE REASON IS A REAL DECK. A shape id is unique within a
    slide in a well-formed file, and this tool reads the file with python-pptx
    and edits it through PowerPoint -- two readings that agree only while the
    file is well formed. A deck carrying the same id on two shapes, which is
    what pasting between decks produces, is renumbered silently by PowerPoint
    when it opens it. Measured on such a deck: the group holding an icon and a
    text box two shapes away both claimed id 910, and a correction addressed by
    id centred the icon on the text box. Document order is the one thing both
    readings agree on.

    THE NAME IS THE CHECK, not the key. A path that finds a shape with a
    different name means the deck has changed since it was read -- a shape
    deleted, an order reversed -- so the id is tried instead, and if that finds
    nothing recognisable either, nothing is touched and the step is refused
    with a reason. A name is not unique and is never used to search.
    """
    found = _by_path(slide, path)
    if found is not None and (not name or _name_of(found) == name):
        return found
    by_id = _shape_by_id(slide, shape_id)
    if by_id is not None and (not name or _name_of(by_id) == name):
        return by_id
    return found or by_id if not name else None


def _by_path(slide: Any, path: Sequence[int]) -> Optional[Any]:
    """The shape at this position in the tree, or None if it is not there."""
    if not path:
        return None
    try:
        shape = slide.Shapes(int(path[0]))
        for index in path[1:]:
            shape = shape.GroupItems(int(index))
        return shape
    except Exception:
        return None


def _shape_by_id(slide: Any, shape_id: Optional[int]) -> Optional[Any]:
    """The shape carrying this id, inside groups too.

    The fallback rather than the way in -- see `_find`. Still by id and never
    by name: a name is not unique on a real slide, and editing the wrong shape
    in a client deck is worse than editing none.
    """
    if shape_id is None:
        return None
    for shape in _com_each(slide.Shapes):
        found = _matching(shape, shape_id)
        if found is not None:
            return found
    return None


def _matching(shape: Any, shape_id: int) -> Optional[Any]:
    try:
        if int(shape.Id) == shape_id:
            return shape
        if int(shape.Type) == _MSO_GROUP:
            for child in _com_each(shape.GroupItems):
                found = _matching(child, shape_id)
                if found is not None:
                    return found
    except Exception:
        return None
    return None


def _com_each(collection: Any):
    """A 1-based COM collection as an iterator, skipping what will not come."""
    try:
        count = int(collection.Count)
    except Exception:
        return
    for i in range(1, count + 1):
        try:
            yield collection(i)
        except Exception:
            continue
