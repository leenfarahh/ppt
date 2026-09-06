"""Tests for the layout rules and the rebuild.

The rule tests build profiles in memory, like the rest of the suite. The
rebuild tests need real files, because the whole point of the rebuild is what
python-pptx does to a package, so they skip when it is not installed.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from formatting_tool.models import (
    BrandGuidelines,
    DeckProfile,
    Geometry,
    LayoutProfile,
    MasterSpec,
    ParagraphProfile,
    RunProfile,
    ShapeProfile,
    SlideProfile,
)
from formatting_tool.rebuild.matcher import choose_layout, slide_regions
from formatting_tool.rules import RuleContext, build_master_rules, run_rules
from formatting_tool.rules.layouts import LayoutMissingRule


def _shape(
    name: str,
    *,
    ph: str | None = None,
    idx: int | None = None,
    top: float = 3.0,
    height: float = 1.0,
    text: bool = False,
) -> ShapeProfile:
    return ShapeProfile(
        shape_id=1,
        name=name,
        shape_type="PLACEHOLDER (14)" if ph else "AUTO_SHAPE (1)",
        geometry=Geometry(left_in=1.0, top_in=top, width_in=4.0, height_in=height),
        placeholder_type=ph,
        placeholder_idx=idx,
        paragraphs=[ParagraphProfile(text="x", runs=[RunProfile(text="x")])]
        if text
        else [],
    )


def _spec(*layouts: LayoutProfile) -> MasterSpec:
    return MasterSpec(
        source="master.pptx",
        width_in=13.333,
        height_in=7.5,
        guidelines=BrandGuidelines(),
        layouts=list(layouts),
    )


# --------------------------------------------------------------------------- #
# layout.not_in_master
# --------------------------------------------------------------------------- #

def test_slide_on_a_foreign_layout_is_reported() -> None:
    spec = _spec(LayoutProfile(name="title_content", index=0))
    deck = DeckProfile(
        path="messy.pptx",
        width_in=13.333,
        height_in=7.5,
        slides=[SlideProfile(number=1, layout_name="Title and Content")],
    )

    issues = list(LayoutMissingRule().check(RuleContext(deck=deck, spec=spec)))

    assert len(issues) == 1
    assert issues[0].found == "Title and Content"


def test_layout_name_matching_ignores_case_and_separators() -> None:
    """A rename is not drift. "Title Content" and "title_content" are one layout."""
    spec = _spec(LayoutProfile(name="title_content", index=0))
    deck = DeckProfile(
        path="messy.pptx",
        width_in=13.333,
        height_in=7.5,
        slides=[SlideProfile(number=1, layout_name="Title Content")],
    )

    assert list(LayoutMissingRule().check(RuleContext(deck=deck, spec=spec))) == []


def test_a_master_with_no_layouts_reports_nothing() -> None:
    """With nothing to compare against, silence beats reporting every slide."""
    deck = DeckProfile(
        path="messy.pptx",
        width_in=13.333,
        height_in=7.5,
        slides=[SlideProfile(number=1, layout_name="Whatever")],
    )

    assert list(LayoutMissingRule().check(RuleContext(deck=deck, spec=_spec()))) == []


# --------------------------------------------------------------------------- #
# Header and footer completeness
# --------------------------------------------------------------------------- #

def _complete_layout() -> LayoutProfile:
    return LayoutProfile(
        name="title_content",
        index=0,
        shapes=[
            _shape("Title 1", ph="TITLE (1)", idx=0),
            _shape("Footer", ph="FOOTER (15)", idx=11),
            _shape("Header Rule", top=0.4, height=0.02),
            _shape("Footer Bar", top=7.0, height=0.02),
        ],
    )


def test_a_complete_layout_reports_nothing() -> None:
    spec = _spec(_complete_layout())
    deck = DeckProfile(path="master.pptx", width_in=13.333, height_in=7.5)

    issues = run_rules(RuleContext(deck=deck, spec=spec), build_master_rules())

    assert issues == []


def test_missing_footer_placeholder_is_reported() -> None:
    layout = _complete_layout()
    layout.shapes = [s for s in layout.shapes if s.placeholder_type != "FOOTER (15)"]
    spec = _spec(layout)
    deck = DeckProfile(path="master.pptx", width_in=13.333, height_in=7.5)

    issues = run_rules(RuleContext(deck=deck, spec=spec), build_master_rules())

    assert [i.rule_id for i in issues] == ["layout.header_footer_missing"]
    assert "footer" in issues[0].message


def test_placeholder_and_band_are_reported_separately() -> None:
    """The plumbing and the furniture are different defects with different fixes."""
    spec = _spec(LayoutProfile(name="Cover", index=0, shapes=[]))
    deck = DeckProfile(path="master.pptx", width_in=13.333, height_in=7.5)

    issues = run_rules(RuleContext(deck=deck, spec=spec), build_master_rules())

    assert sorted(i.rule_id for i in issues) == [
        "layout.band_missing",
        "layout.header_footer_missing",
    ]


def test_no_layout_is_exempt_from_the_check() -> None:
    """Covers and dividers are held to the same standard as content layouts."""
    spec = _spec(
        LayoutProfile(name="Cover", index=0, shapes=[]),
        LayoutProfile(name="Section Divider", index=1, shapes=[]),
    )
    deck = DeckProfile(path="master.pptx", width_in=13.333, height_in=7.5)

    issues = run_rules(RuleContext(deck=deck, spec=spec), build_master_rules())
    named = {i.message.split("'")[1] for i in issues}

    assert named == {"Cover", "Section Divider"}


def test_a_band_is_found_by_position_not_only_by_name() -> None:
    """A band drawn as "Rectangle 12" at the top of the canvas is still a band."""
    layout = LayoutProfile(
        name="title_content",
        index=0,
        shapes=[
            _shape("Title 1", ph="TITLE (1)", idx=0),
            _shape("Footer", ph="FOOTER (15)", idx=11),
            _shape("Rectangle 12", top=0.3, height=0.05),
            _shape("Rectangle 14", top=7.1, height=0.05),
        ],
    )
    deck = DeckProfile(path="master.pptx", width_in=13.333, height_in=7.5)

    issues = run_rules(RuleContext(deck=deck, spec=_spec(layout)), build_master_rules())

    assert issues == []


# --------------------------------------------------------------------------- #
# Layout matching
# --------------------------------------------------------------------------- #

def test_a_matching_name_wins_outright() -> None:
    layouts = [
        LayoutProfile(name="Cover", index=0),
        LayoutProfile(name="title_content", index=1),
    ]
    slide = SlideProfile(number=1, layout_name="Title Content")

    match = choose_layout(slide, layouts, floor=0.5)

    assert match.name == "title_content"
    assert match.confident and match.score == 1.0


def test_loose_text_boxes_count_as_content_regions() -> None:
    """The messy deck's copy lives outside placeholders; matching must see it.

    Counting placeholders alone would say this slide wants nothing but a
    title, and put a four-block comparison onto a cover.
    """
    slide = SlideProfile(
        number=1,
        layout_name="Foreign",
        shapes=[
            _shape("Title 1", ph="TITLE (1)", idx=0, text=True),
            _shape("Box A", text=True),
            _shape("Box B", text=True),
        ],
    )

    assert slide_regions(slide) == {"title": 1, "content": 2}


def test_decoration_does_not_inflate_the_region_count() -> None:
    """A row of marks is one visual block, not five demands on the layout."""
    slide = SlideProfile(
        number=1,
        shapes=[
            _shape("Title 1", ph="TITLE (1)", idx=0, text=True),
            _shape("Dot 1"),
            _shape("Dot 2"),
            _shape("Dot 3"),
        ],
    )

    assert slide_regions(slide) == {"title": 1, "content": 1}


def test_region_counts_pick_two_columns_over_one() -> None:
    layouts = [
        LayoutProfile(
            name="title_content",
            index=0,
            shapes=[
                _shape("Title 1", ph="TITLE (1)", idx=0),
                _shape("Content", ph="OBJECT (7)", idx=1),
            ],
        ),
        LayoutProfile(
            name="title_two_columns",
            index=1,
            shapes=[
                _shape("Title 1", ph="TITLE (1)", idx=0),
                _shape("Left", ph="OBJECT (7)", idx=1),
                _shape("Right", ph="OBJECT (7)", idx=2),
            ],
        ),
    ]
    slide = SlideProfile(
        number=1,
        layout_name="Foreign",
        shapes=[
            _shape("Title 1", ph="TITLE (1)", idx=0, text=True),
            _shape("Box A", text=True),
            _shape("Box B", text=True),
        ],
    )

    match = choose_layout(slide, layouts, floor=0.5)

    assert match.name == "title_two_columns"


def test_a_slide_that_fits_nothing_is_still_placed_but_flagged() -> None:
    """Never drop a slide. Flag it and let a designer decide."""
    layouts = [
        LayoutProfile(
            name="title_content",
            index=0,
            shapes=[
                _shape("Title 1", ph="TITLE (1)", idx=0),
                _shape("Content", ph="OBJECT (7)", idx=1),
            ],
        )
    ]
    slide = SlideProfile(
        number=1,
        layout_name="Foreign",
        shapes=[_shape("Title 1", ph="TITLE (1)", idx=0, text=True)]
        + [_shape(f"Oval {n}", text=True) for n in range(10)],
    )

    match = choose_layout(slide, layouts, floor=0.5)

    assert match.name == "title_content"
    assert not match.confident


# --------------------------------------------------------------------------- #
# The rebuild itself
# --------------------------------------------------------------------------- #

def _sample_master(tmp_path: Path):
    """A two-layout master, built with python-pptx's own default template."""
    from pptx import Presentation

    prs = Presentation()
    # The stock template's layout 0 is a title slide and layout 1 is title
    # and content, which is enough structure for the matcher to choose between.
    prs.slides.add_slide(prs.slide_layouts[0])
    path = tmp_path / "master.pptx"
    prs.save(str(path))
    return path


