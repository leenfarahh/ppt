"""Text that cannot be read off what it sits on.

Every colour on a slide can be on the brand palette and the slide still be
unreadable: a palette says which colours are allowed and never which pairs of
them may be stacked. `colorutil.contrast_ratio` has been in the tool since the
colour plan was written and was only ever a VETO -- `_refuse_illegible` stands
a recolour down when it would leave text unreadable, and its own comment says
a shape whose text was already unreadable "is a finding of its own". These are
that finding.

The half of the question the file cannot answer is the design check's: a
caption over the dark half of a photograph has no background colour to measure
against, and the tests here pin the rule's SILENCE on those as carefully as
they pin what it reports. A contrast check that guesses at a photograph is
worse than one that says nothing about it, because the slide it gets wrong is
the slide a reader notices first.
"""

from __future__ import annotations

from formatting_tool.apply import fixer_for
from formatting_tool.apply.fixers import LeaveAlone, _HEX
from formatting_tool.extract import derive_master_spec
from formatting_tool.guidelines import BrandGuidelines
from formatting_tool.models import (
    DeckProfile,
    Geometry,
    LayoutProfile,
    ParagraphProfile,
    RunProfile,
    Severity,
    ShapeProfile,
    SlideProfile,
)
from formatting_tool.rules.base import RuleContext
from formatting_tool.rules.contrast import TextContrastRule

WIDE, TALL = 13.333, 7.5

# The master used by every context here writes words in exactly three colours:
# near-black on the body of a content layout, the brand red on its title, and
# white on the title of a cover. Those three are the whole of what
# `colors.master_text_colors` will offer as a target, which is the point -- the
# set a text colour belongs to is the set the master writes words in, not the
# twelve swatches of a theme.
INK = "1A1A1A"
RED = "A32020"
PAPER = "FFFFFF"


def _shape(name, left, top, width, height, **kwargs):
    runs = kwargs.pop("runs", None)
    text = kwargs.pop("text", "")
    paragraphs = []
    if runs is not None:
        paragraphs = [ParagraphProfile(text="".join(r.text for r in runs), runs=runs)]
    elif text:
        paragraphs = [ParagraphProfile(text=text, runs=[RunProfile(text=text)])]
    return ShapeProfile(
        shape_id=kwargs.pop("shape_id", abs(hash(name)) % 9999),
        name=name,
        shape_type=kwargs.pop("shape_type", "TEXT_BOX (17)"),
        geometry=Geometry(left_in=left, top_in=top, width_in=width, height_in=height),
        text=paragraphs[0].text if paragraphs else "",
        paragraphs=paragraphs,
        **kwargs,
    )


def _run(text, color=None, size_pt=12.0, bold=False, theme=None):
    return RunProfile(
        text=text, color_hex=color, color_theme=theme, size_pt=size_pt, bold=bold
    )


def _master() -> DeckProfile:
    layout = LayoutProfile(
        name="content",
        index=0,
        shapes=[
            ShapeProfile(
                shape_id=1, name="Title 1", shape_type="PLACEHOLDER (14)",
                geometry=Geometry(left_in=0.5, top_in=0.5, width_in=12.0, height_in=1.0),
                placeholder_type="TITLE (13)", placeholder_idx=0,
                text_color_hex=RED,
            ),
            ShapeProfile(
                shape_id=2, name="Text Placeholder 2", shape_type="PLACEHOLDER (14)",
                geometry=Geometry(left_in=0.5, top_in=2.0, width_in=12.0, height_in=4.0),
                placeholder_type="BODY (2)", placeholder_idx=1,
                text_color_hex=INK,
            ),
        ],
    )
    cover = LayoutProfile(
        name="cover",
        index=1,
        shapes=[
            ShapeProfile(
                shape_id=1, name="Title 1", shape_type="PLACEHOLDER (14)",
                geometry=Geometry(left_in=0.5, top_in=3.0, width_in=12.0, height_in=1.5),
                placeholder_type="TITLE (13)", placeholder_idx=0,
                text_color_hex=PAPER,
            ),
        ],
    )
    return DeckProfile(path="master.pptx", width_in=WIDE, height_in=TALL,
                       layouts=[layout, cover])


