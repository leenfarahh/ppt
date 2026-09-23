"""Nothing left in the deck that is not on the palette, and still readable.

The two passes that run after every fix and have no findings behind them.

WHY THEY EXIST is the gap between "no finding names an off-palette colour" and
"there is no off-palette colour". A rule reports what it can read, and a great
deal of a deck's colour is in places no rule reads: a gradient's stops, a drop
shadow, a chart's series, a cell's borders, a bullet. Each could be its own
rule and the list would never be finished, so the sweep asks the only question
with a complete answer -- what does the file say, everywhere.

The contrast pass then runs LAST, because the sweep has just changed what a
good deal of the copy sits on and nothing that ran before it can see the
result.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pptx")

from pptx import Presentation                                 # noqa: E402
from pptx.dml.color import RGBColor                           # noqa: E402
from pptx.util import Inches, Pt                              # noqa: E402

from formatting_tool.apply.contrast import enforce_contrast    # noqa: E402
from formatting_tool.apply.palette import sweep_to_palette     # noqa: E402
from formatting_tool.colorutil import contrast_ratio, delta_e  # noqa: E402

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

# A palette with range in it: two neutrals, a blue, a warm grey and a pale
# grey. Enough for a scheme to be expressible and small enough to collide.
PALETTE = {
    "dk1": "1A1A1A",
    "lt1": "FFFFFF",
    "accent1": "0B5FA5",
    "accent2": "6E7B8B",
    "accent3": "B9C0C8",
    "accent4": "D8402F",
}


def _deck(tmp_path, name="deck.pptx"):
    presentation = Presentation()
    presentation.slide_width, presentation.slide_height = (
        Inches(13.333), Inches(7.5)
    )
    return presentation, tmp_path / name


def _blank(presentation):
    return presentation.slides.add_slide(presentation.slide_layouts[6])


def _card(slide, fill, top=1.0, text=None, ink=None, size=12):
    shape = slide.shapes.add_shape(1, Inches(1), Inches(top), Inches(4), Inches(1.4))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(fill)
    if text is not None:
        run = shape.text_frame.paragraphs[0].add_run()
        run.text = text
        run.font.size = Pt(size)
        if ink:
            run.font.color.rgb = RGBColor.from_string(ink)
    return shape


def _colors(path):
    """Every literal colour left on the slides of a written deck."""
    found = set()
    for slide in Presentation(str(path)).slides:
        for node in slide._element.iter(f"{_A}srgbClr"):
            value = str(node.get("val") or "").upper()
            if len(value) == 6:
                found.add(value)
    return found


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #

def test_a_fill_no_rule_reported_is_still_put_on_the_palette(tmp_path):
    """The whole point. There is no finding behind this shape -- the sweep is
    handed no plan and no issues -- and the colour still cannot survive."""
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "BFD9EE")
    presentation.save(str(path))

    result = sweep_to_palette(path, PALETTE, 3.0)

    assert result.applied
    assert "BFD9EE" not in _colors(path)
    assert _colors(path) <= {value.upper() for value in PALETTE.values()}


def test_a_gradients_stops_are_swept_though_nothing_can_read_them(tmp_path):
    """`fill_hex` is None for a gradient, so no rule has ever named either of
    these colours and no fixer has ever touched one."""
    presentation, path = _deck(tmp_path)
    band = _blank(presentation).shapes.add_shape(
        1, Inches(1), Inches(4), Inches(6), Inches(1)
    )
    band.fill.gradient()
    band.fill.gradient_stops[0].color.rgb = RGBColor.from_string("7F2A9B")
    band.fill.gradient_stops[1].color.rgb = RGBColor.from_string("F0A030")
    presentation.save(str(path))

    sweep_to_palette(path, PALETTE, 3.0)

    left = _colors(path)
    assert "7F2A9B" not in left and "F0A030" not in left


def test_a_gradients_stops_do_not_collapse_into_one_colour(tmp_path):
    """The regression the grouping exists for. Two stops both nearest to one
    entry, snapped independently, turn a band into a flat rectangle -- visible
    at a glance, and the sweep's own doing."""
    presentation, path = _deck(tmp_path)
    band = _blank(presentation).shapes.add_shape(
        1, Inches(1), Inches(4), Inches(6), Inches(1)
    )
    band.fill.gradient()
    # Two blues four delta-E apart, both nearest accent1 by a mile.
    band.fill.gradient_stops[0].color.rgb = RGBColor.from_string("1E6FB4")
    band.fill.gradient_stops[1].color.rgb = RGBColor.from_string("2575BA")
    presentation.save(str(path))

    sweep_to_palette(path, PALETTE, 3.0)

    written = Presentation(str(path))
    stops = next(list(written.slides)[0]._element.iter(f"{_A}gsLst"))
    values = [str(n.get("val")).upper() for n in stops.iter(f"{_A}srgbClr")]
    assert len(values) == 2
    assert values[0] != values[1], "the band came out flat"