def _messy_deck(tmp_path: Path):
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "A title that drifted"
    # Direct formatting on the title, and a loose box holding the body copy:
    # between them, everything the rebuild is supposed to undo and preserve.
    run = slide.shapes.title.text_frame.paragraphs[0].runs[0]
    run.font.size = Pt(11)
    run.font.bold = True
    slide.shapes.title.left = Inches(4.2)
    slide.shapes.title.top = Inches(5.1)

    box = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(4), Inches(1))
    box.text_frame.text = "Body copy in a loose text box"

    path = tmp_path / "messy.pptx"
    prs.save(str(path))
    return path


def test_rebuild_resets_placeholder_geometry(tmp_path: Path) -> None:
    """The point of rebuilding: the title stops carrying its own position.

    Re-pointing a slide at a layout in place leaves the direct formatting
    intact and changes nothing on screen. Recreating the slide is what makes
    the master's geometry take effect, and a placeholder with no xfrm of its
    own is the proof.
    """
    pytest.importorskip("pptx")
    from pptx import Presentation

    from formatting_tool.rebuild import rebuild

    out = tmp_path / "rebuilt.pptx"
    rebuild(_sample_master(tmp_path), _messy_deck(tmp_path), out)

    xfrm = "{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm"
    slide = Presentation(str(out)).slides[0]
    titles = [s for s in slide.shapes if s.is_placeholder and s.has_text_frame]

    assert titles, "the rebuilt slide kept no placeholder"
    assert titles[0]._element.spPr.find(xfrm) is None
    assert "A title that drifted" in titles[0].text_frame.text