def _ctx(*shapes, background="FFFFFF", theme=None, layout_name="content"):
    deck = DeckProfile(
        path="messy.pptx", width_in=WIDE, height_in=TALL,
        theme_colors=theme or {},
        slides=[SlideProfile(number=1, layout_name=layout_name,
                             shapes=list(shapes), background_hex=background)],
    )
    return RuleContext(deck=deck, spec=derive_master_spec(_master(), BrandGuidelines()))


def _found(ctx):
    return list(TextContrastRule().check(ctx))


# --------------------------------------------------------------------------- #
# Where the background comes from
#
# New plumbing, and the reason the rule can say anything at all about the
# commonest shape on a deck: a text box with no fill of its own. Read from the
# XML rather than through python-pptx, whose `slide.background.fill` calls
# `get_or_add_bgPr()` and WRITES a `<p:bgPr>` into a slide that had none.
# --------------------------------------------------------------------------- #

_NS = (
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
)
THEME = {"lt1": "FFFFFF", "dk1": "111111", "lt2": "DEDEDE", "dk2": "203864",
         "accent1": "A32020"}


def _xml(fragment: str):
    from lxml import etree
    return etree.fromstring(f"<p:wrap {_NS}>{fragment}</p:wrap>".encode())[0]


def _bg(fragment: str, color_map=None):
    from formatting_tool.extract.deck_reader import _background_color
    return _background_color(_xml(fragment), color_map or {}, THEME)


def test_a_literal_background_is_read_as_it_is_written() -> None:
    assert _bg('<p:bg><p:bgPr><a:solidFill><a:srgbClr val="f5f5f5"/>'
               '</a:solidFill></p:bgPr></p:bg>') == "F5F5F5"


def test_a_scheme_background_goes_through_the_masters_colour_map() -> None:
    """`bg1` is not a colour in the theme. It is a slot the master points at
    one of lt1, dk1, lt2 or dk2, and a master that points bg1 at dk1 is how a
    dark deck is built. Reading the theme directly for `bg1` finds nothing on
    a well-formed file and the wrong thing on that one."""
    fragment = ('<p:bg><p:bgPr><a:solidFill><a:schemeClr val="bg1"/>'
                '</a:solidFill></p:bgPr></p:bg>')
    assert _bg(fragment, {"bg1": "lt1"}) == "FFFFFF"
    assert _bg(fragment, {"bg1": "dk1"}) == "111111"


def test_the_first_background_fill_style_is_a_colour_and_the_rest_are_not(
) -> None:
    """idx 1001 is the first entry of the theme's background fill list, which
    is a solid fill in every theme PowerPoint writes. The higher indices are
    its gradient and textured variants, and those have no single colour."""
    solid = ('<p:bg><p:bgRef idx="1001"><a:schemeClr val="dk2"/>'
             '</p:bgRef></p:bg>')
    assert _bg(solid, {"dk2": "dk2"}) == "203864"

    gradient = ('<p:bg><p:bgRef idx="1002"><a:schemeClr val="dk2"/>'
                '</p:bgRef></p:bg>')
    assert _bg(gradient, {"dk2": "dk2"}) is None


def test_a_colour_carrying_a_transform_is_not_the_colour_that_gets_drawn(
) -> None:
    """Working out what `lumMod` draws means reimplementing DrawingML's colour
    model. Reporting None is not a shortcut: a contrast check measured against
    the wrong background is worse than one that says nothing about that slide.
    """
    assert _bg('<p:bg><p:bgPr><a:solidFill><a:schemeClr val="lt2">'
               '<a:lumMod val="75000"/></a:schemeClr></a:solidFill>'
               '</p:bgPr></p:bg>', {"lt2": "lt2"}) is None


def test_a_picture_or_a_gradient_behind_the_slide_is_not_a_colour() -> None:
    assert _bg('<p:bg><p:bgPr><a:blipFill><a:blip/></a:blipFill>'
               '</p:bgPr></p:bg>') is None
    assert _bg('<p:bg><p:bgPr><a:gradFill><a:gsLst/></a:gradFill>'
               '</p:bgPr></p:bg>') is None


# --------------------------------------------------------------------------- #
# What it measures against
# --------------------------------------------------------------------------- #

def test_text_is_measured_against_the_fill_of_the_shape_it_is_in() -> None:
    """Text in a filled box sits on that box and on nothing else, whatever is
    under it."""
    ctx = _ctx(_shape("Card 2", 1.0, 1.0, 4.0, 2.0, fill_hex="DEDEDE",
                      runs=[_run("Our perspective", color="FFFFFF")]))

    [issue] = _found(ctx)
    assert issue.severity is Severity.ERROR
    assert "#FFFFFF on #DEDEDE" in issue.message and "1.3:1" in issue.message


