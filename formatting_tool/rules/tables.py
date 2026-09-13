"""Header rows, in real tables and in the components drawn to look like them.

A consulting deck states its comparisons in tables, and the header row is the
part a reader uses to navigate one. Two things go wrong in it, both visible at
a glance and neither reported by anything else here:

- Some headings are set on one line and others on two, so the row reads as
  ragged and the cells no longer look like one band.
- Some headings are left aligned and others centred, so no two column labels
  start at the same place and the eye has nothing to run down.

Off a real deck, one seven-column table had both at once: two headings on one
line and left aligned, five on two lines and centred.

TWO KINDS OF TABLE, ONE RULE EACH. A real PowerPoint table is a graphic frame
whose cells live in `ShapeProfile.table`; a table-like component is a set of
drawn boxes that `rules.space.matrix_components` reads as rows. The header of
each is found here in one place, because a rule that answered the question
twice would answer it two ways.

WHAT MAKES A ROW A HEADER. Its fill. A header row is filled differently from
the body under it, which is what makes it read as a header in the first place,
and it is the one signal that means the same thing in both kinds of table. The
`firstRow` banding flag is not usable on its own: the table this was written
against has a plainly styled header row and carries the flag off. So the flag
corroborates a header and never defines one, and a table whose top row is not
set apart is left alone rather than guessed at.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..models import (
    Category,
    Issue,
    Severity,
    ShapeProfile,
    SlideProfile,
    TableCell,
)
from .base import Rule, RuleContext
from .space import Cell, matrix_components

# Alignments that put a heading somewhere other than the edge its column
# starts at. `None` is not among them on purpose: a cell that states no
# alignment inherits one, and in a left-to-right deck what it inherits renders
# flush left, which is the answer this rule wants. Reporting "unstated" as
# "wrong" would fault most of the body of most tables for being ordinary.
_OFF_EDGE = {"CENTER", "RIGHT", "JUSTIFY", "DISTRIBUTE"}


class TableHeaderRowsRule(Rule):
    """Headings in one table set on different numbers of lines."""

    id = "table.header_rows"
    category = Category.SPACE
    description = "Headings in a table header are not all on a single line."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, anchor, headings in _headers(ctx):
            stacked = [h for h in headings if h.lines > 1]
            if not stacked:
                continue
            names = ", ".join(f"{h.label!r}" for h in stacked[:3])
            more = f" and {len(stacked) - 3} more" if len(stacked) > 3 else ""
            deepest = max(h.lines for h in stacked)
            yield self.issue(
                f"{len(stacked)} of {len(headings)} headings in this table are "
                f"set on more than one line ({names}{more}), so the header row "
                "is ragged where it should read as one band.",
                slide=slide,
                shape=anchor,
                expected="one line per heading",
                found=f"up to {deepest} lines",
                suggestion=(
                    "Put each heading on a single line. Where one will not fit, "
                    "shorten the wording or widen the column: breaking it with "
                    "a return makes that column taller than its neighbours."
                ),
            )


class TableHeaderAlignmentRule(Rule):
    """Headings in one table not all set to the edge their columns start at."""

    id = "table.header_alignment"
    category = Category.SPACE
    description = "Headings in a table header are not aligned to the leading edge."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        side = "right" if ctx.deck.rtl else "left"
        for slide, anchor, headings in _headers(ctx):
            off = [h for h in headings if _off_edge(h, ctx.deck.rtl)]
            if not off:
                continue
            names = ", ".join(f"{h.label!r}" for h in off[:3])
            more = f" and {len(off) - 3} more" if len(off) > 3 else ""
            found = sorted({h.alignment or "unstated" for h in off})
            yield self.issue(
                f"{len(off)} of {len(headings)} headings in this table are not "
                f"{side} aligned in their cells ({names}{more}), so no two "
                "column labels start at the same place.",
                slide=slide,
                shape=anchor,
                expected=f"{side} aligned, as the rest of the header",
                found=", ".join(found).lower(),
                suggestion=(
                    f"Select the header row and set it {side} aligned. The "
                    "column a heading names starts at that edge, and a reader "
                    "runs down the labels to find it."
                ),
            )


# --------------------------------------------------------------------------- #
# Finding the header
# --------------------------------------------------------------------------- #

class _Heading:
    """One header cell, from either kind of table."""

    __slots__ = ("label", "lines", "alignment")

    def __init__(self, label: str, lines: int, alignment: Optional[str]) -> None:
        self.label = label
        self.lines = lines
        self.alignment = alignment


def _off_edge(heading: _Heading, rtl: bool) -> bool:
    """True when a heading is set somewhere other than its column's edge."""
    stated = (heading.alignment or "").split(" ")[0].upper()
    if not stated:
        return False
    if rtl:
        return stated != "RIGHT"
    return stated in _OFF_EDGE


