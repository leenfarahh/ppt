"""Typography rules: orphans, widows, and the text hygiene around them.

The orphan and widow checks depend on rendered line breaks, which a .pptx does
not store -- see formatting_tool/linemetrics.py. They stay silent unless a
line-metrics provider is wired in. The remaining rules here need no renderer
and catch the manual workarounds people use *instead* of fixing a bad wrap,
which in practice is where most of these defects are visible anyway.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..linemetrics import LineMetricsProvider, NullLineMetrics, ShapeKey
from ..models import (
    Category,
    Issue,
    Severity,
    ShapeProfile,
    SlideProfile,
    TextRole,
)
from .base import Rule, RuleContext


class OrphanWidowRule(Rule):
    """A lone word on the last line, or a stub pushed onto a new line.

    Requires a line-metrics provider. With NullLineMetrics it reports nothing.
    """

    id = "typography.orphan_widow"
    category = Category.TYPOGRAPHY
    description = "A line ends with a single stranded word."
    default_severity = Severity.WARNING

    def __init__(self, metrics: LineMetricsProvider | None = None) -> None:
        self.metrics = metrics or NullLineMetrics()

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        if not self.metrics.available:
            return

        rules = ctx.guidelines.typography
        for slide, shape in ctx.text_shapes():
            lines = self.metrics.lines(ShapeKey(slide.number, shape.shape_id))
            if not lines or len(lines) < 2:
                continue

            last = lines[-1].strip()
            if not last:
                continue

            words = last.split()
            # The widest line the renderer drew is the only evidence anyone
            # has about how much text fits on a line in this shape, and the
            # fix that binds two words into one unbreakable token cannot be
            # made safely without it. Carried on the finding rather than
            # re-measured later: measuring means driving PowerPoint, and the
            # measurement has already been done to get here.
            widest = max(len(line.strip()) for line in lines)
            if len(words) <= rules.max_orphan_words:
                yield self.issue(
                    f"Last line is a single stranded word ({last!r}).",
                    slide=slide,
                    shape=shape,
                    expected=f"more than {rules.max_orphan_words} word(s) on the last line",
                    found=last,
                    suggestion="Widen the box, edit the copy, or insert a non-breaking space.",
                    widest_line_chars=widest,
                )
            elif len(last) < rules.min_widow_chars:
                yield self.issue(
                    f"Last line is a {len(last)}-character stub ({last!r}).",
                    slide=slide,
                    shape=shape,
                    expected=f">= {rules.min_widow_chars} characters",
                    found=last,
                )

            if shape.role is TextRole.TITLE and rules.max_title_lines:
                if len(lines) > rules.max_title_lines:
                    yield self.issue(
                        f"Title wraps to {len(lines)} lines.",
                        slide=slide,
                        shape=shape,
                        severity=Severity.ERROR,
                        expected=f"<= {rules.max_title_lines} lines",
                        found=f"{len(lines)} lines",
                    )


class HeadingBalanceRule(Rule):
    """A row of parallel headings that do not all take the same number of lines.

    Four column headings, two of them wrapping to a second line and two
    sitting on one: "MBA SURVEY*" and "MBA CASEBOOK" short, "TAILORED
    TRAININGS*" and "AMBASSADORS SITE ON OURSPACE*" long. Every heading is
    correct on its own and the row reads as ragged, because what the eye
    compares across a row of columns is not the words but the block each one
    makes.

    Requires a renderer. How many lines a heading takes is not in the file --
    a .pptx stores a paragraph and a box -- so without one this reports
    nothing rather than guessing from character counts.

    Equalised UP, to the longest in the row, and that is not arbitrary: a
    break can always be added to a short heading, and taking one out of a long
    one needs a wider box or smaller type, which are the designer's to give.

    Only a row of the same box at the same height, three or more of them. Two
    same-sized boxes are a coincidence often enough to matter, and a set that
    is not in a row is not being compared by anyone.

    Not reported when the row is more than one line apart. A row of one-line
    headings beside a four-line one is a copy-length problem, and balancing to
    four would make three bad headings out of one.
    """

    id = "typography.heading_balance"
    category = Category.TYPOGRAPHY
    description = "Parallel headings in a row take different numbers of lines."
    default_severity = Severity.WARNING

    def __init__(self, metrics: LineMetricsProvider | None = None) -> None:
        self.metrics = metrics or NullLineMetrics()

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        if not self.metrics.available:
            return
        for slide in ctx.deck.slides:
            for row in _heading_rows(slide):
                counts = {}
                for shape in row:
                    lines = self.metrics.lines(
                        ShapeKey(slide.number, shape.shape_id)
                    )
                    if not lines:
                        counts = {}
                        break
                    counts[shape.shape_id] = len(lines)
                if not counts or len(set(counts.values())) < 2:
                    continue

                longest = max(counts.values())
                if longest - min(counts.values()) > 1 or longest > _BALANCE_TO:
                    continue

                for shape in row:
                    have = counts[shape.shape_id]
                    if have >= longest:
                        continue
                    yield self.issue(
                        f"This heading takes {have} line(s) where the "
                        f"{len(row)} headings beside it take {longest}, so the "
                        "row reads ragged.",
                        slide=slide,
                        shape=shape,
                        expected=f"{longest} lines, as the rest of the row",
                        found=f"{have} line(s)",
                        suggestion=(
                            f"Break it across {longest} lines at a word, or "
                            "reword it. Widening one box of the row would put "
                            "the row out of line instead."
                        ),
                    )


# The most lines a row is balanced to. Two is the case this exists for and
# three is defensible; past that a heading is a paragraph and the row's
# raggedness is the smaller of its problems.
_BALANCE_TO = 3

# How many boxes make a row rather than a coincidence. The same number the
# repeat rules use for a majority.
_ROW_NEEDS = 3


def _heading_rows(slide: SlideProfile) -> list[list[ShapeProfile]]:
    """Sets of same-sized text boxes sitting at the same height.

    The signal a row of column headings actually carries. Grouped on the box
    rather than on the copy, because the copy is what differs and the box is
    what was drawn once and repeated -- the same reasoning `repeats._series`
    uses, with the top added: a set that is not in a row is not being read as
    a row.
    """
    buckets: dict[tuple, list[ShapeProfile]] = {}
    for shape in slide.shapes:
        if shape.is_group or not shape.text.strip():
            continue
        box = shape.geometry
        if not box.width_in or not box.height_in:
            continue
        key = (
            round(box.width_in, 2),
            round(box.height_in, 2),
            round(box.top_in, 1),
        )
        buckets.setdefault(key, []).append(shape)
    return [
        sorted(members, key=lambda s: s.geometry.left_in)
        for members in buckets.values()
        if len(members) >= _ROW_NEEDS
    ]


class ManualLineBreakRule(Rule):
    """Soft returns used to force a wrap.

    The usual fix for a bad wrap, and the reason a deck breaks the moment the
    copy or the box width changes.

    With one exception, and it is the tool's own doing: `heading_balance`
    breaks a short heading across two lines to square up a row, and a soft
    return is the only way to choose WHERE that break falls. Reporting those
    would be the tool filing a defect against its own fix, so a shape in a row
    whose headings all take the same number of lines is left alone -- the
    break is doing the job the rule is otherwise warning about the absence of.

    Judged on the row rather than on a marker in the file, because a designer
    who balances a row by hand has done the same correct thing and should not
    be told off for it either.
    """

    id = "typography.manual_line_break"
    category = Category.TYPOGRAPHY
    description = "Text uses manual line breaks to control wrapping."
    default_severity = Severity.WARNING

    def __init__(self, metrics: LineMetricsProvider | None = None) -> None:
        self.metrics = metrics or NullLineMetrics()

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            balanced = self._balanced_shapes(slide)
            for shape in slide.shapes:
                if shape.is_group:
                    continue
                # python-pptx renders a soft return (a:br) as a vertical tab.
                count = shape.text.count("\v")
                if not count or shape.shape_id in balanced:
                    continue
                yield self.issue(
                    f"{count} manual line break(s) force the wrap.",
                    slide=slide,
                    shape=shape,
                    expected="natural wrapping",
                    found=f"{count} soft return(s)",
                    suggestion="Remove the breaks and size the box to the copy.",
                )

    def _balanced_shapes(self, slide: SlideProfile) -> set:
        """Shapes in a row of headings that all take the same line count."""
        if not self.metrics.available:
            return set()
        balanced = set()
        for row in _heading_rows(slide):
            counts = []
            for shape in row:
                lines = self.metrics.lines(ShapeKey(slide.number, shape.shape_id))
                if not lines:
                    counts = []
                    break
                counts.append(len(lines))
            if counts and len(set(counts)) == 1 and counts[0] > 1:
                balanced.update(shape.shape_id for shape in row)
        return balanced


class WhitespaceHygieneRule(Rule):
    """Double spaces, trailing spaces, and spaces used as indentation."""

    id = "typography.whitespace"
    category = Category.TYPOGRAPHY
    description = "Stray or repeated whitespace in the copy."
    default_severity = Severity.INFO

    _DOUBLE_SPACE = re.compile(r"\S {2,}\S")

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, shape in ctx.text_shapes():
            for paragraph in shape.paragraphs:
                text = paragraph.text
                if not text.strip():
                    continue
                if self._DOUBLE_SPACE.search(text):
                    yield self.issue(
                        "Repeated spaces inside a line.",
                        slide=slide,
                        shape=shape,
                        found=text[:80],
                    )
                elif text != text.rstrip():
                    yield self.issue(
                        "Trailing whitespace.",
                        slide=slide,
                        shape=shape,
                        found=repr(text[-20:]),
                    )


class TitlePunctuationRule(Rule):
    """Terminal punctuation on titles, and inconsistent bullet punctuation."""

    id = "typography.terminal_punctuation"
    category = Category.TYPOGRAPHY
    description = "Title ends in a full stop, or bullets punctuate inconsistently."
    default_severity = Severity.INFO

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, shape in ctx.text_shapes():
            if shape.role is TextRole.TITLE and shape.text.strip().endswith("."):
                yield self.issue(
                    "Title ends in a full stop.",
                    slide=slide,
                    shape=shape,
                    expected="no terminal punctuation on titles",
                    found=shape.text.strip()[-40:],
                )

        # TODO: bullet-level consistency. Group paragraphs by (shape, level),
        # then report a group where some entries end in a full stop and others
        # do not. Needs a minimum group size (3) to avoid firing on a two-item
        # list where one item is a full sentence by design.