def test_an_unfilled_label_is_measured_against_the_card_under_it() -> None:
    """Nearest means last in document order among the shapes drawn before this
    one that contain it: document order is z-order, so the last one drawn is
    the one the text lands on."""
    ctx = _ctx(
        _shape("Panel 1", 0.5, 0.5, 8.0, 5.0, fill_hex="FFFFFF"),
        _shape("Card 2", 1.0, 1.0, 4.0, 2.0, fill_hex="203864"),
        _shape("Label 3", 1.2, 1.2, 3.0, 0.5,
               runs=[_run("Phase one", color="333333")]),
    )

    [issue] = _found(ctx)
    # The navy card, not the white panel behind it and not the white slide.
    assert "on #203864" in issue.message


def test_text_on_nothing_is_measured_against_the_slide_background() -> None:
    ctx = _ctx(
        _shape("Caption 1", 1.0, 1.0, 4.0, 0.5,
               runs=[_run("Source: internal", color="F2F2F2")]),
        background="FFFFFF",
    )

    [issue] = _found(ctx)
    assert "on #FFFFFF" in issue.message


def test_a_picture_under_the_text_ends_the_search_rather_than_falling_through(
) -> None:
    """The case this rule most wants to report and is least able to. The
    photograph has no colour, and reporting the white slide BEHIND the
    photograph would give the worst slide in the deck a clean bill of health."""
    ctx = _ctx(
        _shape("Picture 1", 0.0, 0.0, WIDE, TALL, shape_type="PICTURE (13)",
               is_picture=True),
        _shape("Caption 2", 1.0, 1.0, 4.0, 0.5,
               runs=[_run("Riyadh, 2026", color="FFFFFF")]),
    )
    assert _found(ctx) == []


def test_a_background_that_is_not_a_colour_is_not_measured() -> None:
    """`background_hex` is None for a gradient, a picture or a pattern, and
    None means "not a colour" rather than "white"."""
    ctx = _ctx(
        _shape("Caption 1", 1.0, 1.0, 4.0, 0.5,
               runs=[_run("Riyadh, 2026", color="FFFFFF")]),
        background=None,
    )
    assert _found(ctx) == []


def test_a_theme_bound_colour_is_resolved_through_the_deck_that_will_draw_it(
) -> None:
    """A run bound to lt1 and a fill bound to lt2 carry no RGB at all. What a
    reader sees is what THIS deck's theme says those slots hold."""
    ctx = _ctx(
        _shape("Chip 1", 1.0, 1.0, 3.0, 0.6, fill_theme="lt2",
               runs=[_run("Our approach", theme="lt1")]),
        theme={"lt1": "FFFFFF", "lt2": "DEDEDE", "dk1": "000000"},
    )

    [issue] = _found(ctx)
    assert "#FFFFFF on #DEDEDE" in issue.message


def test_a_fill_that_is_drawn_and_is_not_one_colour_stops_the_search() -> None:
    """`fill_hex` cannot tell a gradient card from an unfilled text box: both
    arrive as None. So a gradient was stepped straight over and its caption
    measured against the slide behind it, which is the same lie a photograph
    would tell by a quieter route."""
    for kind in ("gradient", "picture", "textured", "pattern", "inherit"):
        ctx = _ctx(
            _shape("Card 1", 0.5, 0.5, 8.0, 5.0, fill_kind=kind),
            _shape("Caption 2", 1.0, 1.0, 4.0, 0.5, fill_kind="background",
                   runs=[_run("Riyadh, 2026", color="F2F2F2")]),
        )
        assert _found(ctx) == [], f"{kind} was stepped over"

    # And the shape's own fill, on the same terms: a patterned fill arrives as
    # the pattern's FOREGROUND colour, as though the whole shape were solid.
    ctx = _ctx(_shape("Card 1", 1.0, 1.0, 4.0, 2.0,
                      fill_hex="000000", fill_kind="pattern",
                      runs=[_run("Our perspective", color="1A1A1A")]))
    assert _found(ctx) == []


