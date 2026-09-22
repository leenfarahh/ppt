"""Whether the text on a slide can be read off the thing it sits on.

A QUESTION THE PALETTE RULES CANNOT ASK. Every colour on a slide can be on the
brand palette and the slide still be unreadable: the palette says which colours
are allowed, never which pairs of them may be stacked. Delta-E, which the
palette rules measure with, answers "do these read as the same colour" and says
nothing about legibility -- a mid grey and a navy sit far apart in Lab and white
text is fine on one and gone on the other. That is a luminance question, and
`colorutil.contrast_ratio` is WCAG's answer to it.

The machinery has been here since the colour plan was written and has only ever
been used as a VETO: `apply.fixers._refuse_illegible` stands a recolour down
when it would leave the text on a shape unreadable, and its own comment says
that a shape whose text was ALREADY unreadable "is a finding of its own". It
was not. This is that finding.

WHY IT BELONGS AFTER THE MASTER GOES ON. `pipeline.run` restyles the deck onto
the master before the rules measure anything, so every rule here reads the deck
the designer will actually send. That ordering matters more for this rule than
for any other, because the restyle is the commonest way a deck acquires the
defect: a card that was pale is snapped to the palette entry nearest it, the
copy on it keeps the dark colour it was typed in, and a pairing nobody chose
is the one that goes out. Measuring the deck as it arrived would report neither
the defect the restyle fixed nor the one it caused.

WHAT IT REFUSES TO ANSWER, and the list is the point of the rule rather than a
limitation of it. Contrast needs both colours, and the file states one of them
far less often than it looks:

    text over a photograph, a gradient or a pattern
    text whose own colour is inherited and stated nowhere
    a fill or a background carrying lumMod, tint or shade
    text on a shape whose fill is a picture

Every one of those is silent here, and the first is the reason the design check
next door is worth having: a model looking at a render can see a caption
disappear into a photograph, and no reading of the file ever will.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..colorutil import contrast_ratio, delta_e
from ..models import (
    Category,
    DeckProfile,
    Issue,
    Severity,
    ShapeProfile,
    SlideProfile,
    walk_shapes,
)
from .base import Rule, RuleContext
from .colors import _resolved, master_text_color, master_text_colors

# WCAG AA, and both of its numbers rather than one. 4.5:1 is the floor for body
# copy and 3.0:1 for large text, where large is 18pt, or 14pt when it is bold.
#
# `colorutil.TEXT_CONTRAST_FLOOR` is the single number 3.0 and stays that way,
# because it guards a FIX -- holding a recolour to the body figure declined
# corrections a designer would have waved through, and a guard that is too
# strict leaves the colour wrong instead. A report is the other way round: it
# costs a line on a page, and a 9pt caption at 3.5:1 is genuinely hard to read
# off a projector at the back of a room.
_BODY_FLOOR = 4.5
_LARGE_FLOOR = 3.0
_LARGE_PT = 18.0
_LARGE_BOLD_PT = 14.0

# Under this, nothing is readable at any size and the slide has a defect rather
# than a preference. It is also `_LARGE_FLOOR`: below the floor for the largest
# type on the deck, there is no size at which the pairing is defensible.
_UNREADABLE = _LARGE_FLOOR

# Text this rule does not judge. A page number set in the master's pale grey is
# furniture, deliberately quiet, and reporting it on every slide of a ninety
# slide deck would bury everything else this rule finds.
_CHROME = frozenset({"FOOTER", "SLIDE_NUMBER", "DATE"})


class TextContrastRule(Rule):
    """Text that cannot be read off the colour behind it.

    ONE FINDING PER COLOUR ON A SHAPE, not one per run and not one per shape.

    Not per run, because a heading is routinely four runs -- somebody retyped a
    word and the file split there -- and four identical lines on a report about
    one heading is how a report stops being read.

    Not per shape either, and that took a real deck to settle. A card on one
    carried a red label and a black paragraph on a red fill: the label was
    invisible at 1.0:1 and the paragraph hard work at 2.8:1, two defects with
    two different answers. Reported as one finding on the shape's worst run,
    the fix recoloured the label, the card came back still carrying black text
    at 2.8:1, and the second defect needed a second round to be seen at all.
    Grouping on the colour reports both, and `fix_text_contrast` matches runs
    by the colour the finding names, so both are corrected in one pass.

    The severity splits on whether the pairing is bad or hopeless. Below 3.0:1
    nothing is readable at any size, which is an error; between there and the
    floor for its size it is a warning, because a designer looking at it may
    reasonably decide a 16pt label at 4.0:1 is fine on a screen in a room they
    know.
    """

    id = "color.text.contrast"
    category = Category.COLOR
    description = "Text does not contrast enough with what it sits on."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide in ctx.deck.slides:
            behind: dict[int, Optional[str]] = {}
            ordered = list(walk_shapes(slide.shapes))
            for index, shape in enumerate(ordered):
                if shape.placeholder_token in _CHROME:
                    continue
                if shape.table is not None:
                    yield from self._table(ctx, slide, shape, ordered, index)
                    continue
                if not shape.paragraphs:
                    continue
                found = self._pairings(ctx, slide, shape, ordered, index, behind)
                for ink, (ratio, background, floor) in sorted(found.items()):
                    target, why = _legible_ink(
                        ctx, slide, shape, background, ink, floor
                    )
                    yield self.issue(
                        f"Text in #{ink} on #{background} reads at "
                        f"{ratio:.1f}:1, under the {floor:g}:1 this size needs.",
                        slide=slide,
                        shape=shape,
                        severity=(
                            Severity.ERROR if ratio < _UNREADABLE
                            else Severity.WARNING
                        ),
                        # THE FIXER READS THESE AS COLOURS AND IN THIS ORDER:
                        # `fix_text_contrast` recolours the runs carrying the
                        # hex in `found` to the FIRST hex in `expected`, and
                        # re-measures against the SECOND before it writes.
                        #
                        # A FINDING WITH NO TARGET CARRIES NO HEX AT ALL, and
                        # that is not tidiness. `fixers._hex_of` takes the
                        # first six hex digits it finds in the string, so an
                        # `expected` that read "4.5:1 against #FFFFFF" would
                        # hand the fixer the BACKGROUND as the colour to set --
                        # pale grey type on white, recoloured to white, and
                        # reported as a correction.
                        expected=(
                            f"#{target} on #{background}, {why}" if target
                            else f"{floor:g}:1 against the fill under it, which "
                                 "no colour the master writes text in reaches"
                        ),
                        found=f"#{ink}, at {ratio:.1f}:1",
                        suggestion=(
                            f"Recolour the text to #{target} ({why})." if target
                            else "Move the text onto a fill it can be read off, "
                                 "or recolour that fill: no colour this master "
                                 "sets text in clears the floor on this "
                                 "background."
                        ),
                    )

    def _table(
        self,
        ctx: RuleContext,
        slide: SlideProfile,
        shape: ShapeProfile,
        ordered: list[ShapeProfile],
        index: int,
    ) -> Iterable[Issue]:
        """A table's cells, which no other path here reaches.

        A TABLE IS NOT A SHAPE WITH PARAGRAPHS ON IT. Its copy lives on its
        cells, each with a fill of its own, and `ShapeProfile.table` keeps them
        out of `children` deliberately -- so a rule walking `shape.paragraphs`
        sees a table as an empty graphic frame and measures nothing. Which left
        unmeasured the single commonest place this defect lives on a client
        deck: a header row filled in the brand colour with white type on it,
        where the brand colour is a mid tone.

        GROUPED ON THE PAIRING, ACROSS THE WHOLE TABLE. A header row of five
        cells all carrying white on the same navy is one decision and one
        defect, and five identical lines about it is five times less likely to
        be read. The count goes in the sentence so it is clear how much of the
        table is affected.

        A cell whose fill is a gradient or a picture, or that states no fill
        and takes the table style's, is skipped rather than measured against
        whatever is behind the table: a banded table style paints alternate
        rows and says so nowhere this can read.
        """
        under = _behind(slide, shape, ordered, index, ctx.deck)
        worst: dict = {}
        for cell in shape.table.cells:
            if cell.spanned:
                continue
            background = under
            if cell.fill_kind == "solid" or (
                cell.fill_kind is None and (cell.fill_hex or cell.fill_theme)
            ):
                background = _resolved(
                    cell.fill_hex, cell.fill_theme, ctx.deck
                )[0]
            elif cell.fill_kind in _OPAQUE_UNKNOWN:
                continue
            if not background:
                continue
            background = background.upper()
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    if not run.text.strip():
                        continue
                    ink = _resolved(run.color_hex, run.color_theme, ctx.deck)[0]
                    if not ink:
                        continue
                    ink = ink.upper()
                    ratio = contrast_ratio(ink, background)
                    if ratio is None:
                        continue
                    floor = _floor_for(run.size_pt, run.bold)
                    if ratio >= floor:
                        continue
                    seen = worst.get((ink, background))
                    if seen is None or ratio < seen[0]:
                        worst[(ink, background)] = (ratio, floor, seen[2] + 1
                                                    if seen else 1)
                    else:
                        worst[(ink, background)] = (
                            seen[0], seen[1], seen[2] + 1
                        )

        for (ink, background), (ratio, floor, cells) in sorted(worst.items()):
            target, why = _legible_ink(
                ctx, slide, shape, background, ink, floor
            )
            yield self.issue(
                f"Text in #{ink} on #{background} reads at {ratio:.1f}:1 in "
                f"{cells} cell(s) of this table, under the {floor:g}:1 this "
                "size needs.",
                slide=slide,
                shape=shape,
                severity=(
                    Severity.ERROR if ratio < _UNREADABLE else Severity.WARNING
                ),
                expected=(
                    f"#{target} on #{background}, {why}" if target
                    else f"{floor:g}:1 against the cell fill, which no colour "
                         "the master writes text in reaches"
                ),
                found=f"#{ink}, at {ratio:.1f}:1",
                suggestion=(
                    f"Recolour the text in those cells to #{target} ({why})."
                    if target else
                    "Recolour the cells: no colour this master sets text in "
                    "can be read on this fill."
                ),
            )

    def _pairings(
        self,
        ctx: RuleContext,
        slide: SlideProfile,
        shape: ShapeProfile,
        ordered: list[ShapeProfile],
        index: int,
        behind: dict,
    ) -> dict:
        """Every colour on this shape that cannot be read, worst case each.

        Keyed on the ink, which is what makes a heading split across four runs
        one finding and a red label beside a black paragraph two. The value is
        the worst that colour reads anywhere on the shape, so a colour set at
        two sizes is judged at the size that struggles.

        The background is worked out once per shape and cached on the way past,
        because it is the expensive half -- every shape drawn before this one
        has to be looked at -- and every run on the shape shares it.
        """
        inherited = _inherited_ink(ctx, slide, shape)
        found: dict = {}
        for paragraph in shape.paragraphs:
            for run in paragraph.runs:
                if not run.text.strip():
                    continue
                ink = _resolved(run.color_hex, run.color_theme, ctx.deck)[0]
                ink = (ink or inherited or "").upper()
                if not ink:
                    continue
                if index not in behind:
                    behind[index] = _behind(slide, shape, ordered, index, ctx.deck)
                background = behind[index]
                if not background:
                    continue
                ratio = contrast_ratio(ink, background)
                if ratio is None:
                    continue
                floor = _floor_for(run.size_pt, run.bold)
                if ratio >= floor:
                    continue
                seen = found.get(ink)
                if seen is None or ratio < seen[0]:
                    found[ink] = (ratio, background, floor)
        return found


def _legible_ink(
    ctx: RuleContext,
    slide: SlideProfile,
    shape: ShapeProfile,
    background: str,
    ink: str,
    floor: float,
) -> tuple[Optional[str], str]:
    """A colour this text could be set in and be read, and where it came from.

    TWO ANSWERS, AND THE FIRST IS NOT A CHOICE THIS MAKES. The master already
    states what colour a title on this layout is, and it is not always the same
    one -- white on the cover, the accent on a divider, the dark text colour on
    a content layout. Where it states one and that one is legible on the
    measured background, it is the answer and nothing is being guessed at.

    The second is a choice and is bounded twice to make it a small one.

    THE SET IS THE COLOURS THE MASTER WRITES WORDS IN, not the theme scheme.
    `colors.master_text_colors` explains why at length and the reason applies
    exactly here: the scheme is twelve swatches, most of them meant for fills
    and chrome, and picking a text colour out of all twelve is how a heading
    ends up in an accent no layout in the master ever sets type in. Legible
    and wrong is not the correction this rule wants.

    AND WITHIN THAT SET, THE ONE NEAREST WHAT THE TEXT IS ALREADY IN. Nearest,
    not the highest contrast available, because the correction should be the
    smallest visible change that makes the copy readable: a caption in a pale
    grey that has to move becomes the darkest grey the master sets on text,
    not black.

    None when neither answers, which is a real outcome rather than a failure:
    a master that writes in three mid-tones has nothing that can be read on a
    mid-tone card, and the fix is to move the text off the card. The finding
    says so and stays work for a designer.
    """
    stated = master_text_color(ctx, slide, shape)
    if stated:
        ratio = contrast_ratio(stated[0], background)
        if ratio is not None and ratio >= floor:
            return stated[0].upper(), f"which the master sets on {stated[1]}"

    best: Optional[tuple[float, str]] = None
    for value in master_text_colors(ctx.spec).values():
        ratio = contrast_ratio(value, background)
        if ratio is None or ratio < floor:
            continue
        distance = delta_e(ink, value)
        if distance is None:
            continue
        if best is None or distance < best[0]:
            best = (distance, value.upper())
    if best is None:
        return None, ""
    return best[1], "the nearest colour the master sets on text that can be read there"


def _floor_for(size_pt: Optional[float], bold: Optional[bool]) -> float:
    """The ratio this run has to clear, by WCAG's definition of large text.

    A run that does not state its size is held to the BODY floor. Its size is
    inherited from a placeholder or a theme, so it could be anything, and the
    two ways of being wrong are not worth the same: holding a 24pt heading to
    4.5:1 costs a line somebody dismisses, and letting a 9pt caption through
    at 3.2:1 costs the thing the rule exists for.
    """
    if size_pt is None:
        return _BODY_FLOOR
    if size_pt >= _LARGE_PT or (bold and size_pt >= _LARGE_BOLD_PT):
        return _LARGE_FLOOR
    return _BODY_FLOOR


def _inherited_ink(ctx: RuleContext, slide, shape) -> Optional[str]:
    """The colour the master gives this placeholder, for a run that states none.

    WITHOUT THIS THE RULE WOULD MOSTLY BE SILENT ON A TIDY DECK, which is the
    wrong way round. A run carries a colour of its own when somebody typed one
    over the top; a placeholder left to inherit is the deck doing as it was
    told, and after the restyle that is most of the copy on it. The master has
    already said what colour a title on this layout is -- `master_text_color`
    walks the same chain the colour rules read it from -- so the pairing can be
    measured without guessing at anything.
    """
    answer = master_text_color(ctx, slide, shape)
    return answer[0] if answer else None


def _behind(
    slide: SlideProfile,
    shape: ShapeProfile,
    ordered: list[ShapeProfile],
    index: int,
    deck: DeckProfile,
) -> Optional[str]:
    """The colour drawn under a shape's text, or None when it is not a colour.

    Three answers in order, and the order is what a reader's eye does:

      1. THE SHAPE'S OWN FILL. Text in a filled box sits on that box and on
         nothing else, whatever is under it.
      2. THE NEAREST FILLED SHAPE UNDER IT. An unfilled label on a card sits on
         the card. Nearest means last in document order among the shapes drawn
         before this one that contain it -- document order is z-order, so the
         last one to be drawn is the one the text lands on.
      3. THE SLIDE'S BACKGROUND, resolved through the layout, the master and
         the theme by `extract.deck_reader`.

    ANYTHING THAT IS DRAWN AND IS NOT ONE COLOUR ENDS THE SEARCH WITH None,
    rather than being stepped over as though it were not there. A picture is
    the case the rule most wants to report and is least able to -- a caption
    over a photograph, where reporting the white slide BEHIND the photograph
    would give the worst slide in the deck a clean bill of health -- and it is
    not the only one. `fill_hex` alone cannot tell a gradient card from an
    unfilled text box: both arrive as None, so a gradient was stepped straight
    over and its caption measured against the slide behind it, which is the
    same lie by a quieter route. A patterned fill is worse again, arriving as
    the pattern's foreground colour as though the whole shape were solid
    black. `fill_kind` is what tells the three apart.

    An inherited fill counts as unknown for the same reason. A shape that
    states no fill at all takes the theme's, and an autoshape drawing the
    theme's accent behind a caption is a real colour that the file never
    names.
    """
    own = _resolved(shape.fill_hex, shape.fill_theme, deck)[0]
    if own and shape.fill_kind in _KNOWN_FILL:
        return own.upper()
    if shape.fill_kind in _OPAQUE_UNKNOWN:
        return None

    for candidate in reversed(ordered[:index]):
        if candidate is shape or not _contains(candidate, shape):
            continue
        if candidate.is_picture or candidate.fill_kind in _OPAQUE_UNKNOWN:
            return None
        if candidate.fill_kind not in _KNOWN_FILL:
            continue
        fill = _resolved(candidate.fill_hex, candidate.fill_theme, deck)[0]
        if fill:
            return fill.upper()
    return slide.background_hex


# A fill whose colour is the colour drawn. `solid` is the only one, and
# `None` is here for the shapes nothing read a kind off -- a profile built by
# hand in a test, or by a caller older than `fill_kind` -- which must keep
# behaving as it did rather than going silent.
_KNOWN_FILL = frozenset({"solid", None})

# Drawn, and not one colour. The search stops here rather than looking further
# back: whatever is behind this is not what the reader sees.
_OPAQUE_UNKNOWN = frozenset({"gradient", "picture", "textured", "pattern",
                             "inherit"})


def _contains(outer: ShapeProfile, inner: ShapeProfile) -> bool:
    """Whether one shape's box covers another's, within a hair.

    A hair, because a label drawn flush to the edge of the card it sits on is
    sitting on the card, and the two boxes agreeing to the thousandth of an
    inch is not something a deck built by hand ever does.
    """
    a, b = outer.geometry, inner.geometry
    if a is None or b is None or a.width_in <= 0 or a.height_in <= 0:
        return False
    return (
        a.left_in - _EDGE_SLACK_IN <= b.left_in
        and a.top_in - _EDGE_SLACK_IN <= b.top_in
        and a.left_in + a.width_in + _EDGE_SLACK_IN >= b.left_in + b.width_in
        and a.top_in + a.height_in + _EDGE_SLACK_IN >= b.top_in + b.height_in
    )


_EDGE_SLACK_IN = 0.02
