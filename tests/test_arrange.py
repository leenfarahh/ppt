"""Aligning elements, and aligning the text inside them.

The four capabilities ported from the Wizardly add-in's Productivity Tools
service: align and distribute a set of elements, set how text sits in its box,
copy one shape's properties onto others, and find the shapes that already
match one.

Two things are tested harder than the rest because they are where a file-based
port can quietly differ from the COM original and nobody would notice for a
year. The first is DrawingML element order: a fill written before the geometry
gives a deck PowerPoint declines to open, and declines without saying why. The
second is the table traversal -- a table's insets and colour live on its
cells, and a write aimed at the graphic frame lands somewhere PowerPoint never
reads, reporting a correction that did not happen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool import arrange
from formatting_tool.arrange import (
    AlignAction,
    DistributeAction,
    MatchAction,
    ReferenceMode,
)

pptx = pytest.importorskip("pptx")

from pptx import Presentation  # noqa: E402
from pptx.dml.color import RGBColor  # noqa: E402
from pptx.enum.dml import MSO_THEME_COLOR  # noqa: E402
from pptx.enum.shapes import MSO_SHAPE  # noqa: E402
from pptx.util import Inches, Pt  # noqa: E402

EMU = arrange.EMU_PER_INCH


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def slide():
    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    return presentation.slides.add_slide(presentation.slide_layouts[6])


def box(slide, left, top, width, height, text="Copy"):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE,
        Inches(left), Inches(top), Inches(width), Inches(height),
    )
    shape.text_frame.text = text
    return shape


def at(shape) -> tuple[float, float]:
    return round(shape.left / EMU, 3), round(shape.top / EMU, 3)


def size(shape) -> tuple[float, float]:
    return round(shape.width / EMU, 3), round(shape.height / EMU, 3)


# --------------------------------------------------------------------------- #
# Aligning elements
# --------------------------------------------------------------------------- #

def test_the_anchor_never_moves(slide) -> None:
    """Shape one is the reference, and the reference is what everything else
    is brought to."""
    anchor = box(slide, 1, 1, 2, 1)
    other = box(slide, 5, 3, 1, 1)

    arrange.align([anchor, other], AlignAction.LEFT)

    assert at(anchor) == (1.0, 1.0)
    assert at(other) == (1.0, 3.0)


def test_only_position_is_written(slide) -> None:
    """Aligning a set does not resize it and does not rotate it.

    A fix that quietly resized a shape to make an edge line up would trade a
    visible defect for an invisible one.
    """
    anchor = box(slide, 1, 1, 2, 1)
    other = box(slide, 5, 3, 3.5, 0.75)
    other.rotation = 12.0

    arrange.align([anchor, other], AlignAction.BOTTOM_RIGHT)

    assert size(other) == (3.5, 0.75)
    assert other.rotation == pytest.approx(12.0)


@pytest.mark.parametrize(
    "action, expected",
    [
        (AlignAction.LEFT, (1.0, 3.0)),
        (AlignAction.RIGHT, (2.0, 3.0)),
        (AlignAction.TOP, (5.0, 1.0)),
        (AlignAction.BOTTOM, (5.0, 1.5)),
        (AlignAction.TOP_LEFT, (1.0, 1.0)),
        (AlignAction.TOP_RIGHT, (2.0, 1.0)),
        (AlignAction.BOTTOM_LEFT, (1.0, 1.5)),
        (AlignAction.BOTTOM_RIGHT, (2.0, 1.5)),
        (AlignAction.CENTER, (1.5, 3.0)),
        (AlignAction.MIDDLE, (5.0, 1.25)),
        (AlignAction.CENTER_MIDDLE, (1.5, 1.25)),
        # The dock family: an edge on one axis, a centre on the other.
        (AlignAction.POSITION_LEFT, (1.0, 1.25)),
        (AlignAction.POSITION_RIGHT, (2.0, 1.25)),
        (AlignAction.POSITION_TOP, (1.5, 1.0)),
        (AlignAction.POSITION_BOTTOM, (1.5, 1.5)),
    ],
)
def test_every_alignment(slide, action, expected) -> None:
    """The anchor is 1,1 and 2x1, so its right edge is 3.00 and its middle
    1.50. The shape being moved is 1x0.5."""
    anchor = box(slide, 1, 1, 2, 1)
    other = box(slide, 5, 3, 1, 0.5)

    arrange.align([anchor, other], action)

    assert at(other) == expected


def test_slide_mode_moves_everything_onto_the_page(slide) -> None:
    """There is no shape to hold still, so every shape given is moved."""
    first = box(slide, 1, 1, 2, 1)
    second = box(slide, 5, 3, 2, 1)

    result = arrange.align(
        [first, second],
        AlignAction.CENTER_MIDDLE,
        mode=ReferenceMode.SLIDE,
        slide_size=(Inches(13.333), Inches(7.5)),
    )

    assert result.success
    assert at(first) == at(second) == (5.667, 3.25)


def test_slide_mode_without_a_page_size_says_so(slide) -> None:
    result = arrange.align(
        [box(slide, 1, 1, 1, 1)], AlignAction.LEFT, mode=ReferenceMode.SLIDE
    )

    assert not result.success
    assert "page size" in result.message


def test_one_shape_cannot_be_aligned_to_itself(slide) -> None:
    result = arrange.align([box(slide, 1, 1, 1, 1)], AlignAction.LEFT)

    assert not result.success
    assert "two shapes" in result.message


# --------------------------------------------------------------------------- #
# Distributing
# --------------------------------------------------------------------------- #

def test_distribute_equalises_the_gaps_and_holds_the_ends(slide) -> None:
    shapes = [box(slide, 1, 1, 1, 1), box(slide, 2, 1, 2, 1), box(slide, 8, 1, 1, 1)]

    result = arrange.distribute(shapes, DistributeAction.HORIZONTAL)

    assert result.success
    lefts = [round(shape.left / EMU, 3) for shape in shapes]
    # Ends held: 1.00 and 9.00 - 1.00. Free space 8.00 - 4.00 = 4.00, over two
    # gaps, so 2.00 each.
    assert lefts == [1.0, 4.0, 8.0]


def test_distribute_works_down_the_page_too(slide) -> None:
    shapes = [box(slide, 1, 1, 1, 1), box(slide, 1, 2, 1, 1), box(slide, 1, 5, 1, 1)]

    arrange.distribute(shapes, DistributeAction.VERTICAL)

    assert [round(shape.top / EMU, 3) for shape in shapes] == [1.0, 3.0, 5.0]


def test_a_row_that_cannot_fit_is_refused(slide) -> None:
    """Spreading shapes wider than the space they have still leaves them
    overlapping, and reporting success for that would be a lie."""
    shapes = [box(slide, 1, 1, 3, 1), box(slide, 2, 1, 3, 1), box(slide, 3, 1, 3, 1)]

    result = arrange.distribute(shapes, DistributeAction.HORIZONTAL)

    assert not result.success
    assert "overlap" in result.message
    assert [round(shape.left / EMU, 2) for shape in shapes] == [1.0, 2.0, 3.0]


def test_a_named_gap_is_used_as_named(slide) -> None:
    """A finding shown to a designer names a rounded gap, and the fix has to
    lay the row out on the number the report printed."""
    shapes = [box(slide, 1, 1, 1, 1), box(slide, 2, 1, 1, 1), box(slide, 3, 1, 1, 1)]

    arrange.distribute(
        shapes, DistributeAction.HORIZONTAL, span_in=(1.0, 9.0), gap_in=0.5
    )

    assert [round(shape.left / EMU, 3) for shape in shapes] == [1.0, 2.5, 4.0]


def test_two_shapes_are_already_evenly_spaced(slide) -> None:
    result = arrange.distribute(
        [box(slide, 1, 1, 1, 1), box(slide, 5, 1, 1, 1)], DistributeAction.HORIZONTAL
    )

    assert not result.success
    assert "at least 3" in result.message


# --------------------------------------------------------------------------- #
# The primitives the fixers use
# --------------------------------------------------------------------------- #

def test_the_right_edge_subtracts_the_shape_width(slide) -> None:
    """The failure this exists to stop: snapping the LEFT edge of a
    right-aligned shape moves it by its own width off its column, and an
    Arabic deck is a deck of right-aligned shapes."""
    shape = box(slide, 1, 1, 2, 1)

    moved = arrange.align_edge(shape, "right", 10.0)

    assert round(shape.left / EMU, 2) == 8.0
    assert moved == pytest.approx(7.0)


def test_an_edge_already_in_place_reports_nothing(slide) -> None:
    assert arrange.align_edge(box(slide, 1, 1, 2, 1), "left", 1.0) is None


def test_a_centre_line_is_not_an_edge(slide) -> None:
    """A row of shapes of different heights lines up on its middle."""
    shape = box(slide, 1, 1, 2, 1)

    arrange.align_centre(shape, "y", 4.0)

    assert round(shape.top / EMU, 2) == 3.5


def test_move_to_reports_the_move_it_made(slide) -> None:
    shape = box(slide, 1, 1, 2, 1)

    assert arrange.move_to(shape, 3.0, 2.0) == pytest.approx((2.0, 1.0))
    assert arrange.move_to(shape, 3.0, 2.0) is None


# --------------------------------------------------------------------------- #
# Text inside elements
# --------------------------------------------------------------------------- #

def test_the_four_text_settings_are_written_and_read_back(slide) -> None:
    shape = box(slide, 1, 1, 3, 1, "A line")

    result = arrange.align_text(
        shape,
        anchor="middle",
        insets_in=(0.1, 0.05, 0.1, 0.05),
        alignment="right",
        rtl=True,
    )

    assert result.success
    assert arrange.anchor_of(shape) == "middle"
    assert arrange.insets_of(shape) == (0.1, 0.05, 0.1, 0.05)
    assert arrange.alignment_of(shape) == "right"
    assert arrange.reading_order_of(shape) is True


def test_a_setting_left_alone_is_left_alone(slide) -> None:
    shape = box(slide, 1, 1, 3, 1, "A line")
    arrange.align_text(shape, anchor="bottom")

    arrange.align_text(shape, alignment="center")

    assert arrange.anchor_of(shape) == "bottom"
    assert arrange.alignment_of(shape) == "center"


def test_the_horizontal_anchor_is_a_separate_setting(slide) -> None:
    """`anchorCtr` centres the whole block across the box, which is not the
    same thing as centring the lines within it."""
    shape = box(slide, 1, 1, 3, 1, "A line")

    arrange.align_text(shape, centred=True)

    assert arrange.anchor_centred(shape) is True
    assert arrange.alignment_of(shape) is None


def test_a_shape_with_no_text_frame_says_so(slide) -> None:
    """A connector has no text to align, and neither has nothing at all."""
    from pptx.enum.shapes import MSO_CONNECTOR

    connector = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(1), Inches(1), Inches(3), Inches(1)
    )

    assert "no text frame" in arrange.align_text(connector, anchor="top").message
    assert "no text frame" in arrange.align_text(None, anchor="top").message


def test_a_tables_insets_are_written_to_its_cells(slide) -> None:
    """A table's padding lives on `a:tcPr`. The graphic frame has a `a:bodyPr`
    that accepts the same values and that PowerPoint ignores, so a write aimed
    there reports a correction that did not happen."""
    frame = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(1))
    table = frame.table
    for row in range(2):
        for column in range(2):
            table.cell(row, column).text = f"c{row}{column}"

    assert arrange.set_insets(frame, (0.2, 0.1, 0.2, 0.1)) == 4
    assert arrange.insets_of(frame) == (0.2, 0.1, 0.2, 0.1)
    assert round(table.cell(1, 1).margin_left / EMU, 2) == 0.2


def test_writing_visits_every_cell_and_reading_samples_the_first(slide) -> None:
    frame = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(1))
    table = frame.table
    for row in range(2):
        for column in range(2):
            table.cell(row, column).text = f"c{row}{column}"

    assert arrange.set_alignment(frame, "right") == 4
    assert arrange.alignment_of(frame) == "right"
    assert len(arrange.paragraphs(frame)) == 4
    assert arrange.first_run(frame).text == "c00"


def test_reading_order_can_be_narrowed_to_some_paragraphs(slide) -> None:
    """A bilingual shape has an English heading over an Arabic body, and
    turning the heading round would be the same defect pointed the other
    way."""
    shape = box(slide, 1, 1, 3, 2, "Heading")
    frame = shape.text_frame
    frame.add_paragraph().text = "نص عربي"

    changed = arrange.set_reading_order(
        shape, True, only=lambda p: any(ord(c) > 0x5FF for c in (p.text or ""))
    )

    assert changed == 1
    assert arrange.reading_order_of(shape) is None  # the heading is untouched


# --------------------------------------------------------------------------- #
# Make Same
# --------------------------------------------------------------------------- #

def reference(slide, left=1.0):
    shape = box(slide, left, 1, 2, 0.8, "Reference")
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor(0xC0, 0x00, 0x00)
    shape.line.color.rgb = RGBColor(0x00, 0x33, 0x66)
    shape.line.width = Pt(3)
    run = shape.text_frame.paragraphs[0].runs[0]
    run.font.name = "Georgia"
    run.font.size = Pt(22)
    run.font.bold = True
    shape.adjustments[0] = 0.4
    return shape


def test_make_same_size(slide) -> None:
    first, second = reference(slide), box(slide, 6, 1, 3.5, 1.4)

    result = arrange.make_same([first, second], MatchAction.SIZE)

    assert result.success
    assert size(second) == (2.0, 0.8)
    assert size(first) == (2.0, 0.8)


def test_a_locked_aspect_ratio_is_counted_and_written_through(slide) -> None:
    """In a file the lock does nothing: it constrains the resize handles in
    the PowerPoint window and no more. So the shape a designer locked comes
    back distorted with no complaint from anybody, and the count is the
    complaint."""
    from pptx.oxml.ns import qn

    first, second = reference(slide), box(slide, 6, 1, 3.5, 1.4)
    locks = second._element.find(qn("p:nvSpPr")).find(qn("p:cNvSpPr"))
    locks.append(locks.makeelement(qn("a:spLocks"), {"noChangeAspect": "1"}))

    assert arrange.locks_aspect(second) is True
    result = arrange.make_same([first, second], MatchAction.WIDTH)

    assert result.aspect_locked == 1
    assert "bypasses the lock" in result.message
    assert size(second) == (2.0, 1.4)


def test_make_same_fill_carries_a_theme_colour_and_its_transform(slide) -> None:
    """The case the COM original cannot do without the format painter: a
    theme colour with a `lumMod` on it reads as "na" through the object
    model, and copying the element carries it exactly."""
    first = box(slide, 1, 1, 2, 1)
    first.fill.solid()
    first.fill.fore_color.theme_color = MSO_THEME_COLOR.ACCENT_2
    first.fill.fore_color.brightness = -0.25
    second = box(slide, 5, 1, 2, 1)

    arrange.make_same([first, second], MatchAction.FILL_COLOR)

    assert second.fill.fore_color.theme_color == MSO_THEME_COLOR.ACCENT_2
    assert second.fill.fore_color.brightness == pytest.approx(-0.25)


def test_make_same_line_colour_leaves_the_weight_alone(slide) -> None:
    """The original runs the format painter for this and then puts the weight,
    the dash and the arrowheads back, because the painter copies everything.
    The colour is one child of `a:ln`, so here it is the colour and nothing
    else."""
    first, second = reference(slide), box(slide, 6, 1, 2, 0.8)
    second.line.width = Pt(6)

    arrange.make_same([first, second], MatchAction.LINE_COLOR)

    assert str(second.line.color.rgb) == "003366"
    assert second.line.width.pt == 6


def test_make_same_font_style_writes_every_run(slide) -> None:
    first = reference(slide)
    second = box(slide, 6, 1, 2, 0.8, "One")
    second.text_frame.add_paragraph().text = "Two"

    result = arrange.make_same([first, second], MatchAction.FONT_STYLE)

    assert result.success
    for paragraph in second.text_frame.paragraphs:
        for run in paragraph.runs:
            assert run.font.name == "Georgia"
            assert run.font.size.pt == 22
            assert run.font.bold is True


def test_make_same_corner_radius(slide) -> None:
    first, second = reference(slide), box(slide, 6, 1, 2, 0.8)

    arrange.make_same([first, second], MatchAction.CORNER_RADIUS)

    assert second.adjustments[0] == pytest.approx(0.4)


def test_a_reference_with_no_adjustable_corners_says_so(slide) -> None:
    plain = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(1), Inches(1), Inches(2), Inches(1)
    )
    result = arrange.make_same(
        [plain, box(slide, 6, 1, 2, 1)], MatchAction.CORNER_RADIUS
    )

    assert not result.success
    assert "rounded corners" in result.message


def test_make_same_format_carries_the_text_frame_with_it(slide) -> None:
    first = reference(slide)
    arrange.align_text(first, anchor="bottom", insets_in=(0.3, 0.2, 0.3, 0.2))
    second = box(slide, 6, 1, 2, 0.8)

    arrange.make_same([first, second], MatchAction.FORMAT)

    assert str(second.fill.fore_color.rgb) == "C00000"
    assert second.line.width.pt == 3
    assert arrange.anchor_of(second) == "bottom"
    assert arrange.insets_of(second) == (0.3, 0.2, 0.3, 0.2)


def test_format_does_not_change_what_a_shape_is(slide) -> None:
    """Painting a rounded rectangle onto an ellipse leaves an ellipse, and
    leaves it where it was and the size it was."""
    first = reference(slide)
    second = slide.shapes.add_shape(
        MSO_SHAPE.OVAL, Inches(6), Inches(1), Inches(3), Inches(1.4)
    )
    second.text_frame.text = "Oval"

    arrange.make_same([first, second], MatchAction.FORMAT)

    assert second.shape_type == first.shape_type  # both AUTO_SHAPE
    assert second.auto_shape_type == MSO_SHAPE.OVAL
    assert size(second) == (3.0, 1.4)
    assert at(second) == (6.0, 1.0)


def test_the_slide_is_a_reference_for_size_and_nothing_else(slide) -> None:
    shape = box(slide, 1, 1, 2, 1)
    page = (Inches(13.333), Inches(7.5))

    sized = arrange.make_same(
        [shape], MatchAction.WIDTH, mode=ReferenceMode.SLIDE, slide_size=page
    )
    refused = arrange.make_same(
        [shape, box(slide, 6, 1, 1, 1)], MatchAction.FILL_COLOR,
        mode=ReferenceMode.SLIDE, slide_size=page,
    )

    assert sized.success and size(shape)[0] == pytest.approx(13.333, abs=0.001)
    assert not refused.success
    assert "width, height or size" in refused.message


def test_one_shape_that_refuses_does_not_cost_the_batch(slide) -> None:
    """One grouped shape refusing a fill should not cost the other eleven
    cards their fill."""
    first = reference(slide)
    good = box(slide, 6, 1, 2, 0.8)
    awkward = slide.shapes.add_table(1, 1, Inches(9), Inches(1), Inches(2), Inches(1))

    result = arrange.make_same(
        [first, good, awkward], MatchAction.CORNER_RADIUS
    )

    assert result.success
    assert good.adjustments[0] == pytest.approx(0.4)
    assert result.skipped == 1
    assert "1 shape could not be updated" in result.message


# --------------------------------------------------------------------------- #
# Select Same
# --------------------------------------------------------------------------- #

def test_select_same_finds_the_shapes_that_already_match(slide) -> None:
    first = reference(slide)
    twin = box(slide, 6, 1, 2, 0.8, "Twin")
    arrange.make_same([first, twin], MatchAction.FILL_COLOR)
    odd = box(slide, 9, 1, 2, 0.8, "Odd")

    found = arrange.select_same([twin, odd], first, MatchAction.FILL_COLOR)

    assert [shape.text_frame.text for shape in found] == ["Reference", "Twin"]


def test_select_same_by_whole_format(slide) -> None:
    first = reference(slide)
    twin = box(slide, 6, 1, 2, 0.8, "Twin")
    arrange.make_same([first, twin], MatchAction.FORMAT)
    odd = box(slide, 9, 1, 2, 0.8, "Odd")

    found = arrange.select_same([twin, odd], first, MatchAction.FORMAT)

    assert [shape.text_frame.text for shape in found] == ["Reference", "Twin"]
    assert arrange.signature(first) == arrange.signature(twin)
    assert arrange.signature(first) != arrange.signature(odd)


def test_the_reference_comes_back_even_when_nothing_matched(slide) -> None:
    """A caller always gets a set it can act on rather than an empty list it
    has to special-case."""
    first = reference(slide)
    odd = box(slide, 9, 1, 2, 0.8, "Odd")

    found = arrange.select_same([odd], first, MatchAction.FILL_COLOR)

    assert found == [first]


def test_the_reference_is_not_returned_twice(slide) -> None:
    first = reference(slide)

    assert arrange.select_same([first, first], first, MatchAction.WIDTH) == [first]


def test_size_matching_is_tolerant_of_a_hundredth_of_an_inch(slide) -> None:
    first = box(slide, 1, 1, 2, 1)
    near = box(slide, 5, 1, 2, 1)
    near.width = first.width + 4000        # ~0.004in
    far = box(slide, 9, 1, 2, 1)
    far.width = first.width + 40000        # ~0.044in

    found = arrange.select_same([near, far], first, MatchAction.WIDTH)

    assert found == [first, near]


def test_a_hidden_line_matches_any_hidden_line(slide) -> None:
    """The C#'s invisible-line shortcut: a hidden line has no colour to
    disagree about."""
    first, second = box(slide, 1, 1, 2, 1), box(slide, 5, 1, 2, 1)
    for shape in (first, second):
        shape.line.fill.background()
    second.line.width = Pt(9)
    drawn = box(slide, 9, 1, 2, 1)
    drawn.line.color.rgb = RGBColor(0x00, 0x00, 0x00)

    style = arrange.capture(first, MatchAction.LINE_COLOR)

    assert arrange.matches(second, MatchAction.LINE_COLOR, style) is True
    assert arrange.matches(drawn, MatchAction.LINE_COLOR, style) is False


def test_a_shape_that_cannot_be_compared_is_not_a_match(slide) -> None:
    assert arrange.matches(None, MatchAction.WIDTH, arrange.Style()) is False


# --------------------------------------------------------------------------- #
# The file has to open afterwards
# --------------------------------------------------------------------------- #

def test_the_written_xml_keeps_the_order_the_schema_fixes(tmp_path: Path) -> None:
    """A `a:solidFill` written before `a:prstGeom`, or an `a:effectLst`
    written before the `a:ln`, gives a file PowerPoint refuses to open -- and
    it refuses without saying which element it objected to."""
    presentation = Presentation()
    page = presentation.slides.add_slide(presentation.slide_layouts[6])
    first = reference(page)
    second = box(page, 6, 1, 2, 0.8, "Second")

    arrange.make_same([first, second], MatchAction.FORMAT)
    out = tmp_path / "written.pptx"
    presentation.save(str(out))

    reopened = Presentation(str(out))
    for shape in reopened.slides[0].shapes:
        tags = [child.tag.rsplit("}", 1)[-1] for child in shape._element.spPr]
        assert tags == sorted(tags, key=_SP_PR_ORDER.index)


_SP_PR_ORDER = [
    "xfrm", "custGeom", "prstGeom", "noFill", "solidFill", "gradFill",
    "blipFill", "pattFill", "grpFill", "ln", "effectLst", "effectDag",
    "scene3d", "sp3d", "extLst",
]


# --------------------------------------------------------------------------- #
# Parsing what a caller wrote
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "written, expected",
    [
        ("left", AlignAction.LEFT),
        ("TopLeft", AlignAction.TOP_LEFT),
        ("top_left", AlignAction.TOP_LEFT),
        ("POSITION_BOTTOM", AlignAction.POSITION_BOTTOM),
        ("positionBottom", AlignAction.POSITION_BOTTOM),
        ("sideways", None),
        (None, None),
    ],
)
def test_actions_are_read_however_they_are_written(written, expected) -> None:
    assert arrange.parse_action(AlignAction, written) is expected


def test_only_the_word_slide_selects_the_slide() -> None:
    """An unreadable mode that silently became "align everything to the page"
    would move every shape in the set."""
    assert arrange.parse_reference_mode("Slide") is ReferenceMode.SLIDE
    assert arrange.parse_reference_mode("firstShape") is ReferenceMode.FIRST_SHAPE
    assert arrange.parse_reference_mode("nonsense") is ReferenceMode.FIRST_SHAPE
    assert arrange.parse_reference_mode(None) is ReferenceMode.FIRST_SHAPE
