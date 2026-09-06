"""Tests for the two detections a real deck showed were missing.

A designer marked up two slides: a numbered badge sitting out of line with the
ten identical badges around it, and content crossing the frame the master
draws. Neither was reported. The first needed a rule; the second needed the
margin to come from somewhere other than a brand file nobody had written.
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
from formatting_tool.rules.repeats import RepeatedElementRule
from formatting_tool.rules.space import SafeMarginRule

CANVAS_W, CANVAS_H = 13.333, 7.5


def _badge(name: str, left: float, top: float) -> ShapeProfile:
    return ShapeProfile(
        shape_id=abs(hash(name)) % 9999,
        name=name,
        shape_type="AUTO_SHAPE (1)",
        geometry=Geometry(left_in=left, top_in=top, width_in=0.6, height_in=0.6),
    )


def _text(name: str, left: float, top: float, *, role=TextRole.BODY,
          width: float = 3.0, height: float = 0.5) -> ShapeProfile:
    return ShapeProfile(
        shape_id=abs(hash(name)) % 9999,
        name=name,
        shape_type="TEXT_BOX (17)",
        geometry=Geometry(left_in=left, top_in=top, width_in=width, height_in=height),
        role=role,
        text="copy",
        paragraphs=[ParagraphProfile(text="copy", runs=[RunProfile(text="copy")])],
    )


def _ctx(slide: SlideProfile, *, layouts=None, margins=None) -> RuleContext:
    master = DeckProfile(
        path="master.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
        layouts=layouts or [],
    )
    guidelines = BrandGuidelines(safe_margins=margins or Margins())
    deck = DeckProfile(
        path="messy.pptx", width_in=CANVAS_W, height_in=CANVAS_H, slides=[slide]
    )
    return RuleContext(deck=deck, spec=derive_master_spec(master, guidelines))


# --------------------------------------------------------------------------- #
# One of a set out of line
# --------------------------------------------------------------------------- #

def test_one_badge_out_of_line_with_its_set_is_reported() -> None:
    """The defect a designer circled: five identical badges, one adrift.

    No other rule sees it. `space.alignment_grid` needs four shapes on a
    rounded edge before it believes in a grid at all, and then only reports a
    miss within four tolerances of one.
    """
    slide = SlideProfile(
        number=1,
        shapes=[_badge(f"Badge {n}", 2.0, 1.0 + n) for n in range(4)]
        + [_badge("Badge 4", 2.19, 5.0)],
    )

    issues = list(RepeatedElementRule().check(_ctx(slide)))

    assert len(issues) == 1
    assert issues[0].shape == "Badge 4"
    assert "0.19in off the left edge" in issues[0].message


def test_a_set_that_lines_up_is_not_reported() -> None:
    slide = SlideProfile(
        number=1,
        shapes=[_badge(f"Badge {n}", 2.0, 1.0 + n) for n in range(5)],
    )

    assert list(RepeatedElementRule().check(_ctx(slide))) == []


def test_a_shape_placed_elsewhere_is_not_called_drift() -> None:
    """Six inches from the set is a position, not a misalignment.

    Without a ceiling this reported every deliberate two-column layout as a
    row with one member out of line.
    """
    slide = SlideProfile(
        number=1,
        shapes=[_badge(f"Badge {n}", 2.0, 1.0 + n) for n in range(4)]
        + [_badge("Far", 8.63, 5.0)],
    )

    assert list(RepeatedElementRule().check(_ctx(slide))) == []


def test_three_shapes_in_two_places_is_not_a_series() -> None:
    """Two of three agreeing is not a majority worth calling an intent."""
    slide = SlideProfile(
        number=1,
        shapes=[_badge("A", 2.0, 1.0), _badge("B", 2.0, 2.0), _badge("C", 2.4, 3.0)],
    )

    assert list(RepeatedElementRule().check(_ctx(slide))) == []


def test_shapes_of_different_sizes_are_not_one_series() -> None:
    """A series is one thing repeated, not everything that happens to be near."""
    odd = _badge("Odd", 2.4, 4.0)
    odd.geometry = Geometry(left_in=2.4, top_in=4.0, width_in=1.9, height_in=0.6)
    slide = SlideProfile(
        number=1,
        shapes=[_badge(f"Badge {n}", 2.0, 1.0 + n) for n in range(3)] + [odd],
    )

    assert list(RepeatedElementRule().check(_ctx(slide))) == []


# --------------------------------------------------------------------------- #
# Margins without a brand file
# --------------------------------------------------------------------------- #

def _master_layout() -> LayoutProfile:
    return LayoutProfile(
        name="Content",
        index=0,
        shapes=[
            ShapeProfile(
                shape_id=2, name="Title", shape_type="PLACEHOLDER (14)",
                placeholder_type="TITLE (1)",
                geometry=Geometry(left_in=0.5, top_in=0.4, width_in=12.33, height_in=1.0),
            ),
            ShapeProfile(
                shape_id=3, name="Body", shape_type="PLACEHOLDER (14)",
                placeholder_type="OBJECT (7)",
                geometry=Geometry(left_in=0.5, top_in=1.6, width_in=12.33, height_in=5.2),
            ),
            # Footer chrome sits at the very bottom edge by design and must not
            # drag the frame out with it.
            ShapeProfile(
                shape_id=4, name="Footer", shape_type="PLACEHOLDER (14)",
                placeholder_type="FOOTER (15)",
                geometry=Geometry(left_in=0.5, top_in=7.2, width_in=4.0, height_in=0.25),
            ),
        ],
    )


def test_margins_come_from_the_master_when_no_brand_file_says() -> None:
    """The check used to need a brand file, so on a master-only run -- which is
    most runs -- it never ran at all."""
    spec = derive_master_spec(
        DeckProfile(path="m.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
                    layouts=[_master_layout()]),
        BrandGuidelines(),
    )

    assert spec.safe_margins.left_in == 0.5
    assert spec.safe_margins.top_in == 0.4
    assert round(spec.safe_margins.right_in, 2) == 0.5
    # 7.5 - (1.6 + 5.2) = 0.7, from the body: the footer is excluded.
    assert round(spec.safe_margins.bottom_in, 2) == 0.7


def test_an_authored_margin_still_wins() -> None:
    spec = derive_master_spec(
        DeckProfile(path="m.pptx", width_in=CANVAS_W, height_in=CANVAS_H,
                    layouts=[_master_layout()]),
        BrandGuidelines(safe_margins=Margins(left_in=1.25)),
    )

    assert spec.safe_margins.left_in == 1.25      # stated
    assert spec.safe_margins.top_in == 0.4        # observed


def test_content_outside_the_derived_frame_is_reported() -> None:
    slide = SlideProfile(number=1, shapes=[_text("Stray", left=0.1, top=3.0)])
    ctx = _ctx(slide, layouts=[_master_layout()])

    issues = list(SafeMarginRule().check(ctx))

    assert len(issues) == 1
    assert "left" in issues[0].message


def test_a_footer_in_the_margin_is_left_alone() -> None:
    """Footers live in the margin by design, and holding them to the content
    frame reported the furniture on every slide of every deck."""
    slide = SlideProfile(
        number=1,
        shapes=[_text("Page number", left=0.5, top=7.25, role=TextRole.FOOTER)],
    )

    assert list(SafeMarginRule().check(_ctx(slide, layouts=[_master_layout()]))) == []


def test_a_master_with_no_layouts_leaves_the_margin_unchecked() -> None:
    """No frame anywhere is still a reason to stay silent, not to invent one."""
    slide = SlideProfile(number=1, shapes=[_text("Stray", left=0.0, top=3.0)])

    assert list(SafeMarginRule().check(_ctx(slide))) == []


def test_furniture_wholly_in_the_margin_strip_is_left_alone() -> None:
    """A date line drawn in the footer strip was put there on purpose.

    Without this the rule flags the footer row on every slide, and the fixer
    then shoves those shapes up into the real footer -- trading a margin
    finding for a collision. Six of those appeared on a real deck.
    """
    # Content band bottom is 7.5 - 0.7 = 6.8in; this sits wholly below it.
    slide = SlideProfile(
        number=1,
        shapes=[_text("Date line", left=0.5, top=7.23, height=0.14)],
    )

    assert list(SafeMarginRule().check(_ctx(slide, layouts=[_master_layout()]))) == []


def test_content_that_spills_out_of_the_band_is_still_reported() -> None:
    """Overflow starts inside the usable area and runs past its edge."""
    slide = SlideProfile(
        number=1,
        shapes=[_text("Body", left=0.5, top=6.6, height=0.5)],
    )

    issues = list(SafeMarginRule().check(_ctx(slide, layouts=[_master_layout()])))

    assert len(issues) == 1
    assert "bottom" in issues[0].message
