"""The alignment grid, taken from the master instead of guessed from the deck.

`AlignmentGridRule` counts left edges across the deck under audit and treats
any edge enough shapes share as intended. So on a master-only run its answers
were derived entirely from the file it was judging: on one real deck it found
eight grid lines, none of which the master declares, and blessed 0.48in and
0.49in as two separate intentions. A deck consistently sitting off the
master's column had that position certified as its own grid.

A master that marks its presentation space states its columns outright, and a
two-column layout states three left edges where a margin can only report the
outermost. That is what this reads.
"""

from __future__ import annotations

from formatting_tool.extract.master_spec import derive_master_spec
from formatting_tool.models import (
    BrandGuidelines,
    DeckProfile,
    Geometry,
    LayoutProfile,
    ParagraphProfile,
    RunProfile,
    ShapeProfile,
    SlideProfile,
)
from formatting_tool.rules import RuleContext
from formatting_tool.rules.space import AlignmentGridRule

CANVAS_W, CANVAS_H = 13.333, 7.5


def _shape(name, left, top=2.0, *, alt="", token=None, text="", width=3.0):
    return ShapeProfile(
        shape_id=abs(hash((name, left, top))) % 99999,
        name=name,
        shape_type="TEXT_BOX (17)" if text else "AUTO_SHAPE (1)",
        geometry=Geometry(left_in=left, top_in=top, width_in=width, height_in=0.4),
        placeholder_type=token,
        alt_text=alt,
        text=text,
        paragraphs=[ParagraphProfile(text=text, runs=[RunProfile(text=text)])]
        if text else [],
    )


def _two_column_layout(*, ps=True):
    """The real shape of one: an outer edge and one per column."""
    shapes = [
        _shape("Title 1", 0.917, 0.399, token="TITLE (1)"),
        _shape("Body L", 0.917, 1.997, token="OBJECT (7)"),
        _shape("Body R", 6.750, 1.997, token="OBJECT (7)"),
        _shape("Page 1", 9.417, 6.951, token="SLIDE_NUMBER (13)"),
    ]
    if ps:
        shapes += [
            _shape("PS L", 0.586, 1.997, alt="PS"),
            _shape("PS R", 6.750, 1.997, alt="PS"),
        ]
    return LayoutProfile(name="title_two_columns", index=0, shapes=shapes)


def _ctx(slide_shapes, layouts, slides=None):
    master = DeckProfile(path="master.pptx", width_in=CANVAS_W,
                         height_in=CANVAS_H, layouts=layouts)
    deck = DeckProfile(
        path="messy.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
        slides=slides or [SlideProfile(number=1, layout_name="title_two_columns",
                                       shapes=slide_shapes)],
    )
    return RuleContext(deck=deck, spec=derive_master_spec(master, BrandGuidelines()))


# --------------------------------------------------------------------------- #
# Where the grid comes from
# --------------------------------------------------------------------------- #

def test_a_marked_master_declares_every_column_not_just_the_outermost() -> None:
    """The point of the whole change: three left edges, not one.

    A margin frame reports 0.59 and stops. The two interior columns are just
    as much the master's statement and were checked by nothing.
    """
    spec = derive_master_spec(
        DeckProfile(path="m.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
                    layouts=[_two_column_layout()]),
        BrandGuidelines(),
    )

    assert spec.grid_edges_in == [0.59, 0.92, 6.75]
    assert spec.safe_margins.left_in == 0.59      # one number, the outermost


def test_chrome_is_not_a_column() -> None:
    spec = derive_master_spec(
        DeckProfile(path="m.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
                    layouts=[_two_column_layout()]),
        BrandGuidelines(),
    )

    assert 9.42 not in spec.grid_edges_in      # the page number sits there


def test_near_duplicate_edges_collapse() -> None:
    """Layouts drawn by hand disagree slightly about the same column.

    Left alone those are separate grid lines a few hundredths apart, which is
    the inferred grid's 0.48/0.49 problem arriving by another route. Merged
    within the position tolerance rather than by rounding, because rounding
    only catches the disagreements that happen to straddle a decimal: 0.90 and
    0.93 round apart and are plainly one column.
    """
    layouts = [
        _two_column_layout(),
        LayoutProfile(name="other", index=1, shapes=[
            _shape("PS", 0.90, 1.997, alt="PS"),
            _shape("Body", 0.93, 1.997, token="OBJECT (7)"),
        ]),
    ]
    spec = derive_master_spec(
        DeckProfile(path="m.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
                    layouts=layouts),
        BrandGuidelines(),
    )

    assert spec.grid_edges_in == [0.59, 0.92, 6.75]