def test_rebuild_keeps_emphasis_and_drops_brand_formatting(tmp_path: Path) -> None:
    """Bold is what the writer meant; 11pt is drift. They are treated apart."""
    pytest.importorskip("pptx")
    from pptx import Presentation

    from formatting_tool.rebuild import rebuild

    out = tmp_path / "rebuilt.pptx"
    rebuild(_sample_master(tmp_path), _messy_deck(tmp_path), out)

    slide = Presentation(str(out)).slides[0]
    title = next(s for s in slide.shapes if s.is_placeholder and s.has_text_frame)
    run = title.text_frame.paragraphs[0].runs[0]

    assert run.font.bold is True
    assert run.font.size is None


def test_rebuild_drops_the_masters_sample_slides(tmp_path: Path) -> None:
    """The master is a template here, not content. Its samples must not ship."""
    pytest.importorskip("pptx")
    from pptx import Presentation

    from formatting_tool.rebuild import rebuild

    out = tmp_path / "rebuilt.pptx"
    result = rebuild(_sample_master(tmp_path), _messy_deck(tmp_path), out)

    assert result.sample_slides_removed == 1
    assert len(Presentation(str(out)).slides._sldIdLst) == 1


def test_rebuild_transplants_loose_shapes(tmp_path: Path) -> None:
    """Loose content was never governed by a layout, so it crosses untouched."""
    pytest.importorskip("pptx")
    from pptx import Presentation

    from formatting_tool.rebuild import rebuild

    out = tmp_path / "rebuilt.pptx"
    result = rebuild(_sample_master(tmp_path), _messy_deck(tmp_path), out)

    assert result.slides[0].transplanted
    assert not result.dropped
    texts = [s.text_frame.text for s in Presentation(str(out)).slides[0].shapes
             if s.has_text_frame]
    assert "Body copy in a loose text box" in texts


