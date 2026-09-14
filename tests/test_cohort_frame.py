"""A set move that leaves the page.

`space.text_collision` is allowed to move a whole set of shapes, and rightly:
nudging one status bar out of a row of four breaks the row. What nothing
measured was whether the set was still on the page afterwards.

The slide that needed this: a heading's hardcoded typeface was cleared so the
theme's would apply, the theme face is wider, the heading stopped fitting its
box, and the recheck saw the spilled text drawn across the rule beneath it. The
set found was a column -- the rule, the copy above it, the first of three cards
and the band below them -- and all four moved 0.55in left, out of the 0.61in
margin every other shape on the slide sat against. The card slid out from under
its own heading.

Nothing already there could see it. The move broke no alignment, because the
whole column moved together, which is the point of a cohort. It landed on
nothing, because the space it moved into was empty.
"""

from __future__ import annotations

from formatting_tool.apply.applier import FixContext, _cohort_outside_the_frame

EMU = 914400


class Shape:
    def __init__(self, left, top, width, height):
        self.left = int(left * EMU)
        self.top = int(top * EMU)
        self.width = int(width * EMU)
        self.height = int(height * EMU)


class Move:
    """Just enough of a CohortMove: where the shapes are, and where they were."""

    def __init__(self, shapes, moved_by):
        self.shapes = shapes
        dx, dy = moved_by
        self.origin = {
            id(s): (s.left - int(dx * EMU), s.top - int(dy * EMU)) for s in shapes
        }


def _context(margins=None):
    return FixContext(
        width_emu=int(13.333 * EMU),
        height_emu=int(7.5 * EMU),
        margins=margins if margins is not None else {
            "left": 0.61, "top": 0.3, "right": 0.61, "bottom": 0.3,
        },
    )


def test_a_column_dragged_out_of_the_margin_is_counted() -> None:
    """The real one: four shapes at 0.61-0.66in, moved 0.55in left."""
    column = [Shape(0.06, y, w, 0.3) for y, w in
              ((1.98, 6.6), (1.89, 0.54), (2.41, 2.76), (5.39, 8.62))]

    assert _cohort_outside_the_frame(Move(column, (-0.55, 0)), _context()) == 4


def test_a_set_that_was_already_outside_is_not_counted() -> None:
    """Counted as a change, not as a state, for the same reason
    `_cohort_worsened` asks whether an overlap got WORSE. A row of status bars
    already below the master's bottom margin is nudged clear of the copy over
    it and stays exactly as far outside as it was -- and refusing there would
    block the fix the whole mechanism was built for."""
    # Already below the 7.2in limit before the move, and still below it after.
    bars = [Shape(0.8 + n * 3.1, 7.46, 2.2, 0.06) for n in range(4)]

    assert _cohort_outside_the_frame(Move(bars, (0, 0.2)), _context()) == 0


def test_a_move_that_stays_inside_is_allowed() -> None:
    row = [Shape(0.8 + n * 3.1, 5.26, 2.2, 0.06) for n in range(4)]

    assert _cohort_outside_the_frame(Move(row, (0, 0.2)), _context()) == 0


def test_without_a_master_the_canvas_is_the_frame() -> None:
    """A deck checked without a master keeps the weaker check rather than
    none."""
    off = [Shape(-0.4, 2.0, 2.0, 0.3)]

    assert _cohort_outside_the_frame(Move(off, (-1.0, 0)), _context(margins={})) == 1
    # And inside the canvas is fine when no margins are declared.
    on = [Shape(0.1, 2.0, 2.0, 0.3)]
    assert _cohort_outside_the_frame(Move(on, (-0.5, 0)), _context(margins={})) == 0
