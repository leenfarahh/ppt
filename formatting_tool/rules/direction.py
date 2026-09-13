"""Which way a slide reads, and what has to follow it.

Three rules, one subject: a deck written in Arabic reads from the right, and
every part of it that states a side has to say the same one. They are together
here rather than spread across `fonts` and `space` because they share their
evidence -- the script the copy is written in -- and because a deck that fails
one of them usually fails the others for the same reason: it was built, or
rebuilt, on a frame drawn for English.

WHAT EACH ONE ANSWERS, and they are deliberately different questions:

- `typography.rtl_not_set` is about the paragraph's DIRECTION. Without
  `a:pPr/@rtl` the letters still shape and join, and the punctuation, the
  numbers and any Latin inside the line land in the wrong place.
- `typography.rtl_alignment` is about the paragraph's SIDE. A paragraph
  explicitly set flush left is flush left however its direction is marked:
  `algn="l"` names an edge, not a leading edge, so it survives the direction
  fix and leaves Arabic copy hanging off the wrong margin.
- `space.rtl_leading_edge` is about the SHAPE. Copy set flush right inside a
  box that starts at the English column is still reading from the left of the
  page, and that is the defect a rebuilt Arabic deck carries most often:
  pictures especially, because no other rule here measures a shape that holds
  no text.

SCOPE IS PER PARAGRAPH FOR THE FIRST TWO, and per deck for the third. An
Arabic paragraph is wrong-sided whatever the deck around it is doing, so a
bilingual deck's Arabic pull-quote is reported on an English slide. A shape,
by contrast, sits on the deck's own columns, and moving one across the page is
only right when the page as a whole reads that way.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional

from ..models import (
    MARGIN_CHROME,
    Category,
    Issue,
    ParagraphProfile,
    Severity,
    ShapeProfile,
)
from ..script import is_rtl
from .base import Rule, RuleContext


class RightToLeftRule(Rule):
    """Arabic copy in a paragraph that was never marked right-to-left.

    `a:pPr/@rtl` is what tells the renderer which way the paragraph runs, and
    a paragraph that does not set it inherits left-to-right. The Arabic
    letters still shape and join correctly -- that part is the font's job --
    so the slide looks almost right, which is what makes this easy to ship.
    What lands in the wrong place is everything that is not a letter: the full
    stop at the end of the sentence, a bracketed aside, a number, a Latin
    product name inside an Arabic line.

    Reported per paragraph, because that is the thing that carries the
    property and the thing a fix sets.
    """

    id = "typography.rtl_not_set"
    category = Category.TYPOGRAPHY
    description = "Arabic text sits in a paragraph not marked right-to-left."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, shape in ctx.text_shapes():
            unset = [
                paragraph for paragraph in shape.paragraphs
                if paragraph.rtl is not True and is_rtl(paragraph.text)
            ]
            if not unset:
                continue
            yield self.issue(
                f"{len(unset)} Arabic paragraph(s) are not marked "
                "right-to-left, so their punctuation and numbers are placed as "
                "though the copy were English.",
                slide=slide,
                shape=shape,
                expected="right-to-left set on every Arabic paragraph",
                found=(
                    f"{len(unset)} of {len(shape.paragraphs)} paragraph(s) "
                    "unmarked"
                ),
                suggestion=(
                    "Set the paragraph direction to right-to-left. It changes "
                    "no words and no typeface."
                ),
            )


class RtlAlignmentRule(Rule):
    """Arabic copy explicitly set flush left.

    NOT THE SAME DEFECT AS AN UNMARKED PARAGRAPH, and the difference is why
    marking a deck right-to-left does not fix it. `a:pPr/@algn` names an edge
    of the box: "l" is the left one whichever way the paragraph runs. So a
    paragraph carrying both `rtl="1"` and `algn="l"` reads right to left and
    sits against the left margin, with a ragged right edge where the reader
    starts. It is the state a deck lands in when Arabic copy is pasted into
    boxes an English deck left behind, and it survives every direction fix
    made anywhere else here.

    An unstated alignment is not reported. A paragraph that says nothing
    inherits, and what a right-to-left paragraph inherits is flush right --
    writing "l" is a deliberate act, so only "l" is read as one.

    CENTRED AND JUSTIFIED HAVE NO SIDE and are left alone. Centred copy reads
    the same either way, and justified copy justifies to whichever direction
    the paragraph runs, so both are already correct once the direction is set.

    Per paragraph rather than per deck: an Arabic paragraph on an otherwise
    English slide is against its own margin, and a bilingual deck is the
    normal case here rather than the exception.

    TABLE CELLS COUNT, minus the header row. A consulting deck states its
    comparisons in tables, so most of the Arabic in one is in cells; the header
    row is left out because `table.header_alignment` is already its subject and
    two rules reporting one row would have the designer fix it twice.
    """

    id = "typography.rtl_alignment"
    category = Category.TYPOGRAPHY
    description = "Arabic text is set flush left, against the way it reads."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, shape in ctx.shapes():
            paragraphs = list(_aligned_paragraphs(shape))
            wrong = [p for p in paragraphs if _flush_left(p)]
            if not wrong:
                continue
            yield self.issue(
                f"{len(wrong)} Arabic paragraph(s) are set flush left, so the "
                "copy sits against the margin the reader finishes at and rags "
                "at the one they start from.",
                slide=slide,
                shape=shape,
                expected="right aligned on every Arabic paragraph",
                found=f"{len(wrong)} of {len(paragraphs)} paragraph(s) left aligned",
                suggestion=(
                    "Set those paragraphs right aligned. Nothing moves and no "
                    "words change: the copy swaps which edge of its own box it "
                    "sits against."
                ),
            )


class RtlLeadingEdgeRule(Rule):
    """A shape in a right-to-left deck that still leads from the left.

    The defect an Arabic deck built on an English master carries on nearly
    every slide, and the one that survives having the copy inside each box set
    flush right: the BOX starts at the English column. A picture placed at
    0.92in in a 13.33in deck is 0.92in from where an English reader starts and
    11.4in from where an Arabic one does.

    PICTURES ARE THE REASON THIS EXISTS AS ITS OWN RULE. `space.alignment_grid`
    already measures a right-to-left deck on its trailing edge, but it reads
    copy: a shape with no text in it is skipped, which is right for the
    dividers and panels that make up most of the empty shapes on a slide and
    wrong for the photograph that is the whole content of the slide. And that
    rule reports NEAR misses only, by design -- a shape 0.06in off a column is
    drift worth naming, a shape deliberately placed elsewhere is not a defect.
    A shape on the wrong side of the page is neither: it is half a slide away
    from its column, far outside the near-miss window, and invisible to that
    rule for exactly the reason the window exists.

    WHAT MAKES IT PROVABLE rather than a matter of taste is the pair of
    conditions, and both are needed:

    - the shape's left edge sits on a column the MASTER declares, and
    - its right edge sits on none of them.

    That is the signature of a box placed against a left-hand frame. A shape
    the designer put somewhere of its own matches no column on either side and
    is left alone; a full-width shape spanning the frame matches on both and is
    left alone too.

    THE COLUMNS ARE THE MASTER'S, READ AS THE DESIGNER DREW THEM, and nothing
    has to be added to the master to make that work. The layout each slide is
    bound to states where content goes -- its placeholders, and the rectangles
    marked "PS" if it carries any -- and those edges are what a shape is
    measured against. That is why this reads the layouts directly instead of
    `MasterSpec.grid_edges_in`, which is deliberately gated behind the PS mark:
    that gate exists so a marked-up master is what changes `space.alignment_grid`
    on every deck at once, and it is not a licence to ask a designer to go and
    annotate a master they have already finished. Here it would only mean an
    unmarked master -- most of them -- reports nothing at all.

    Per slide, against its own layout. A two-column layout and a full-bleed one
    declare different columns, and the slide says which of them it is built on.
    A slide bound to a layout the master does not have falls back to every
    column the master states, which is `layout.not_in_master`'s finding to
    report and not this one's.

    THE TARGET IS THE MIRROR OF THE COLUMN IT SITS ON, and it is only reported
    when the master declares that mirrored column as well. A master drawn
    symmetrically states both; one whose right-hand frame is genuinely
    different does not, and there this says nothing rather than inventing a
    position. That is also what makes the finding fixable: the shape is not
    being moved somewhere a rule thinks it might look better, it is being moved
    onto a column the master drew.

    Chrome is exempt. A footer, a page number and a date live where the master
    puts them, which is a decision this has no evidence to overturn.

    Top-level shapes only. A group is one drawn object and whoever drew it
    decided how its parts sit, so a group moves whole or not at all -- the same
    line `rebuild.rtl` draws when it turns a deck round.
    """

    id = "space.rtl_leading_edge"
    category = Category.SPACE
    description = "A shape in a right-to-left deck starts at the left column."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        if not ctx.deck.rtl:
            return
        if not ctx.spec.layouts:
            return          # no master to follow, so nothing to measure against

        tolerance = ctx.spec.tolerances.position_in
        width = ctx.deck.width_in
        # Two page sizes are two coordinate spaces. Mirroring a 4:3 slide about
        # a 16:9 master's centre puts the shape somewhere neither file asked
        # for, so a deck of a different size is not measured here.
        if width <= 0 or abs(width - ctx.spec.width_in) > tolerance:
            return

        for slide in ctx.deck.slides:
            if slide.hidden:
                continue
            lefts, rights = _columns(ctx, slide.layout_name)
            if not lefts or not rights:
                continue
            for shape in slide.shapes:
                target = _mirror_target(shape, lefts, rights, width, tolerance)
                if target is None:
                    continue
                box = shape.geometry
                yield self.issue(
                    f"This {_what(shape)} starts at the {box.left_in:.2f}in "
                    f"column and ends at {box.right_in:.2f}in, which is no "
                    "column the master draws. The deck reads right to left, so "
                    f"it belongs against the mirrored column at {target:.2f}in.",
                    slide=slide,
                    shape=shape,
                    expected=f"right {target:.2f}in",
                    found=f"right {box.right_in:.2f}in",
                    suggestion=(
                        "Move it across so its right edge lands on "
                        f"{target:.2f}in. The width does not change: this is "
                        "the same box against the edge the reader starts from."
                    ),
                )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _aligned_paragraphs(shape: ShapeProfile) -> Iterator[ParagraphProfile]:
    """Every paragraph on a shape whose alignment is this rule's business.

    The shape's own copy, plus the body of a real table. Row 0 is left out:
    see RtlAlignmentRule on why the header belongs to one rule only.
    """
    yield from shape.paragraphs
    if shape.table is None:
        return
    for cell in shape.table.cells:
        if cell.row == 0 or cell.spanned:
            continue
        yield from cell.paragraphs


def _flush_left(paragraph: ParagraphProfile) -> bool:
    """An Arabic paragraph that states the left edge of its box."""
    stated = (paragraph.alignment or "").split(" ")[0].upper()
    return stated == "LEFT" and is_rtl(paragraph.text)


def _mirror_target(
    shape: ShapeProfile,
    lefts: list[float],
    rights: list[float],
    width: float,
    tolerance: float,
) -> Optional[float]:
    """Where this shape's right edge belongs, or None if it is not a case.

    None covers every reason to stay silent, and they are all the same kind of
    reason: the file does not say. A shape already on a right-hand column, a
    shape on no column at all, chrome, and a mirrored position the master never
    draws.
    """
    if shape.placeholder_token in MARGIN_CHROME:
        return None
    box = shape.geometry
    if box.width_in <= 0:
        return None
    if _on(rights, box.right_in, tolerance):
        return None                             # already leads from the right
    if not _on(lefts, box.left_in, tolerance):
        return None                             # placed somewhere of its own
    target = round(width - box.left_in, 2)
    if not _on(rights, target, tolerance):
        return None                             # the master draws no mirror
    return target


def _columns(
    ctx: RuleContext, layout_name: Optional[str]
) -> tuple[list[float], list[float]]:
    """The left and right edges the master offers content, for this slide.

    The slide's own layout when the master has it, and every layout's edges
    otherwise. Read off `LayoutProfile`, which already excludes the footer,
    page number and date: chrome lives in the margin by design and is not a
    column anything else should be measured against.
    """
    layout = ctx.spec.layout_named(layout_name)
    layouts = [layout] if layout is not None else list(ctx.spec.layouts)
    lefts: set[float] = set()
    rights: set[float] = set()
    for one in layouts:
        lefts.update(one.declared_left_edges)
        rights.update(one.declared_right_edges)
    return sorted(lefts), sorted(rights)


def _on(edges: list[float], value: float, tolerance: float) -> bool:
    return any(abs(edge - value) <= tolerance for edge in edges)


def _what(shape: ShapeProfile) -> str:
    """What to call the shape in a finding, so the sentence reads as one."""
    if shape.is_picture:
        return "image"
    if shape.is_group:
        return "group"
    if shape.table is not None:
        return "table"
    if shape.text.strip():
        return "text box"
    return "shape"
