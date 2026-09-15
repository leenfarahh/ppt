"""A set of repeated elements that disagrees, and a colour the master never writes.

Four card headings on one real deck were three #007DBA and one #004F71. Both
are entries in the master's own theme, so every colour rule passed all four,
and the set reading as three-plus-one was invisible to a tool that only ever
looked at one shape at a time. The AI layer recoloured exactly one of them and
nothing said a word about the other three.

Two rules answer that slide between them, and neither is enough alone.
`consistency.series_formatting` sees the set disagree but cannot say which
side is right: the majority there was the defect and the minority was the
correction already applied. `color.text.unused_by_master` says which, because
#007DBA is accent3 and no layout in that master puts text in it anywhere.

The pair also has to stay quiet on an ordinary deck, which is most of the
reason for the tie and inheritance cases below.
"""

from __future__ import annotations

from formatting_tool.extract import derive_master_spec
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
from formatting_tool.rules.colors import (
    TextColorUnusedByMasterRule,
    master_text_colors,
)
from formatting_tool.rules.repeats import SeriesFormattingRule

# The master from the deck this was written for. dk2 and accent3 are both in
# the theme; only dk2 is ever put on a word.
THEME = {
    "dk1": "000000", "lt1": "FFFFFF", "dk2": "004F71", "lt2": "E8E8E8",
    "accent1": "004F71", "accent2": "136596", "accent3": "007DBA",
}


def _heading(shape_id: int, colour, *, size: float = 16.0, bold: bool = True):
    """One card heading: a loose text box, as the real ones are."""
    run = RunProfile(text="Card heading", size_pt=size, bold=bold, color_hex=colour)
    return ShapeProfile(
        shape_id=shape_id,
        name=f"Google Shape;{shape_id};p15",
        shape_type="TEXT_BOX",
        geometry=Geometry(0.0, 0.0, 2.75, 0.611),
        paragraphs=[ParagraphProfile(text="Card heading", runs=[run])],
    )


def _ctx(shapes, *, master_text_hex="004F71") -> RuleContext:
    deck = DeckProfile(
        path="messy.pptx", width_in=13.333, height_in=7.5,
        slides=[SlideProfile(number=10, layout_name="Blank", shapes=shapes)],
        theme_colors=dict(THEME),
    )
    # `master_text_colors` reads the master's LAYOUTS, so the opinion under
    # test has to live on one. A title placeholder stating dk2 is the whole
    # of what this master says about text.
    title = ShapeProfile(
        shape_id=1, name="Title 1", shape_type="PLACEHOLDER",
        geometry=Geometry(0.0, 0.0, 10.0, 1.0),
        placeholder_type="TITLE (1)", placeholder_idx=0,
        text_color_hex=master_text_hex or None,
    )
    master = DeckProfile(
        path="master.pptx", width_in=13.333, height_in=7.5,
        layouts=[LayoutProfile(name="Blank", index=0, shapes=[title])],
        theme_colors=dict(THEME),
    )
    return RuleContext(deck=deck, spec=derive_master_spec(master, BrandGuidelines()))


# --------------------------------------------------------------------------- #
# The set disagrees
# --------------------------------------------------------------------------- #

def test_three_against_one_is_reported_once_against_the_odd_one() -> None:
    """The finding names the member that departs, not all four."""
    shapes = [
        _heading(66, "004F71"),
        _heading(94, "007DBA"),
        _heading(108, "007DBA"),
        _heading(122, "007DBA"),
    ]
    found = list(SeriesFormattingRule().check(_ctx(shapes)))
    assert len(found) == 1
    assert found[0].shape_id == 66
    assert "3 of the 4" in found[0].message


def test_the_majority_is_evidence_and_never_a_fix() -> None:
    """The majority on the real slide was the defect. Nothing may act on it."""
    from formatting_tool.apply.fixers import FIXERS, NEEDS_A_PERSON

    assert "consistency.series_formatting" not in FIXERS
    assert "not necessarily" in NEEDS_A_PERSON["consistency.series_formatting"]


def test_a_tie_states_no_intent_and_says_nothing() -> None:
    shapes = [
        _heading(1, "004F71"), _heading(2, "004F71"),
        _heading(3, "007DBA"), _heading(4, "007DBA"),
    ]
    assert list(SeriesFormattingRule().check(_ctx(shapes))) == []


def test_a_set_that_agrees_says_nothing() -> None:
    shapes = [_heading(i, "004F71") for i in range(1, 5)]
    assert list(SeriesFormattingRule().check(_ctx(shapes))) == []


def test_size_is_compared_as_well_as_colour() -> None:
    shapes = [
        _heading(1, "004F71"), _heading(2, "004F71"),
        _heading(3, "004F71"), _heading(4, "004F71", size=11.0),
    ]
    found = list(SeriesFormattingRule().check(_ctx(shapes)))
    assert [i.shape_id for i in found] == [4]
    assert "11pt" in found[0].found


def test_a_member_that_inherits_is_not_convicted_of_departing() -> None:
    """None means "states nothing", which is not "states something else"."""
    shapes = [_heading(i, "004F71") for i in range(1, 4)]
    assert list(SeriesFormattingRule().check(_ctx([*shapes, _heading(4, None)]))) == []


def test_fewer_members_than_the_floor_are_not_a_series() -> None:
    shapes = [_heading(1, "004F71"), _heading(2, "007DBA")]
    assert list(SeriesFormattingRule().check(_ctx(shapes))) == []


# --------------------------------------------------------------------------- #
# Which side is right
# --------------------------------------------------------------------------- #

def test_the_master_text_colours_are_narrower_than_its_theme() -> None:
    """accent3 is in the theme. It is not a colour the master writes text in."""
    allowed = master_text_colors(_ctx([_heading(1, "004F71")]).spec)
    assert "004F71" in allowed.values()
    assert "007DBA" not in allowed.values()


def test_an_on_palette_colour_the_master_never_writes_text_in_is_reported() -> None:
    shapes = [
        _heading(66, "004F71"),
        _heading(94, "007DBA"),
        _heading(108, "007DBA"),
        _heading(122, "007DBA"),
    ]
    found = list(TextColorUnusedByMasterRule().check(_ctx(shapes)))
    assert sorted(i.shape_id for i in found) == [94, 108, 122]
    assert "007DBA" in found[0].found


def test_a_colour_the_master_does_write_text_in_passes() -> None:
    assert list(TextColorUnusedByMasterRule().check(_ctx([_heading(1, "004F71")]))) == []


def test_a_master_that_states_no_text_colour_measures_nothing() -> None:
    """No opinion is not an opinion that everything is wrong."""
    ctx = _ctx([_heading(1, "007DBA")], master_text_hex=None)
    assert list(TextColorUnusedByMasterRule().check(ctx)) == []


def test_the_two_rules_together_name_the_defect_and_the_side() -> None:
    """The pincer: one says the set disagrees, the other says which side moves."""
    shapes = [
        _heading(66, "004F71"),
        _heading(94, "007DBA"),
        _heading(108, "007DBA"),
        _heading(122, "007DBA"),
    ]
    ctx = _ctx(shapes)
    disagree = [i.shape_id for i in SeriesFormattingRule().check(ctx)]
    disowned = {i.shape_id for i in TextColorUnusedByMasterRule().check(ctx)}
    # The set is reported as inconsistent against the one that is correct...
    assert disagree == [66]
    # ...and the three the master disowns are named separately, so a reader
    # seeing both knows it is the majority that has to move.
    assert disowned == {94, 108, 122}