def test_a_profile_that_names_no_fill_kind_behaves_as_it_always_did() -> None:
    """Every fixture built before `fill_kind` existed, and every caller older
    than it, leaves the field None. That has to keep measuring rather than
    going silent."""
    ctx = _ctx(_shape("Card 1", 1.0, 1.0, 4.0, 2.0, fill_hex="DEDEDE",
                      runs=[_run("Our perspective", color="FFFFFF")]))
    assert len(_found(ctx)) == 1


# --------------------------------------------------------------------------- #
# Tables
#
# A table is not a shape with paragraphs on it: its copy lives on its cells,
# and `ShapeProfile.table` keeps those out of `children` on purpose. So a rule
# walking `shape.paragraphs` saw a table as an empty graphic frame -- which
# left unmeasured the commonest place this defect lives on a client deck, a
# header row filled in a mid-tone brand colour with white type on it.
# --------------------------------------------------------------------------- #

def _cell(row, column, fill=None, runs=(), kind="solid"):
    from formatting_tool.models import TableCell
    return TableCell(
        row=row, column=column, fill_hex=fill,
        fill_kind=kind if fill else "background",
        paragraphs=[ParagraphProfile(text="".join(r.text for r in runs),
                                     runs=list(runs))] if runs else [],
    )


def _table_shape(*cells, name="Table 1", **kwargs):
    from formatting_tool.models import TableProfile
    shape = _shape(name, 1.0, 1.0, 8.0, 3.0, shape_type="TABLE (19)", **kwargs)
    shape.table = TableProfile(rows=2, columns=3, cells=list(cells))
    return shape


def test_a_header_row_nobody_could_read_is_reported_once() -> None:
    """Five cells carrying white on the same mid-tone navy is one decision and
    one defect. Five identical lines about it is five times less likely to be
    read, so the pairing is grouped across the table and the count goes in the
    sentence."""
    header = [_cell(0, column, fill="8A8A8A",
                    runs=[_run("Region", color="FFFFFF", size_pt=11.0)])
              for column in range(3)]
    body = [_cell(1, column, runs=[_run("KSA", color="1A1A1A", size_pt=11.0)])
            for column in range(3)]
    ctx = _ctx(_table_shape(*header, *body))

    [issue] = _found(ctx)
    assert "#FFFFFF on #8A8A8A" in issue.message
    assert "in 3 cell(s)" in issue.message
    # The body cells state no fill, so they sit on the slide and read fine.
    assert issue.found.startswith("#FFFFFF")


def test_two_pairings_in_one_table_are_two_findings() -> None:
    ctx = _ctx(_table_shape(
        _cell(0, 0, fill="8A8A8A", runs=[_run("Region", color="FFFFFF")]),
        _cell(1, 0, fill="F2F2F2", runs=[_run("KSA", color="E8E8E8")]),
    ))
    assert len(_found(ctx)) == 2


def test_a_cell_whose_fill_is_the_table_styles_is_not_guessed_at() -> None:
    """A banded table style paints alternate rows and says so nowhere this can
    read, so the cell is skipped rather than measured against whatever is
    behind the table."""
    ctx = _ctx(_table_shape(
        _cell(0, 0, fill=None, kind="inherit",
              runs=[_run("Region", color="FFFFFF")]),
    ))
    ctx.deck.slides[0].shapes[0].table.cells[0].fill_kind = "inherit"
    assert _found(ctx) == []


