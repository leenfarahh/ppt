"""Tests for slide and layout classification, and the fit between them.

The cases are the ones that were wrong on a real deck before the thresholds
were calibrated against it: a photo cover that reads structurally as a content
slide, a dense content page whose boxes happen to line up, and a layout called
"Content slide" that collapses to "contents" if its name is stripped of
separators.
"""

from __future__ import annotations

from formatting_tool.classify import (
    SlideKind,
    classify_layout,
    classify_slide,
    fit_slide,
    layout_coverage,
    missing_kinds,
)
from formatting_tool.models import (
    DeckProfile,
    Geometry,
    LayoutProfile,
    ParagraphProfile,
    RunProfile,
    ShapeProfile,
    SlideProfile,
)
from formatting_tool.rebuild.matcher import structure_score

CANVAS_W, CANVAS_H = 13.333, 7.5


def _shape(
    name: str,
    *,
    ph: str | None = None,
    left: float = 1.0,
    top: float = 2.0,
    width: float = 4.0,
    height: float = 1.0,
    text: str = "",
    picture: bool = False,
) -> ShapeProfile:
    paragraphs = (
        [ParagraphProfile(text=text, runs=[RunProfile(text=text)])] if text else []
    )
    return ShapeProfile(
        shape_id=abs(hash(name)) % 9999,
        name=name,
        shape_type="PICTURE (13)" if picture else "TEXT_BOX (17)",
        geometry=Geometry(left_in=left, top_in=top, width_in=width, height_in=height),
        placeholder_type=ph,
        text=text,
        paragraphs=paragraphs,
        is_picture=picture,
    )


def _deck(*slides: SlideProfile) -> DeckProfile:
    return DeckProfile(
        path="messy.pptx", width_in=CANVAS_W, height_in=CANVAS_H, slides=list(slides)
    )


def _kind(slide: SlideProfile, deck: DeckProfile | None = None) -> SlideKind:
    deck = deck or _deck(slide)
    return classify_slide(slide, deck).kind


# --------------------------------------------------------------------------- #
# Slides
# --------------------------------------------------------------------------- #

def test_a_photo_cover_is_a_cover_not_a_content_slide() -> None:
    """The case structure alone gets wrong.

    A full-bleed image with a few floating text boxes counts as three content
    regions, which is exactly what a three-region content layout offers. The
    picture is the signal that it is not one.
    """
    slide = SlideProfile(
        number=1,
        layout_name="Title Slide - Photo Dark",
        shapes=[
            _shape("Photo", left=0, top=0, width=CANVAS_W, height=CANVAS_H,
                   picture=True),
            _shape("Title 2", text="Strategy review"),
            _shape("Client 3", text="For a client"),
            _shape("Date 4", text="January 2026"),
        ],
    )

    assert _kind(slide) is SlideKind.COVER


