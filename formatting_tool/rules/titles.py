"""Title and subtitle rules: presence, structure, consistency."""

from __future__ import annotations

from typing import Iterable

from ..models import Category, Issue, Severity, ShapeProfile, SlideProfile, TextRole
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

    Reported per slide when a majority of the titles agree on a position, and
    deck-level when they do not. Those are two different findings wearing one
    rule id. Twelve titles at eleven different heights is a fact about the
    deck and there is no odd one out to name; twelve titles at one height and
    a thirteenth 0.3in low is a fact about slide thirteen, and saying so is
    what lets it be corrected -- a deck-level finding names no shape, so the
    applier has nothing to move and it reaches a designer unfixed.

    The majority gate is the same one the repeat rules use. Below it there is
    no intended position, only a scatter.
    """

    id = "title.position_inconsistent"
    category = Category.TITLE
    description = "Title position varies across slides."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.position_in
        titles: dict[int, tuple[SlideProfile, ShapeProfile]] = {}
        for slide in ctx.deck.slides:
            for shape in slide.shapes:
                if shape.role is TextRole.TITLE and shape.text.strip():
                    titles[slide.number] = (slide, shape)
                    break

        if len(titles) < 2:
            return

        # Modal position is the intended one; anything beyond tolerance of it
        # is drift.
        counts: dict[tuple[float, float], int] = {}
        for _slide, shape in titles.values():
            rounded = (
                round(shape.geometry.left_in, 2),
                round(shape.geometry.top_in, 2),
            )
            counts[rounded] = counts.get(rounded, 0) + 1
        modal = max(counts, key=lambda k: counts[k])

        off = [
            (number, slide, shape)
            for number, (slide, shape) in sorted(titles.items())
            if abs(shape.geometry.left_in - modal[0]) > tolerance
            or abs(shape.geometry.top_in - modal[1]) > tolerance
        ]
        if not off:
            return

        where = f"{modal[0]:.2f}, {modal[1]:.2f}in"
        if counts[modal] < ctx.tuning.majority_fraction * len(titles):
            yield self.issue(
                f"Title position differs on {len(off)} of {len(titles)} slides, "
                "with no position the majority agrees on.",
                expected="one title position across the deck",
                found=", ".join(
                    f"slide {n} at {s.geometry.left_in:.2f}, "
                    f"{s.geometry.top_in:.2f}in"
                    for n, _slide, s in off
                ),
            )
            return

        for _number, slide, shape in off:
            drift = max(
                abs(shape.geometry.left_in - modal[0]),
                abs(shape.geometry.top_in - modal[1]),
            )
            yield self.issue(
                f"Title sits {drift:.2f}in from where the other "
                f"{counts[modal]} titles sit.",
                slide=slide,
                shape=shape,
                expected=where,
                found=(
                    f"{shape.geometry.left_in:.2f}, "
                    f"{shape.geometry.top_in:.2f}in"
                ),
                suggestion=f"Move the title to {where}, as the rest of the deck has.",
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