def test_the_fix_reaches_into_a_tables_cells() -> None:
    """A fixer reaching for `shape.text_frame` on a graphic frame finds nothing
    and quietly corrects nothing."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    table = slide.shapes.add_table(
        2, 2, Inches(1), Inches(1), Inches(6), Inches(2)
    ).table
    for column in range(2):
        cell = table.cell(0, column)
        run = cell.text_frame.paragraphs[0].add_run()
        run.text = "Region"
        run.font.size = Pt(11)
        run.font.color.rgb = RGBColor.from_string("FFFFFF")

    header = [_cell(0, column, fill="8A8A8A",
                    runs=[_run("Region", color="FFFFFF", size_pt=11.0)])
              for column in range(2)]
    ctx = _ctx(_table_shape(*header))
    [issue] = _found(ctx)

    detail = fixer_for(issue)(
        slide.shapes[0], issue, _FixCtx()
    )
    assert "recoloured 2 run(s) from #FFFFFF" in detail


# --------------------------------------------------------------------------- #
# Where the floor is
# --------------------------------------------------------------------------- #

def test_large_text_is_held_to_the_large_floor_and_body_copy_is_not() -> None:
    """WCAG's two numbers, not one. 3.0:1 for large text and 4.5:1 for body,
    and 3.6:1 is on opposite sides of that line at 24pt and at 11pt."""
    # #767676 on white is about 4.5:1; #8A8A8A is about 3.5:1.
    big = _ctx(_shape("Heading 1", 1.0, 1.0, 6.0, 1.0,
                      runs=[_run("Our approach", color="8A8A8A", size_pt=24.0)]))
    assert _found(big) == []

    small = _ctx(_shape("Caption 1", 1.0, 1.0, 6.0, 0.4,
                        runs=[_run("Our approach", color="8A8A8A", size_pt=11.0)]))
    [issue] = _found(small)
    assert issue.severity is Severity.WARNING      # readable, just not at 11pt
    assert "4.5:1" in issue.message


def test_bold_text_reaches_the_large_floor_four_points_earlier() -> None:
    ctx = _ctx(_shape("Label 1", 1.0, 1.0, 3.0, 0.4,
                      runs=[_run("KEY", color="8A8A8A", size_pt=14.0, bold=True)]))
    assert _found(ctx) == []


def test_a_run_that_states_no_size_is_held_to_the_body_floor() -> None:
    """Its size is inherited and could be anything, and the two ways of being
    wrong are not worth the same: a dismissed line against a lost caption."""
    ctx = _ctx(_shape("Label 1", 1.0, 1.0, 3.0, 0.4,
                      runs=[_run("KEY", color="8A8A8A", size_pt=None)]))
    assert len(_found(ctx)) == 1


def test_below_the_large_floor_is_an_error_and_above_it_a_warning() -> None:
    """Below 3.0:1 nothing is readable at any size, which is a defect. Between
    there and the floor for its size, a designer may reasonably decide a label
    at 4.0:1 is fine on a screen in a room they know."""
    hopeless = _ctx(_shape("A", 1, 1, 3, 0.4,
                           runs=[_run("x", color="BBBBBB", size_pt=11.0)]))
    [issue] = _found(hopeless)
    assert issue.severity is Severity.ERROR

    borderline = _ctx(_shape("B", 1, 1, 3, 0.4,
                             runs=[_run("x", color="8A8A8A", size_pt=11.0)]))
    [issue] = _found(borderline)
    assert issue.severity is Severity.WARNING


# --------------------------------------------------------------------------- #
# What it does not judge
# --------------------------------------------------------------------------- #

def test_the_masters_furniture_is_not_judged() -> None:
    """A page number set in the master's pale grey is deliberately quiet, and
    reporting it on every slide of a ninety slide deck buries everything else
    this rule finds."""
    ctx = _ctx(_shape("Slide Number 1", 12.0, 7.0, 0.5, 0.2,
                      placeholder_type="SLIDE_NUMBER (13)",
                      runs=[_run("4", color="F2F2F2", size_pt=9.0)]))
    assert _found(ctx) == []


def test_runs_sharing_a_colour_are_one_finding() -> None:
    """A heading that arrives as four runs because somebody retyped a word is
    one defect, not four lines on a report."""
    ctx = _ctx(_shape("Heading 1", 1.0, 1.0, 6.0, 1.0, fill_hex="DEDEDE", runs=[
        _run("Our ", color="FFFFFF"),
        _run("perspective", color="FFFFFF"),
        _run(" on it", color="FFFFFF"),
    ]))

    [issue] = _found(ctx)
    assert "#FFFFFF on #DEDEDE" in issue.message


def test_two_colours_on_one_shape_are_two_findings() -> None:
    """The real card this was changed for: a red label at 1.0:1 beside a black
    paragraph at 2.8:1 on a red fill. Two defects with two different answers,
    and reported as one they took two rounds to correct -- the fix recoloured
    the label and the card came back still carrying black text."""
    ctx = _ctx(_shape("Card 1", 1.0, 1.0, 4.0, 2.0, fill_hex="A32020", runs=[
        _run("Client staff: ", color="A32020", size_pt=14.0),   # 1.0:1
        _run("Faress Hamdy", color="000000", size_pt=12.0),     # 2.8:1
    ]))

    inks = sorted(issue.found.split(",")[0] for issue in _found(ctx))
    assert inks == ["#000000", "#A32020"]


def test_a_colour_set_at_two_sizes_is_judged_at_the_size_that_struggles() -> None:
    ctx = _ctx(_shape("Card 1", 1.0, 1.0, 4.0, 2.0, runs=[
        _run("Heading", color="8A8A8A", size_pt=24.0),   # clears 3.0:1
        _run("caption", color="8A8A8A", size_pt=9.0),    # does not clear 4.5:1
    ]))

    [issue] = _found(ctx)
    assert "4.5:1" in issue.message


def test_a_pairing_that_clears_its_floor_is_not_reported() -> None:
    ctx = _ctx(_shape("Card 1", 1.0, 1.0, 4.0, 2.0, fill_hex="A32020",
                      runs=[_run("Our approach", color="FFFFFF", size_pt=12.0)]))
    assert _found(ctx) == []


# --------------------------------------------------------------------------- #
# The target, and the fix that reads it
# --------------------------------------------------------------------------- #

def test_the_masters_own_colour_for_the_placeholder_is_the_target() -> None:
    """Nothing is guessed at where the master has already said what colour a
    body placeholder on this layout is."""
    ctx = _ctx(_shape("Text Placeholder 2", 1.0, 2.0, 6.0, 1.0,
                      placeholder_type="BODY (2)", placeholder_idx=1,
                      runs=[_run("Copy", color="F2F2F2", size_pt=11.0)]))

    [issue] = _found(ctx)
    assert issue.expected.startswith(f"#{INK} on #FFFFFF")
    assert "the master sets" in issue.expected


def test_otherwise_the_nearest_colour_the_master_writes_text_in() -> None:
    """Nearest, not the highest contrast available: the correction should be
    the smallest visible change that makes the copy readable."""
    ctx = _ctx(_shape("Loose box 1", 1.0, 1.0, 4.0, 2.0, fill_hex="DEDEDE",
                      runs=[_run("Our perspective", color="FFFFFF")]))

    [issue] = _found(ctx)
    # Of the two colours this master writes words in, the red clears 4.5:1 on
    # #DEDEDE and the near-black clears it by more. The red is nearer white.
    assert issue.expected.startswith(f"#{RED} on #DEDEDE")


def test_a_finding_with_no_legible_target_states_no_colour_at_all() -> None:
    """THE PROPERTY THE FIX DEPENDS ON. `fixers._hex_of` takes the first six
    hex digits in the string, so an `expected` reading "4.5:1 against #FFFFFF"
    would hand the fixer the BACKGROUND as the colour to set -- pale grey type
    on white, recoloured to white, and reported as a correction."""
    # A mid grey card, which is the one background that defeats all three: the
    # red reads 1.9:1 on it, white 4.0:1 and the near-black 4.4:1, and the body
    # floor is 4.5:1. The answer is to move the text, not to recolour it.
    ctx = _ctx(_shape("Card 1", 1.0, 1.0, 4.0, 2.0, fill_hex="808080",
                      runs=[_run("Our perspective", color="8E8E8E")]))

    [issue] = _found(ctx)
    assert _HEX.search(issue.expected or "") is None
    assert "Move the text" in issue.suggestion


def test_the_fix_recolours_only_the_runs_the_finding_measured() -> None:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    frame = box.text_frame
    paragraph = frame.paragraphs[0]
    for text, color in (("Client staff: ", "A32020"), ("Faress Hamdy", "000000")):
        run = paragraph.add_run()
        run.text = text
        run.font.size = Pt(12)
        from pptx.dml.color import RGBColor
        run.font.color.rgb = RGBColor.from_string(color)

    ctx = _ctx(_shape("Card 1", 1.0, 1.0, 4.0, 2.0, fill_hex="A32020",
                      runs=[_run("Client staff: ", color="A32020")]))
    [issue] = _found(ctx)

    detail = fixer_for(issue)(box, issue, _FixCtx())
    assert "from #A32020 to #FFFFFF" in detail
    reds = [r for p in frame.paragraphs for r in p.runs
            if str(r.font.color.rgb) == "A32020"]
    assert not reds                        # the one that was measured moved
    blacks = [r for p in frame.paragraphs for r in p.runs
              if str(r.font.color.rgb) == "000000"]
    assert len(blacks) == 1                # and the one that was legible did not


def test_the_fix_measures_the_target_again_before_it_writes() -> None:
    """A recolour earlier in the same round can change the fill the rule read.
    Rechecking costs one division and is the difference between a correction
    and a second defect written over the first."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = "Our perspective"
    run.font.size = Pt(12)
    run.font.color.rgb = RGBColor.from_string("F2F2F2")

    ctx = _ctx(_shape("Card 1", 1.0, 1.0, 4.0, 2.0, fill_hex="FFFFFF",
                      runs=[_run("Our perspective", color="F2F2F2")]))
    [issue] = _found(ctx)
    # The fill the finding was measured against is replaced with one the
    # target cannot be read on, which is what a fix earlier in the round does.
    issue.expected = f"#{INK} on #1A1A1A, which the master sets on 'content'"

    try:
        fixer_for(issue)(box, issue, _FixCtx())
    except LeaveAlone as refusal:
        assert "1.0:1" in str(refusal)
    else:                                   # pragma: no cover - the bug itself
        raise AssertionError("it recoloured the text into the fill")