def test_a_colour_already_on_the_palette_is_left_exactly_alone(tmp_path):
    """A value a hundredth off its entry is that entry to a reader, and
    rewriting the file for it is a change with nothing to show for it."""
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "0B5FA5")
    presentation.save(str(path))

    result = sweep_to_palette(path, PALETTE, 3.0)

    assert not result.applied
    assert "0B5FA5" in _colors(path)


def test_a_shadows_black_is_not_repainted_in_a_brand_colour(tmp_path):
    """Black and white are the two values a deck states for reasons that have
    nothing to do with a brand. Snapping them to the nearest entry is how a
    drop shadow comes out maroon."""
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "000000")
    presentation.save(str(path))

    # A palette with nothing near black in it. The near-black `dk1` is what
    # makes the ordinary case work, so it is deliberately absent here.
    sweep_to_palette(path, {"accent1": "0B5FA5", "accent4": "D8402F"}, 3.0)

    assert "000000" in _colors(path)


def test_a_near_black_in_the_palette_does_claim_the_black(tmp_path):
    """The other half of the same rule: where the brand HAS a near-black, that
    is the colour the deck meant and the swap is an improvement."""
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "000000")
    presentation.save(str(path))

    sweep_to_palette(path, PALETTE, 3.0)

    assert "000000" not in _colors(path)
    assert "1A1A1A" in _colors(path)


def test_the_plan_decides_where_a_colour_goes_when_it_has_an_opinion(tmp_path):
    """The plan is the only thing that saw every off-palette colour at once.
    A sweep taking the nearest entry for each of them on its own would undo
    exactly the distinctions it was written to keep."""
    from formatting_tool.apply.colorplan import build_color_plan

    presentation, path = _deck(tmp_path)
    slide = _blank(presentation)
    _card(slide, "8A8A8A", top=1.0)
    _card(slide, "9A9A9A", top=3.0)
    presentation.save(str(path))

    greys = ["8A8A8A", "9A9A9A"]
    plan = build_color_plan(
        selected=greys, everything=greys, palette=PALETTE,
        tolerance=3.0, limit=12.0,
    )
    sweep_to_palette(path, PALETTE, 3.0, plan)

    landed = _colors(path)
    for value in greys:
        assert plan.choice_for(value).target in landed


def test_an_empty_palette_changes_nothing(tmp_path):
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "BFD9EE")
    presentation.save(str(path))

    result = sweep_to_palette(path, {}, 3.0)

    assert not result.applied and result.reason
    assert "BFD9EE" in _colors(path)


def test_a_deck_it_cannot_open_is_not_fatal(tmp_path):
    missing = tmp_path / "nothing.pptx"
    result = sweep_to_palette(missing, PALETTE, 3.0)
    assert not result.applied and result.reason


# --------------------------------------------------------------------------- #
# The contrast pass
# --------------------------------------------------------------------------- #

INKS = {"dk1": "1A1A1A", "lt1": "FFFFFF"}


def test_white_on_a_pale_card_is_recoloured(tmp_path):
    """The defect the pass exists for, and the one the sweep itself creates: a
    card snapped from a pale tint to a mid brand colour, with a white label on
    it that was fine before and is now unreadable."""
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "B9C0C8", text="Readable?", ink="FFFFFF")
    presentation.save(str(path))

    result = enforce_contrast(path, INKS)

    assert result.applied
    assert result.pairs == {"FFFFFF": "1A1A1A"}
    assert contrast_ratio("1A1A1A", "B9C0C8") >= 4.5


def test_copy_that_reads_perfectly_well_is_left_alone(tmp_path):
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "FFFFFF", text="Fine", ink="1A1A1A")
    presentation.save(str(path))

    assert not enforce_contrast(path, INKS).applied