def test_a_dense_page_whose_boxes_line_up_is_not_columns() -> None:
    """Any dense slide has some row of boxes that happens to align.

    Without a ceiling on blocks and copy, most of a real content deck reads as
    columns, which then sends those slides to a two-column layout.
    """
    body = [
        _shape(f"Box {n}", left=0.5 + (n % 3) * 4.0, top=2.0 + (n // 3) * 1.2,
               text="a long paragraph of body copy " * 6)
        for n in range(12)
    ]
    slide = SlideProfile(
        number=4,
        shapes=[_shape("Title 1", ph="TITLE (1)", text="A finding")] + body,
    )

    assert _kind(slide) is SlideKind.CONTENT


def test_parallel_blocks_are_columns() -> None:
    slide = SlideProfile(
        number=2,
        shapes=[
            _shape("Title 1", ph="TITLE (1)", text="Comparing methods"),
            _shape("Left head", left=0.9, top=2.0, text="Method 1"),
            _shape("Left body", left=0.9, top=3.0, text="one two three"),
            _shape("Right head", left=7.0, top=2.0, text="Method 2"),
            _shape("Right body", left=7.0, top=3.0, text="four five six"),
        ],
    )

    assert _kind(slide) is SlideKind.COLUMNS


def test_a_title_that_names_the_job_wins() -> None:
    slide = SlideProfile(
        number=3,
        shapes=[
            _shape("Title 1", ph="TITLE (1)", text="Table of contents"),
            _shape("Item 1", top=2.0, text="Introduction"),
            _shape("Item 2", top=3.0, text="Findings"),
        ],
    )

    assert _kind(slide) is SlideKind.AGENDA


def test_arabic_titles_are_classified_too() -> None:
    """Prezlab decks are bilingual; the Arabic wording carries the same weight."""
    slide = SlideProfile(
        number=2,
        shapes=[_shape("Title 1", ph="TITLE (1)", text="المحتويات")],
    )

    assert _kind(slide) is SlideKind.AGENDA


def test_a_keyword_on_a_dense_slide_is_not_trusted() -> None:
    """A content slide arguing about the agenda is not an agenda slide."""
    slide = SlideProfile(
        number=6,
        shapes=[
            _shape("Title 1", ph="TITLE (1)", text="Overview of the market"),
            _shape("Body", text="detailed argument " * 80),
        ],
    )

    assert _kind(slide) is not SlideKind.AGENDA


def test_a_slide_with_no_text_is_unknown_not_a_section() -> None:
    """A divider carries a label. A blank slide is a blank slide."""
    slide = SlideProfile(
        number=9,
        shapes=[_shape("Rectangle 4"), _shape("Rectangle 8")],
    )

    assert _kind(slide) is SlideKind.UNKNOWN


def test_a_sparse_labelled_slide_is_a_section() -> None:
    slide = SlideProfile(
        number=5,
        shapes=[_shape("Title 1", ph="TITLE (1)", text="Part two: delivery")],
    )

    assert _kind(slide) is SlideKind.SECTION


# --------------------------------------------------------------------------- #
# Layouts
# --------------------------------------------------------------------------- #

def test_a_content_layout_is_not_read_as_an_agenda_layout() -> None:
    """"13_Content slide" strips to "contentslide", which contains "contents".

    Matching raw substrings turned every content layout in a real master into
    an agenda layout, and then every content slide had nowhere to go.
    """
    layout = LayoutProfile(name="13_Content slide _ VCS_to use", index=0)

    assert classify_layout(layout).kind is SlideKind.CONTENT


def test_layout_names_are_read_through_camel_case_and_separators() -> None:
    for name, expected in [
        ("TitleSlide", SlideKind.COVER),
        ("Cover", SlideKind.COVER),
        ("Section Divider", SlideKind.SECTION),
        ("title_two_columns", SlideKind.COLUMNS),
        ("Thank You", SlideKind.CLOSING),
        ("Agenda", SlideKind.AGENDA),
    ]:
        assert classify_layout(LayoutProfile(name=name, index=0)).kind is expected, name


def test_an_unnamed_layout_falls_back_to_its_placeholders() -> None:
    layout = LayoutProfile(
        name="Layout 7",
        index=0,
        shapes=[
            _shape("Title", ph="TITLE (1)"),
            _shape("Left", ph="OBJECT (7)"),
            _shape("Right", ph="OBJECT (7)"),
        ],
    )

    assert classify_layout(layout).kind is SlideKind.COLUMNS


# --------------------------------------------------------------------------- #
# Fitting
# --------------------------------------------------------------------------- #

def _cover_slide() -> SlideProfile:
    return SlideProfile(
        number=1,
        shapes=[
            _shape("Photo", left=0, top=0, width=CANVAS_W, height=CANVAS_H,
                   picture=True),
            _shape("Title 2", text="Strategy review"),
        ],
    )


def test_a_cover_goes_to_the_cover_layout_over_a_better_structural_match() -> None:
    """Purpose beats structure for the kinds structure cannot express."""
    layouts = [
        LayoutProfile(
            name="Content",
            index=0,
            shapes=[_shape("Title", ph="TITLE (1)"), _shape("Body", ph="OBJECT (7)")],
        ),
        LayoutProfile(
            name="Cover",
            index=1,
            shapes=[
                _shape("Title", ph="CENTER_TITLE (3)"),
                _shape("Sub", ph="SUBTITLE (4)"),
            ],
        ),
    ]
    slide = _cover_slide()

    fit = fit_slide(slide, _deck(slide), layouts,
                    score_structure=structure_score, floor=0.5)

    assert fit.kind is SlideKind.COVER
    assert fit.layout_name == "Cover"
    assert fit.fit == "good"


def test_a_missing_cover_layout_is_reported_as_a_gap_not_a_loose_fit() -> None:
    """A cover has no structural counterpart, so approximating it hides the
    thing worth knowing: the master cannot make this slide."""
    layouts = [
        LayoutProfile(
            name="Content",
            index=0,
            shapes=[_shape("Title", ph="TITLE (1)"), _shape("Body", ph="OBJECT (7)")],
        )
    ]
    slide = _cover_slide()

    fit = fit_slide(slide, _deck(slide), layouts,
                    score_structure=structure_score, floor=0.5)

    assert fit.fit == "none"
    assert fit.layout_name == "Content"      # never dropped, only flagged
    assert "no cover layout" in fit.basis
    assert missing_kinds([fit], layouts) == [SlideKind.COVER]


def test_an_overloaded_content_slide_fits_loosely_not_never() -> None:
    """The right kind of layout with the wrong number of regions is a fit to
    adjust, not a layout that does not exist. The two need different fixes."""
    layouts = [
        LayoutProfile(
            name="Content slide",
            index=0,
            shapes=[_shape("Title", ph="TITLE (1)"), _shape("Body", ph="OBJECT (7)")],
        )
    ]
    slide = SlideProfile(
        number=5,
        shapes=[_shape("Title 1", ph="TITLE (1)", text="A finding")]
        + [
            _shape(f"Box {n}", left=0.5 + (n % 3) * 4.0, top=2.0 + (n // 3) * 1.2,
                   text="body copy " * 20)
            for n in range(12)
        ],
    )

    fit = fit_slide(slide, _deck(slide), layouts,
                    score_structure=structure_score, floor=0.5)

    assert fit.kind is SlideKind.CONTENT
    assert fit.fit == "loose"
    assert missing_kinds([fit], layouts) == []


def test_columns_with_no_columns_layout_is_not_a_gap() -> None:
    """A content layout will hold a columns slide awkwardly. Reporting it as a
    missing layout over-claims; only the distinctive kinds are real gaps."""
    layouts = [
        LayoutProfile(
            name="Content slide",
            index=0,
            shapes=[_shape("Title", ph="TITLE (1)"), _shape("Body", ph="OBJECT (7)")],
        )
    ]
    slide = SlideProfile(
        number=2,
        shapes=[
            _shape("Title 1", ph="TITLE (1)", text="Comparing methods"),
            _shape("Left", left=0.9, top=2.0, text="Method 1"),
            _shape("Right", left=7.0, top=2.0, text="Method 2"),
        ],
    )

    fit = fit_slide(slide, _deck(slide), layouts,
                    score_structure=structure_score, floor=0.5)

    assert fit.kind is SlideKind.COLUMNS
    assert missing_kinds([fit], layouts) == []


def test_a_master_with_no_layouts_fits_nothing() -> None:
    slide = _cover_slide()

    fit = fit_slide(slide, _deck(slide), [], score_structure=structure_score,
                    floor=0.5)

    assert fit.layout is None
    assert fit.fit == "none"


def test_layout_coverage_names_what_the_master_can_serve() -> None:
    layouts = [
        LayoutProfile(name="Cover", index=0),
        LayoutProfile(name="Agenda", index=1),
        LayoutProfile(name="Content slide", index=2),
    ]

    coverage = layout_coverage(layouts)

    assert coverage[SlideKind.COVER] == ["Cover"]
    assert coverage[SlideKind.AGENDA] == ["Agenda"]
    assert coverage[SlideKind.CONTENT] == ["Content slide"]