class _Brand:
    """Enough of a FixBrand for `_palette_entry` to vouch for a colour."""

    palette = {"ink": INK, "red": RED, "paper": PAPER,
               "navy": "1E2761", "mist": "F2F2F2"}


class _FixCtx:
    """The little of a FixContext these fixers touch."""

    width_emu = int(WIDE * 914400)
    height_emu = int(TALL * 914400)
    spec = None
    brand = _Brand()
    color_plan = None

    def __init__(self):
        self.neighbours = []


# --------------------------------------------------------------------------- #
# The other side of the guard
#
# `_refuse_illegible` stops a FILL recolour leaving the text on it unreadable.
# Nothing stopped a TEXT recolour doing the same thing from the other side, and
# a text colour is the easier of the two to get wrong: the colour it has to be
# read against is usually not on the shape being recoloured at all.
# --------------------------------------------------------------------------- #

def _slide_with_a_button():
    """A navy button with a white label in a text box on top of it.

    The real shape of it, and the reason the shape's own fill is not enough to
    answer with: the label's own fill is nothing.
    """
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    button = slide.shapes.add_shape(1, Inches(1), Inches(1), Inches(2), Inches(0.5))
    button.fill.solid()
    button.fill.fore_color.rgb = RGBColor.from_string("1E2761")
    label = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(0.5))
    run = label.text_frame.paragraphs[0].add_run()
    run.text = "Continue"
    run.font.size = Pt(13)
    run.font.color.rgb = RGBColor.from_string("FFFFFF")
    return slide, label


