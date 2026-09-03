"""Title and subtitle rules: presence, structure, consistency."""

from __future__ import annotations

from typing import Iterable

from ..models import Category, Issue, Severity, TextRole
from .base import Rule, RuleContext


class MissingTitleRule(Rule):
    id = "title.missing"
    category = Category.TITLE
    description = "Content slide has no title."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            if slide.hidden:
                continue
            titles = [s for s in slide.shapes if s.role is TextRole.TITLE]
            if not titles:
                yield self.issue(
                    "Slide has no title shape.",
                    slide=slide,
                    expected="one title placeholder",
                    found="none",
                )
            elif not any(s.text.strip() for s in titles):
                yield self.issue(
                    "Title placeholder is empty.",
                    slide=slide,
                    shape=titles[0],
                    expected="title text",
                    found="empty placeholder",
                )


class DetachedTitleRule(Rule):
    """A title typed into a loose text box instead of the placeholder.

    The slide looks right and behaves wrong: it will not follow a layout
    change, will not appear in the outline, and drifts by a few points every
    time someone nudges it.
    """

    id = "title.detached_textbox"
    category = Category.TITLE
    description = "Title is a free text box, not the layout placeholder."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            has_placeholder_title = any(
                s.role is TextRole.TITLE and s.placeholder_type
                for s in slide.shapes
            )
            if has_placeholder_title:
                continue
            for shape in slide.shapes:
                if shape.role is TextRole.TITLE and not shape.placeholder_type:
                    yield self.issue(
                        "Title text sits in a free text box.",
                        slide=slide,
                        shape=shape,
                        expected="title placeholder from the layout",
                        found=f"text box {shape.name!r}",
                        suggestion="Move the copy into the layout placeholder.",
                    )


class TitlePositionConsistencyRule(Rule):
    """Titles that do not line up slide to slide.

    Reported deck-level: a single title 0.2in low is not the finding, twelve
    titles at eleven different heights is.
    """

    id = "title.position_inconsistent"
    category = Category.TITLE
    description = "Title position varies across slides."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.position_in
        positions: dict[int, tuple[float, float]] = {}
        for slide in ctx.deck.slides:
            for shape in slide.shapes:
                if shape.role is TextRole.TITLE and shape.text.strip():
                    positions[slide.number] = (
                        shape.geometry.left_in,
                        shape.geometry.top_in,
                    )
                    break

        if len(positions) < 2:
            return

        # Modal position is the intended one; anything beyond tolerance of it
        # is drift.
        counts: dict[tuple[float, float], int] = {}
        for value in positions.values():
            rounded = (round(value[0], 2), round(value[1], 2))
            counts[rounded] = counts.get(rounded, 0) + 1
        modal = max(counts, key=lambda k: counts[k])

        off = {
            number: value
            for number, value in positions.items()
            if abs(value[0] - modal[0]) > tolerance
            or abs(value[1] - modal[1]) > tolerance
        }
        if off:
            yield self.issue(
                f"Title position differs on {len(off)} of {len(positions)} slides.",
                expected=f"{modal[0]:.2f}, {modal[1]:.2f}in",
                found=", ".join(
                    f"slide {n} at {v[0]:.2f}, {v[1]:.2f}in"
                    for n, v in sorted(off.items())
                ),
            )


class SubtitleRule(Rule):
    """Subtitle presence and pairing.

    A subtitle with no title above it, or a required subtitle missing from the
    cover, both read as a broken hierarchy.
    """

    id = "subtitle.structure"
    category = Category.SUBTITLE
    description = "Subtitle is missing where required, or orphaned from a title."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        spec = ctx.spec.roles.get(TextRole.SUBTITLE.value)

        for slide in ctx.deck.slides:
            subtitles = [
                s for s in slide.shapes
                if s.role is TextRole.SUBTITLE and s.text.strip()
            ]
            titles = [
                s for s in slide.shapes
                if s.role is TextRole.TITLE and s.text.strip()
            ]
            if subtitles and not titles:
                yield self.issue(
                    "Subtitle with no title on the slide.",
                    slide=slide,
                    shape=subtitles[0],
                    expected="title above the subtitle",
                    found="subtitle only",
                )
            if spec and spec.required and slide.number == 1 and not subtitles:
                yield self.issue(
                    "Cover slide has no subtitle.",
                    slide=slide,
                    severity=Severity.ERROR,
                    expected="subtitle present",
                    found="none",
                )