def test_an_unmarked_master_declares_nothing_and_inference_still_runs() -> None:
    """Every master has placeholders and could supply edges this way. Reading
    them unasked would change the alignment check on every deck at once, so
    the PS mark is the signal that somebody has curated this master."""
    layouts = [_two_column_layout(ps=False)]
    spec = derive_master_spec(
        DeckProfile(path="m.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
                    layouts=layouts),
        BrandGuidelines(),
    )
    assert spec.grid_edges_in == []

    # Four shapes agreeing on 3.00 make a grid line the old way, and the
    # fifth misses it.
    shapes = [_shape(f"T{n}", 3.00, 1.0 + n, text="copy") for n in range(4)]
    shapes.append(_shape("Odd", 3.07, 5.0, text="copy"))
    issues = list(AlignmentGridRule().check(_ctx(shapes, layouts)))

    assert [i.shape for i in issues] == ["Odd"]
    assert "the rest of the deck follows" in issues[0].message


def test_the_declared_grid_wins_over_the_deck_habit() -> None:
    """Shapes agreeing on a wrong column used to define the grid, which made
    every one of them correct by definition.

    0.78in is 0.14in off the master's 0.92in column, inside the near-miss
    window. Inferring the grid from five shapes that all sit there makes 0.78
    the grid line and the drift zero, so the rule had nothing to say about the
    one thing most worth saying.
    """
    shapes = [_shape(f"T{n}", 0.78, 1.0 + n * 0.5, text="copy") for n in range(5)]
    issues = list(AlignmentGridRule().check(_ctx(shapes, [_two_column_layout()])))

    assert len(issues) == 1
    assert "the master declares" in issues[0].message
    assert issues[0].expected == "0.92in"


def test_a_shape_measures_against_the_nearest_declared_column() -> None:
    """Not against the busiest one. A right-column shape is judged by the
    right column."""
    shapes = [_shape("Right", 6.81, text="copy")]
    issues = list(AlignmentGridRule().check(_ctx(shapes, [_two_column_layout()])))

    assert len(issues) == 1
    assert issues[0].expected == "6.75in"


# --------------------------------------------------------------------------- #
# One fact reported once
# --------------------------------------------------------------------------- #

def test_a_miss_the_whole_deck_makes_is_one_finding() -> None:
    """Measuring a real deck against a marked-up master turned a single 0.11in
    difference into thirty-two identical findings. It is one fact about the
    deck: it has a column of its own."""
    shapes = [_shape(f"T{n}", 0.48, 1.0 + n * 0.5, text="copy") for n in range(8)]
    issues = list(AlignmentGridRule().check(_ctx(shapes, [_two_column_layout()])))

    assert len(issues) == 1
    assert issues[0].slide is None          # deck-level, not per shape
    assert issues[0].shape is None
    assert "8 shapes" in issues[0].message
    assert "column of its own" in issues[0].message


def test_a_one_off_miss_is_still_named_on_its_shape() -> None:
    """A designer fixes this one by selecting the shape, so it has to say
    which shape."""
    shapes = [_shape("Stray", 0.48, text="copy")]
    issues = list(AlignmentGridRule().check(_ctx(shapes, [_two_column_layout()])))

    assert len(issues) == 1
    assert issues[0].shape == "Stray"
    assert issues[0].slide == 1


def test_positions_within_tolerance_are_one_position() -> None:
    """0.48 and 0.49 are the same column badly drawn, not two columns."""
    shapes = [_shape(f"A{n}", 0.48, 1.0 + n * 0.5, text="copy") for n in range(3)]
    shapes += [_shape(f"B{n}", 0.49, 4.0 + n * 0.5, text="copy") for n in range(3)]
    issues = list(AlignmentGridRule().check(_ctx(shapes, [_two_column_layout()])))

    assert len(issues) == 1
    assert "6 shapes" in issues[0].message


def test_the_deck_level_finding_lists_its_slides_as_ranges() -> None:
    slides = [
        SlideProfile(number=n, layout_name="title_two_columns",
                     shapes=[_shape(f"T{n}a", 0.48, text="copy"),
                             _shape(f"T{n}b", 0.48, 3.0, text="copy")])
        for n in (1, 2, 3, 7)
    ]
    ctx = _ctx([], [_two_column_layout()], slides=slides)

    issues = list(AlignmentGridRule().check(ctx))

    assert len(issues) == 1
    assert "1-3, 7" in issues[0].found