def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    import struct, zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_transplanted_images_do_not_collide_with_the_masters(tmp_path: Path) -> None:
    """Two different images must not land on the same partname.

    Relating the source's image part straight into the target package looks
    like it works: the part keeps its own partname, and the master almost
    always has a `ppt/media/image1.png` of its own. Both then write to the same
    entry in the zip and one image silently replaces the other.
    """
    import zipfile
    from collections import Counter

    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    from formatting_tool.rebuild import rebuild

    # The master's image goes on a layout, not a slide, so that it survives the
    # sample slides being dropped and is still occupying ppt/media/image1.png
    # when the deck's image arrives. python-pptx has no add_picture on a
    # layout, so the element is moved there and re-related by hand.
    import copy

    from pptx.oxml.ns import qn

    master = Presentation()
    layout = master.slide_layouts[5]
    staging = master.slides.add_slide(layout)
    master_image = _png(8, 8, (10, 20, 30))
    picture = staging.shapes.add_picture(
        BytesIO(master_image), Inches(1), Inches(1)
    )
    element = copy.deepcopy(picture._element)
    _part, rid = layout.part.get_or_add_image_part(BytesIO(master_image))
    for blip in element.iter(qn("a:blip")):
        blip.set(qn("r:embed"), rid)
    layout.shapes._spTree.append(element)
    master_path = tmp_path / "master.pptx"
    master.save(str(master_path))

    deck_image = _png(8, 8, (200, 30, 60))
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[5]).shapes.add_picture(
        BytesIO(deck_image), Inches(2), Inches(2)
    )
    deck_path = tmp_path / "messy.pptx"
    deck.save(str(deck_path))

    out = tmp_path / "rebuilt.pptx"
    result = rebuild(master_path, deck_path, out)

    names = Counter(zipfile.ZipFile(str(out)).namelist())
    assert not [n for n, c in names.items() if c > 1], "duplicate part in the package"
    assert not result.dropped

    # Both images are present and distinct, and the transplanted one is the
    # deck's own bytes rather than the master's image showing through.
    rebuilt = Presentation(str(out))
    pictures = [
        s for s in rebuilt.slides[0].shapes if "PICTURE" in str(s.shape_type)
    ]
    assert len(pictures) == 1
    assert pictures[0].image.blob == deck_image
    assert len([n for n in names if n.startswith("ppt/media/")]) == 2


def test_rebuild_refuses_a_master_with_no_layouts(tmp_path: Path) -> None:
    pytest.importorskip("pptx")

    from formatting_tool.rebuild import RebuildError, rebuild

    empty = DeckProfile(path="x", width_in=1, height_in=1)
    assert empty.layouts == []

    # A real file with no layouts cannot be built with python-pptx, so this
    # asserts the guard through the profile the builder actually consults.
    from unittest.mock import patch

    with patch("formatting_tool.rebuild.builder.read_deck", return_value=empty):
        with pytest.raises(RebuildError, match="no slide layouts"):
            rebuild(
                _sample_master(tmp_path),
                _messy_deck(tmp_path),
                tmp_path / "out.pptx",
            )