def _headers(
    ctx: RuleContext,
) -> Iterable[tuple[SlideProfile, ShapeProfile, list[_Heading]]]:
    """Every header row in the deck, with the shape a finding hangs on."""
    for slide in ctx.deck.slides:
        if slide.hidden:
            continue
        for shape in slide.shapes:
            if shape.table is not None:
                headings = _table_header(shape)
                if headings:
                    yield slide, shape, headings
        for anchor, headings in _drawn_headers(ctx, slide):
            yield slide, anchor, headings


def _table_header(shape: ShapeProfile) -> list[_Heading]:
    """The header row of a real table, or [] when its top row is not one."""
    table = shape.table
    if table is None or table.rows < 2:
        return []
    top, below = table.row_at(0), table.row_at(1)
    if not _set_apart(
        [(c.fill_hex, c.fill_theme) for c in top],
        [(c.fill_hex, c.fill_theme) for c in below],
        table.first_row_header,
    ):
        return []
    return [_of_cell(cell) for cell in top if cell.text.strip()]


def _of_cell(cell: TableCell) -> _Heading:
    stated = next(
        (p.alignment for p in cell.paragraphs if p.text.strip() and p.alignment),
        None,
    )
    return _Heading(cell.text.strip().splitlines()[0], cell.lines, stated)


def _drawn_headers(
    ctx: RuleContext, slide: SlideProfile
) -> Iterable[tuple[ShapeProfile, list[_Heading]]]:
    """Header rows of components drawn as boxes rather than made as tables."""
    by_id = {s.shape_id: s for s in slide.shapes}
    cells = [
        Cell(s.shape_id, s.geometry.left_in, s.geometry.top_in,
             s.geometry.width_in, s.geometry.height_in)
        for s in slide.shapes if not s.is_group
    ]
    for matrix in matrix_components(cells, ctx.spec.tolerances.position_in):
        if len(matrix.rows) < 2:
            continue
        top = [by_id.get(c.key) for c in matrix.rows[0]]
        below = [by_id.get(c.key) for c in matrix.rows[1]]
        if len(top) < 2 or any(s is None for s in top + below):
            continue        # a heading band is one cell: no headings to check
        if not _set_apart(
            [(s.fill_hex, s.fill_theme) for s in top],
            [(s.fill_hex, s.fill_theme) for s in below],
            False,
        ):
            continue
        headings = [_of_shape(s) for s in top if s.text.strip()]
        if headings:
            yield top[0], headings


def _of_shape(shape: ShapeProfile) -> _Heading:
    stated = next(
        (p.alignment for p in shape.paragraphs if p.text.strip() and p.alignment),
        None,
    )
    lines = sum(1 for p in shape.paragraphs if p.text.strip())
    return _Heading(shape.text.strip().splitlines()[0], lines, stated)


def _set_apart(top: list, below: list, flagged: bool) -> bool:
    """True when the top row is filled as one band and the body is not.

    The TOP row has to be internally consistent: a row whose cells carry three
    different fills is not a header band, it is a row of coloured boxes, and
    holding those to a heading's rules would fault an ordinary diagram for not
    being a table.

    The BODY does not, and requiring it was wrong. A status table colours its
    cells by value -- red, blank, blank, theme -- which is the whole point of
    it, and demanding one fill across the body threw out exactly the components
    most likely to carry this defect. What matters is that the header's fill is
    its own: not worn by any cell of the row beneath it.
    """
    if not top or not below:
        return False
    if len(set(top)) != 1:
        return False
    if top[0] == (None, None):
        # No fill of its own, so nothing sets it apart. The banding flag is
        # all that is left, and on its own it is weak.
        return flagged
    if top[0] in set(below):
        return flagged
    return True
