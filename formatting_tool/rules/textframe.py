"""The text inside a shape, as opposed to where the shape sits.

THREE SETTINGS NOTHING IN THIS TOOL READ, and each of them defeats something
the tool otherwise does well. A shape's box is measured by a dozen rules; what
the shape does with the text INSIDE that box was read for its wrap mode and
nothing else, and the three left out are the ones that decide whether a box can
be corrected at all, whether the size in the file is the size on the screen, and
whether a row of cards reads as a row.

  vertical_anchor   where the text sits in the box. It decides whether the
                    overflow fix can run: growing a box does not move
                    top-anchored text and DOES move middle- or bottom-anchored
                    text. `space.text_overflow` has always refused those with
                    "anchor the text to the top first" -- an instruction to a
                    designer for a correction nothing could make, because
                    nothing read the anchor.
  autofit_scale     how far PowerPoint is shrinking the copy. The SETTING was
                    read and reported; the AMOUNT was not, and the amount is
                    the defect. A box the file says is 14pt and the reader sees
                    at 8.75pt is invisible to every size rule here, all of
                    which read the file.
  text_margins      the insets. A row of cards whose copy starts at four
                    different distances from the card edge reads as ragged
                    while every box on it is exactly where it should be, so no
                    alignment rule has anything to say about it.

WHY THEY BELONG TOGETHER. Each one is a property of the text frame rather than
of the shape or the copy, each is corrected by writing one value, and each is
invisible on a render until you know to look -- which is the combination that
leaves a defect in a deck for a year.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..linemetrics import LineMetricsProvider, NullLineMetrics, ShapeKey
from ..models import Category, Issue, Severity, ShapeProfile
from .base import Rule, RuleContext

# How far the drawn text may exceed its box before the box counts as failing.
# The same slack `space.text_overflow` measures with, and for the same reason:
# PowerPoint reports the bounds including line leading, so copy that fits
# exactly measures a hair over.
_SLACK_IN = 0.02

# Below this, a box is shrinking its text enough that the size in the file is
# not a description of the slide. 0.95 rather than 1.0 because PowerPoint
# writes small scales for sub-point adjustments nobody can see, and reporting
# those would be reporting rounding.
_SHRINKING = 0.95

# How far apart two insets have to be to read as different. A hundredth of an
# inch is under a point, which is below what anyone sees and above the rounding
# a deck built by hand carries.
_MARGIN_SLACK_IN = 0.01

# How many shapes make a row rather than a coincidence, and the same number the
# repeat and heading rules use.
_ROW_NEEDS = 3


class AnchorBlocksFitRule(Rule):
    """Copy that will not fit a box the anchor will not let anything grow.

    THE FINDING THE APPLIER HAS BEEN ASKING FOR. `apply.fixers.fix_text_overflow`
    grows a box to the size its copy needs, and refuses outright when the text
    is middle- or bottom-anchored, because growing such a box moves the copy
    rather than giving it room -- measured on a real box, growing it 0.22in to
    0.50in moved bottom-anchored copy 0.28in DOWN, onto the bar it was meant to
    clear and had not even been touching. The refusal ends "shorten the copy or
    anchor the text to the top first", and anchoring the text to the top is a
    correction nothing in this tool could make, because nothing read the anchor.

    So it is reported on its own terms: not "this box is middle-anchored",
    which is a preference and true of most of the text on most decks, but "this
    box is middle-anchored AND its copy does not fit", which is a box nobody
    can correct until the anchor moves.

    Needs a renderer. Whether copy fits is not in the file.
    """

    id = "typography.anchor_blocks_fit"
    category = Category.TYPOGRAPHY
    description = "Text overflows a box whose anchor stops it being grown."
    default_severity = Severity.WARNING

    def __init__(self, metrics: Optional[LineMetricsProvider] = None) -> None:
        self.metrics = metrics or NullLineMetrics()

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        if not self.metrics.available:
            return
        for slide, shape in ctx.text_shapes():
            if shape.is_group or not shape.text.strip():
                continue
            if shape.vertical_anchor not in ("middle", "bottom"):
                continue
            box = shape.geometry
            if box.height_in <= 0:
                continue
            bounds = self.metrics.bounds(ShapeKey(slide.number, shape.shape_id))
            if bounds is None:
                continue
            over = (bounds.top_in + bounds.height_in) - (box.top_in + box.height_in)
            if over <= _SLACK_IN:
                continue
            yield self.issue(
                f"This copy runs {over:.2f}in past its box, and the box is "
                f"{shape.vertical_anchor}-anchored, so growing it would move "
                "the copy rather than give it room.",
                slide=slide,
                shape=shape,
                expected="top-anchored, so the box can be grown to fit",
                found=f"{shape.vertical_anchor}-anchored",
                suggestion=(
                    "Anchor the text to the top, which lets the overflow be "
                    "corrected by growing the box."
                ),
            )


class AutofitScaleRule(Rule):
    """A box drawing its text at a size the file does not admit to.

    `size.autofit_shrink` reports the SETTING and always has: this box shrinks
    its text to fit. What it could not report is the AMOUNT, because nothing
    read `a:normAutofit/@fontScale`, and the amount is the whole defect. A
    heading stored at 14pt and drawn at 8.75pt is not a preference about
    overflow; it is a size that appears nowhere in the brand system, differs
    per slide, and is invisible to every size rule in this tool, all of which
    read the file and see 14.

    THE CORRECTION IS TO WRITE DOWN WHAT IS ALREADY ON THE SCREEN. Multiply the
    stored sizes by the scale, set them, and take the shrinking off. Nothing
    about the slide changes -- that is the point, and it is why this is safe to
    do without a designer: the deck renders exactly as it did, and the tool can
    finally see what it renders as. What happens next is the rest of the
    report's business, which is where it belongs.

    Only where it is actually shrinking. A box set to shrink but drawing at
    full size is a trap waiting for the next copy edit and not a defect today,
    and reporting it would bury this one.
    """

    id = "size.autofit_scale"
    category = Category.FONT_SIZE
    description = "Text is drawn smaller than the size stored for it."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, shape in ctx.text_shapes():
            scale = shape.autofit_scale
            if scale is None or scale >= _SHRINKING or scale <= 0:
                continue
            sizes = sorted({
                run.size_pt
                for paragraph in shape.paragraphs
                for run in paragraph.runs
                if run.size_pt
            })
            if not sizes:
                continue
            drawn = ", ".join(f"{size * scale:.1f}pt" for size in sizes)
            stored = ", ".join(f"{size:g}pt" for size in sizes)
            yield self.issue(
                f"This box is drawing its {stored} copy at {drawn}: "
                f"PowerPoint is shrinking it to {scale * 100:.0f}% to make it "
                "fit, so the size in the file is not the size on the slide.",
                slide=slide,
                shape=shape,
                severity=(
                    Severity.ERROR if scale < 0.8 else Severity.WARNING
                ),
                expected=f"the drawn size written down, at {scale * 100:.0f}%",
                found=f"{scale * 100:.0f}% of {stored}",
                suggestion=(
                    "Set the type to the size it is actually drawn at and turn "
                    "the shrinking off. The slide looks the same and the size "
                    "stops being a secret."
                ),
            )


class TextInsetRule(Rule):
    """A row of repeated shapes whose copy starts at different insets.

    THE MISALIGNMENT NO ALIGNMENT RULE CAN SEE. Four cards drawn at the same
    size, on the same top edge, evenly spaced -- and the copy in one of them
    starts a tenth of an inch further in than the other three, because somebody
    pasted it from a slide with a different text frame. Every geometric rule
    here measures the BOX, and every box is exactly where it should be. What a
    reader sees is four cards whose text does not line up.

    Grouped the way `typography._heading_rows` groups: same size, same top,
    three or more of them. A set of same-sized boxes in a row is a component
    repeated, and the inset a component uses is one decision rather than four.

    The majority is the answer, and no majority means no finding: two cards at
    0.1in and two at 0.05in is a pair of decisions somebody made, not a drift.
    """

    id = "space.text_insets"
    category = Category.SPACE
    description = "Repeated shapes inset their text differently."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            for row in _repeated_rows(slide):
                insets = {
                    shape.shape_id: shape.text_margins
                    for shape in row
                    if shape.text_margins is not None
                }
                if len(insets) < _ROW_NEEDS:
                    continue
                agreed = _majority(list(insets.values()))
                if agreed is None:
                    continue
                for shape in row:
                    mine = insets.get(shape.shape_id)
                    if mine is None or _same_insets(mine, agreed):
                        continue
                    yield self.issue(
                        "The copy in this shape starts at a different inset "
                        f"from the {len(row)} shapes it repeats with, so their "
                        "text does not line up even though their boxes do.",
                        slide=slide,
                        shape=shape,
                        expected=_inset_words(agreed),
                        found=_inset_words(mine),
                        suggestion=(
                            "Set its text insets to "
                            f"{_inset_words(agreed)}, which is what the rest "
                            "of the row uses."
                        ),
                    )


def _repeated_rows(slide) -> list[list[ShapeProfile]]:
    """Same-sized text-bearing shapes sitting at the same height.

    The same grouping `typography._heading_rows` uses, and deliberately so: a
    set of boxes drawn once and copied across a slide is what both rules are
    about, and two ways of deciding what counts as a row would report two
    different rows on one slide.
    """
    buckets: dict[tuple, list[ShapeProfile]] = {}
    for shape in slide.shapes:
        if shape.is_group or not shape.text.strip():
            continue
        box = shape.geometry
        if not box or not box.width_in or not box.height_in:
            continue
        key = (round(box.width_in, 2), round(box.height_in, 2),
               round(box.top_in, 1))
        buckets.setdefault(key, []).append(shape)
    return [
        sorted(members, key=lambda s: s.geometry.left_in)
        for members in buckets.values()
        if len(members) >= _ROW_NEEDS
    ]


def _majority(insets: list) -> Optional[tuple]:
    """The inset more than half the row uses, or None if there is no such thing.

    A MAJORITY, NOT THE COMMONEST. Four cards at four insets have a commonest
    one only by tie-break, and setting the row to whichever came first is a
    coin toss a designer has to check anyway. More than half is the point at
    which the odd ones out are drift rather than a decision.
    """
    counts: dict[tuple, int] = {}
    for inset in insets:
        rounded = tuple(round(value, 2) for value in inset)
        counts[rounded] = counts.get(rounded, 0) + 1
    best, count = max(counts.items(), key=lambda pair: pair[1])
    return best if count * 2 > len(insets) else None


def _same_insets(a, b) -> bool:
    return all(abs(x - y) <= _MARGIN_SLACK_IN for x, y in zip(a, b))


def _inset_words(inset) -> str:
    """The four insets as a sentence, collapsed where they agree."""
    left, top, right, bottom = (round(value, 2) for value in inset)
    if left == right and top == bottom:
        return f"{left:g}in at the sides and {top:g}in top and bottom"
    return f"{left:g}/{top:g}/{right:g}/{bottom:g}in"
