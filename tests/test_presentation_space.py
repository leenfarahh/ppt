"""The presentation space: a shape a designer marks "PS" to say where content
may go.

Replaces guessing the usable area from where placeholders happen to sit. The
guess was always a proxy for a decision somebody had already made and had no
way of writing down; PS is that decision, stated in the file.

Read per layout, because a layout is the unit that offers space. A two-column
layout draws two PS rectangles and genuinely offers a different area than a
full-width one, and a single deck-wide frame cannot say so: it has to take the
roomiest edge any layout offers and apply it everywhere.
"""

from __future__ import annotations

from formatting_tool.extract.master_spec import derive_master_spec
from formatting_tool.models import (
    BrandGuidelines,
    DeckProfile,
    Geometry,
    LayoutProfile,
    Margins,
    ParagraphProfile,
    RunProfile,
    ShapeProfile,
    SlideProfile,
    TextRole,
)
from formatting_tool.rules import RuleContext
from formatting_tool.rules.space import SafeMarginRule

CANVAS_W, CANVAS_H = 13.333, 7.5

# The geometry of a real master, measured. PS reaches further left than the
# placeholders (0.59 against 0.92) and starts below the title (2.00 against
# 0.40), which is what makes both halves of the union necessary.
PS_LEFT, PS_TOP, PS_RIGHT, PS_BOTTOM = 0.586, 1.997, 12.747, 6.755
PH_LEFT, PH_TOP, PH_RIGHT, PH_BOTTOM = 0.917, 0.399, 12.417, 6.756


def _shape(name, box, *, alt="", token=None, role=TextRole.UNKNOWN, text=""):
    left, top, right, bottom = box
    return ShapeProfile(
        shape_id=abs(hash(name)) % 9999,
        name=name,
        shape_type="AUTO_SHAPE (1)" if not text else "TEXT_BOX (17)",
        geometry=Geometry(left_in=left, top_in=top,
                          width_in=right - left, height_in=bottom - top),
        placeholder_type=token,
        alt_text=alt,
        role=role,
        text=text,
        paragraphs=[ParagraphProfile(text=text, runs=[RunProfile(text=text)])]
        if text else [],
    )


def _layout(name="title_content", *, ps=True, extra=()):
    shapes = [
        _shape("Title 1", (PH_LEFT, PH_TOP, PH_RIGHT, 1.849), token="TITLE (1)"),
        _shape("Body 1", (PH_LEFT, PS_TOP, PH_RIGHT, PH_BOTTOM), token="OBJECT (7)"),
        # Chrome sits in the margin by design and must never widen the frame.
        _shape("Footer 1", (PH_LEFT, 6.951, 4.417, 7.35), token="FOOTER (15)"),
        _shape("Page 1", (9.417, 6.951, 12.417, 7.35), token="SLIDE_NUMBER (13)"),
    ]
    if ps:
        shapes.append(
            _shape("Rectangle 6", (PS_LEFT, PS_TOP, PS_RIGHT, PS_BOTTOM), alt="PS")
        )
    shapes.extend(extra)
    return LayoutProfile(name=name, index=0, shapes=shapes)


def _ctx(slide, layouts, guidelines=None):
    master = DeckProfile(path="master.pptx", width_in=CANVAS_W,
                         height_in=CANVAS_H, layouts=layouts)
    deck = DeckProfile(path="messy.pptx", width_in=CANVAS_W,
                       height_in=CANVAS_H, slides=[slide])
    return RuleContext(
        deck=deck,
        spec=derive_master_spec(master, guidelines or BrandGuidelines()),
    )


def _probe(layout_name, left):
    """A slide on `layout_name` with one text box at `left`."""
    return SlideProfile(
        number=1,
        layout_name=layout_name,
        shapes=[_shape("TextBox 3", (left, 3.0, left + 3.0, 3.4), text="copy")],
    )


# --------------------------------------------------------------------------- #
# Reading the mark
# --------------------------------------------------------------------------- #

def test_a_shape_marked_ps_is_the_presentation_space() -> None:
    assert [s.name for s in _layout().presentation_space] == ["Rectangle 6"]


def test_the_mark_is_case_insensitive() -> None:
    layout = LayoutProfile(name="l", index=0, shapes=[
        _shape("A", (1, 1, 2, 2), alt="ps"),
        _shape("B", (1, 1, 2, 2), alt=" PS "),
    ])

    assert len(layout.presentation_space) == 2


def test_the_mark_is_the_whole_alt_text_not_a_prefix() -> None:
    """Alt text is prose on most shapes. "PS logo lockup" describes a logo."""
    layout = LayoutProfile(name="l", index=0, shapes=[
        _shape("A", (1, 1, 2, 2), alt="PS logo lockup"),
        _shape("B", (1, 1, 2, 2), alt="a photo of the PS"),
    ])

    assert layout.presentation_space == []


def test_the_old_pres_space_mark_is_not_accepted() -> None:
    """Deliberate. Masters in the wild carry `pres_space`, and quietly
    accepting both would leave nobody able to tell which had been updated to
    the convention designers are being asked for. Such a master reads as
    marking nothing and falls back to its placeholders."""
    layout = LayoutProfile(name="l", index=0, shapes=[
        _shape("A", (1, 1, 2, 2), alt="pres_space"),
    ])

    assert layout.presentation_space == []
    assert layout.content_frame() is None


