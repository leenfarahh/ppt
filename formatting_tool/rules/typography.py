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
from ..models import Category, Issue, Severity, TextRole
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
            if len(words) <= rules.max_orphan_words:
                yield self.issue(
                    f"Last line is a single stranded word ({last!r}).",
                    slide=slide,
                    shape=shape,
                    expected=f"more than {rules.max_orphan_words} word(s) on the last line",
                    found=last,
                    suggestion="Widen the box, edit the copy, or insert a non-breaking space.",
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


class ManualLineBreakRule(Rule):
    """Soft returns used to force a wrap.

    The usual fix for a bad wrap, and the reason a deck breaks the moment the
    copy or the box width changes.
    """

    id = "typography.manual_line_break"
    category = Category.TYPOGRAPHY
    description = "Text uses manual line breaks to control wrapping."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, shape in ctx.text_shapes():
            # python-pptx renders a soft return (a:br) as a vertical tab.
            count = shape.text.count("\v")
            if count:
                yield self.issue(
                    f"{count} manual line break(s) force the wrap.",
                    slide=slide,
                    shape=shape,
                    expected="natural wrapping",
                    found=f"{count} soft return(s)",
                    suggestion="Remove the breaks and size the box to the copy.",
                )


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
