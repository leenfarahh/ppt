"""Rule protocol and the context every rule receives.

A rule is deliberately small: it takes a RuleContext and yields Issues. No rule
reads a file, calls an API, or knows about other rules. That keeps each one
unit-testable against a hand-built DeckProfile, with no fixture deck.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

from ..models import (
    BrandGuidelines,
    Category,
    DeckProfile,
    Issue,
    MasterSpec,
    RuleTuning,
    ParagraphProfile,
    RunProfile,
    Severity,
    ShapeProfile,
    SlideProfile,
    Source,
    walk_shapes,
)


@dataclass
class RuleContext:
    """Everything a rule is allowed to look at."""

    deck: DeckProfile
    spec: MasterSpec

    @property
    def guidelines(self) -> BrandGuidelines:
        return self.spec.guidelines

    @property
    def tuning(self) -> RuleTuning:
        """Rule thresholds. Not brand values; see models.RuleTuning."""
        return self.spec.guidelines.tuning

    @property
    def has_guidelines(self) -> bool:
        """False when no brand file was supplied.

        Rules that can only fire against authored guidelines opt out via
        Rule.requires_guidelines rather than silently reporting nothing.
        """
        return bool(
            self.guidelines.palette
            or self.guidelines.allowed_fonts
            or self.guidelines.roles
        )

    def shapes(self) -> Iterator[tuple[SlideProfile, ShapeProfile]]:
        """Every shape on every slide, descending into groups."""
        for slide in self.deck.slides:
            yield from ((slide, shape) for shape in walk_shapes(slide.shapes))

    def text_shapes(self) -> Iterator[tuple[SlideProfile, ShapeProfile]]:
        for slide, shape in self.shapes():
            if shape.paragraphs:
                yield slide, shape

    def runs(
        self,
    ) -> Iterator[tuple[SlideProfile, ShapeProfile, ParagraphProfile, RunProfile]]:
        """Every non-empty text run, the unit most formatting rules work on.

        A TABLE'S COPY IS IN HERE TOO, and it was not. A table's text lives on
        its cells, `ShapeProfile.table` keeps those out of `children` on
        purpose -- a cell is not a shape, and every geometric rule walking into
        one would report margins and overlaps on forty-two boxes none of them
        was written for -- and the effect was that no rule reading runs saw a
        word of it. A deck's tables went out with their text in whatever colour
        and typeface they arrived in, measured by nothing.

        The cell's own paragraphs are yielded against the TABLE's shape, which
        is the shape a finding can be addressed to and the shape a fixer is
        handed. Which cell it was is not carried, because every fixer here
        matches on the value the finding measured rather than on an address.
        """
        for slide, shape in self.text_shapes():
            for paragraph in shape.paragraphs:
                for run in paragraph.runs:
                    if run.text.strip():
                        yield slide, shape, paragraph, run

        for slide, shape in self.shapes():
            if shape.table is None:
                continue
            for cell in shape.table.cells:
                if cell.spanned:
                    continue
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        if run.text.strip():
                            yield slide, shape, paragraph, run


class Rule(ABC):
    """Base class for a deterministic check."""

    id: str = "unset"
    category: Category = Category.OTHER
    description: str = ""
    default_severity: Severity = Severity.WARNING
    requires_guidelines: bool = False

    @abstractmethod
    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        """Yield one Issue per inconsistency found."""

    def applies(self, ctx: RuleContext) -> bool:
        return ctx.has_guidelines or not self.requires_guidelines

    # -- helper so rules never hand-assemble an Issue ----------------------- #

    def issue(
        self,
        message: str,
        *,
        slide: Optional[SlideProfile] = None,
        shape: Optional[ShapeProfile] = None,
        severity: Optional[Severity] = None,
        expected: Optional[str] = None,
        found: Optional[str] = None,
        suggestion: Optional[str] = None,
        widest_line_chars: Optional[int] = None,
    ) -> Issue:
        return Issue(
            category=self.category,
            severity=severity or self.default_severity,
            message=message,
            source=Source.RULE,
            rule_id=self.id,
            slide=slide.number if slide else None,
            shape=shape.name if shape else None,
            shape_id=shape.shape_id if shape else None,
            expected=expected,
            found=found,
            suggestion=suggestion,
            widest_line_chars=widest_line_chars,
        )