def test_a_ps_shape_inside_a_group_is_still_found() -> None:
    group = _shape("Group 1", (PS_LEFT, PS_TOP, PS_RIGHT, PS_BOTTOM))
    group.is_group = True
    group.children = [
        _shape("Rectangle 9", (PS_LEFT, PS_TOP, PS_RIGHT, PS_BOTTOM), alt="PS")
    ]
    layout = LayoutProfile(name="l", index=0, shapes=[group])

    assert [s.name for s in layout.presentation_space] == ["Rectangle 9"]


# --------------------------------------------------------------------------- #
# The frame it implies
# --------------------------------------------------------------------------- #

def test_the_frame_unions_ps_with_the_content_placeholders() -> None:
    """Both halves are needed, and a real master shows why.

    PS reaches left of the placeholders, so PS widens the frame. PS also
    starts below the title, so PS alone would put every title outside the
    frame it was placed by.
    """
    frame = _layout().content_frame()

    assert frame.left_in == PS_LEFT      # from PS, left of the placeholders
    assert frame.top_in == PH_TOP        # from the title, above PS
    assert frame.right_in == PS_RIGHT    # from PS
    assert round(frame.bottom_in, 3) == PH_BOTTOM


def test_chrome_never_widens_the_frame() -> None:
    """A footer at the slide edge would open the frame to the edge."""
    frame = _layout().content_frame()

    assert frame.bottom_in < 6.9    # the footer sits at 6.95


def test_a_layout_that_marks_nothing_has_no_frame() -> None:
    assert _layout(ps=False).content_frame() is None


def test_two_ps_rectangles_union() -> None:
    """A two-column layout draws one per column."""
    layout = LayoutProfile(name="cols", index=0, shapes=[
        _shape("L", (0.586, 1.997, 6.583, 6.755), alt="PS"),
        _shape("R", (6.750, 1.997, 12.747, 6.755), alt="PS"),
    ])
    frame = layout.content_frame()

    assert (frame.left_in, frame.right_in) == (0.586, 12.747)


# --------------------------------------------------------------------------- #
# What the rule does with it
# --------------------------------------------------------------------------- #

def test_content_inside_the_presentation_space_is_not_reported() -> None:
    """x=0.70 is outside every placeholder and inside PS.

    That band is the whole point: it is legal space the old derivation had no
    way of knowing about, so content there was reported on every deck.
    """
    layouts = [_layout()]
    issues = list(SafeMarginRule().check(_ctx(_probe("title_content", 0.70), layouts)))

    assert issues == []


def test_content_outside_the_presentation_space_is_still_reported() -> None:
    issues = list(SafeMarginRule().check(_ctx(_probe("title_content", 0.30), [_layout()])))

    assert len(issues) == 1
    assert "left by" in issues[0].message


def test_each_slide_is_judged_against_its_own_layout() -> None:
    """The same box at the same place: legal on one layout, not on the other.

    This is what per-layout buys. A single deck-wide frame has to take the
    roomiest edge any layout offers, so the narrow layout inherits the wide
    one's allowance and content crowding it goes unreported.
    """
    wide = _layout("wide")
    narrow = _layout("narrow", ps=False, extra=[
        _shape("Rectangle 7", (4.0, PS_TOP, 12.0, PS_BOTTOM), alt="PS"),
    ])
    # narrow's frame comes from its own PS unioned with its placeholders.
    layouts = [wide, narrow]

    on_wide = list(SafeMarginRule().check(_ctx(_probe("wide", 0.70), layouts)))
    on_narrow = list(SafeMarginRule().check(_ctx(_probe("narrow", 0.70), layouts)))

    assert on_wide == []
    assert [i.shape for i in on_narrow] == ["TextBox 3"]


def test_a_layout_marking_nothing_falls_back_to_the_deck_wide_frame() -> None:
    """Not to its own placeholders, which would be far stricter than the
    reading this rule has always applied. A cover marks no presentation space
    and its two placeholders sit well inside the slide; holding it to them
    would report the logo on every cover in every deck."""
    cover = LayoutProfile(name="Cover", index=0, shapes=[
        _shape("Title", (1.667, 1.227, 11.667, 3.838), token="CENTER_TITLE (3)"),
    ])
    layouts = [_layout("title_content"), cover]

    # 0.70 is outside the cover's own placeholders but inside the deck-wide
    # frame, which the PS-bearing layout widened to 0.59.
    assert list(SafeMarginRule().check(_ctx(_probe("Cover", 0.70), layouts))) == []


def test_an_unknown_layout_falls_back_to_the_deck_wide_frame() -> None:
    slide = _probe("a name the master has never heard of", 0.70)

    assert list(SafeMarginRule().check(_ctx(slide, [_layout()]))) == []


def test_an_authored_margin_still_wins_over_the_drawn_one() -> None:
    """A PS rectangle is a drawing; the brand file is what somebody signed."""
    guidelines = BrandGuidelines(safe_margins=Margins(left_in=1.25))
    ctx = _ctx(_probe("title_content", 0.70), [_layout()], guidelines)

    issues = list(SafeMarginRule().check(ctx))

    assert [i.shape for i in issues] == ["TextBox 3"]
    assert "left by 0.55in" in issues[0].message


# --------------------------------------------------------------------------- #
# The deck-wide frame, which the payload and the fallback both read
# --------------------------------------------------------------------------- #

def test_the_deck_wide_frame_prefers_presentation_space() -> None:
    with_ps = derive_master_spec(
        DeckProfile(path="m.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
                    layouts=[_layout()]),
        BrandGuidelines(),
    )
    without = derive_master_spec(
        DeckProfile(path="m.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
                    layouts=[_layout(ps=False)]),
        BrandGuidelines(),
    )

    assert with_ps.safe_margins.left_in == 0.59     # PS
    assert without.safe_margins.left_in == 0.92     # the placeholders