def test_text_is_not_recoloured_into_the_shape_it_sits_on() -> None:
    """The round that made this necessary: a cross-slide colour mismatch set
    every label on a slide to the deck's majority navy, three of them sat on
    buttons, and one of those buttons was navy. 1.0:1, and the word vanished.
    """
    from formatting_tool.apply.fixers import _ai_recolor_text
    from formatting_tool.models import FixAction

    slide, label = _slide_with_a_button()
    ctx = _FixCtx()
    ctx.neighbours = list(slide.shapes)

    with __import__("pytest").raises(LeaveAlone) as refusal:
        _ai_recolor_text(label, FixAction(op="recolor_text", hex="1E2761"), ctx)
    assert "1.0:1" in str(refusal.value)
    # And the words are still white.
    run = label.text_frame.paragraphs[0].runs[0]
    assert str(run.font.color.rgb) == "FFFFFF"


def test_a_legible_recolour_still_goes_through() -> None:
    """A guard that refused everything it could not prove would decline the
    corrections it is there to allow."""
    from formatting_tool.apply.fixers import _ai_recolor_text
    from formatting_tool.models import FixAction

    slide, label = _slide_with_a_button()
    ctx = _FixCtx()
    ctx.neighbours = list(slide.shapes)

    detail = _ai_recolor_text(label, FixAction(op="recolor_text", hex="F2F2F2"), ctx)
    assert "recoloured 1 run(s)" in detail


def test_a_recolour_over_something_that_is_not_a_colour_is_not_second_guessed(
) -> None:
    """A caption over a photograph has no background to measure against, and a
    guard that refused every recolour it could not check would be the wrong
    kind of careful."""
    from formatting_tool.apply.fixers import _ai_recolor_text
    from formatting_tool.models import FixAction

    slide, label = _slide_with_a_button()
    slide.shapes[0].fill.gradient()          # the button is now a gradient
    ctx = _FixCtx()
    ctx.neighbours = list(slide.shapes)

    assert _ai_recolor_text(
        label, FixAction(op="recolor_text", hex="1E2761"), ctx
    ) is not None