def test_the_nearest_legible_colour_wins_not_the_highest_contrast(tmp_path):
    """The correction should be the smallest visible change that makes the
    copy readable. A caption in a pale grey becomes the darkest grey the
    master sets on text, not black."""
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "FFFFFF", text="Caption", ink="D0D4D8")
    presentation.save(str(path))

    inks = {"dk1": "000000", "mid": "4A4A4A"}
    result = enforce_contrast(path, inks)

    assert result.pairs == {"D0D4D8": "4A4A4A"}
    assert delta_e("D0D4D8", "4A4A4A") < delta_e("D0D4D8", "000000")


def test_large_type_is_held_to_the_large_floor(tmp_path):
    """WCAG's own split. A 24pt heading at 3.5:1 is readable and a 9pt caption
    at the same ratio is not, so holding both to 4.5:1 costs a correction
    nobody wanted on the heading."""
    presentation, path = _deck(tmp_path)
    slide = _blank(presentation)
    # 3.4:1 against white -- under the body floor, over the large one.
    _card(slide, "FFFFFF", top=1.0, text="Heading", ink="8A8A8A", size=24)
    presentation.save(str(path))

    assert not enforce_contrast(path, INKS).applied


def test_a_pairing_no_master_colour_can_fix_is_said_rather_than_guessed(tmp_path):
    """A master that writes in three mid-tones has nothing readable on a
    mid-tone card, and the answer is to move the copy or recolour the card --
    both a designer's call."""
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "7F7F7F", text="Lost", ink="8A8A8A")
    presentation.save(str(path))

    result = enforce_contrast(path, {"mid": "6E7B8B", "mid2": "7A8590"})

    assert not result.applied
    assert result.unresolved and "slide 1" in result.unresolved[0]


def test_a_run_that_states_no_colour_is_left_to_the_master(tmp_path):
    """It is inheriting from the placeholder, which is the master having
    already decided. Writing a literal onto the run would freeze that decision
    into the file so a later master could not reach it."""
    presentation, path = _deck(tmp_path)
    shape = _card(_blank(presentation), "B9C0C8")
    shape.text_frame.paragraphs[0].add_run().text = "Inherited"
    presentation.save(str(path))

    assert not enforce_contrast(path, INKS).applied


def test_text_over_a_gradient_is_not_judged(tmp_path):
    """A gradient has no one colour to measure against, and reporting the
    slide behind it would give the worst pairing in a deck a clean bill of
    health. That question belongs to the model, which is looking at a
    picture."""
    presentation, path = _deck(tmp_path)
    band = _blank(presentation).shapes.add_shape(
        1, Inches(1), Inches(1), Inches(6), Inches(1)
    )
    band.fill.gradient()
    run = band.text_frame.paragraphs[0].add_run()
    run.text = "Over a gradient"
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor.from_string("F4F4F4")
    presentation.save(str(path))

    assert not enforce_contrast(path, INKS).applied


def test_a_table_cells_copy_is_measured_against_the_cell(tmp_path):
    """A TABLE IS NOT A SHAPE WITH PARAGRAPHS ON IT. Its copy is on its cells,
    each with a fill of its own, so every pass that walks `text_frame` sees an
    empty graphic frame -- and a header row filled in the brand colour with
    white type on it is where this defect actually lives."""
    presentation, path = _deck(tmp_path)
    frame = _blank(presentation).shapes.add_table(
        2, 2, Inches(1), Inches(1), Inches(6), Inches(2)
    )
    cell = frame.table.cell(0, 0)
    cell.fill.solid()
    cell.fill.fore_color.rgb = RGBColor.from_string("B9C0C8")
    run = cell.text_frame.paragraphs[0].add_run()
    run.text = "Header"
    run.font.size = Pt(11)
    run.font.color.rgb = RGBColor.from_string("FFFFFF")
    presentation.save(str(path))

    result = enforce_contrast(path, INKS)

    assert result.applied and result.pairs == {"FFFFFF": "1A1A1A"}


def test_a_master_that_writes_no_text_colour_leaves_the_deck_alone(tmp_path):
    presentation, path = _deck(tmp_path)
    _card(_blank(presentation), "B9C0C8", text="Readable?", ink="FFFFFF")
    presentation.save(str(path))

    result = enforce_contrast(path, {})

    assert not result.applied and result.reason
