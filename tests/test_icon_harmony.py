"""Related icons end the palette sweep in one colour scheme.

Off a real slide: two cards, each with an icon. One was drawn in the brand's
navy and green, the other in brown and gold. The sweep put each colour on the
palette on its own, and the pair came out in two schemes -- one of them blue
and a grey that vanished on its card. The icons are related, so after the
sweep the one that needed no correction leads and the other takes its tones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool.apply.palette import sweep_to_palette

PALETTE = {
    "navy": "004F71", "blue": "136596", "green": "4C8C2B",
    "lime": "A4D65E", "grey": "E8E8E8", "white": "FFFFFF", "black": "000000",
}


def _icon(slide, left, top, dark, light, name):
    """A two-tone icon drawn as two touching freeforms, as Google Slides
    exports one."""
    from pptx.dml.color import RGBColor
    from pptx.util import Inches

    shapes = []
    for i, colour in enumerate((dark, light)):
        builder = slide.shapes.build_freeform(Inches(left + i * 0.18), Inches(top))
        builder.add_line_segments([
            (Inches(left + i * 0.18 + 0.17), Inches(top)),
            (Inches(left + i * 0.18 + 0.17), Inches(top + 0.3)),
            (Inches(left + i * 0.18), Inches(top + 0.3)),
        ])
        shape = builder.convert_to_shape()
        shape.name = f"{name} part {i + 1}"
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor.from_string(colour)
        shape.line.fill.background()
        shapes.append(shape)
    return shapes


def _fills(path: Path) -> dict[str, str]:
    from pptx import Presentation

    return {
        s.name: str(s.fill.fore_color.rgb)
        for s in Presentation(str(path)).slides[0].shapes
        if s.fill.type is not None
    }


def _deck(tmp_path: Path, build) -> Path:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    build(prs.slides.add_slide(prs.slide_layouts[6]))
    path = tmp_path / "icons.pptx"
    prs.save(str(path))
    return path


def test_the_off_brand_icon_takes_its_siblings_scheme(tmp_path: Path) -> None:
    deck = _deck(tmp_path, lambda slide: (
        _icon(slide, 4.1, 2.4, "9A5918", "FDCF85", "Card 1 icon"),   # brown, gold
        _icon(slide, 8.7, 2.4, "004F71", "A4D65E", "Card 2 icon"),   # brand
    ))

    result = sweep_to_palette(deck, PALETTE, 5.0)

    assert result.matched_icons == 1
    fills = _fills(deck)
    assert fills["Card 1 icon part 1"] == "004F71"       # dark takes dark
    assert fills["Card 1 icon part 2"] == "A4D65E"       # light takes light
    assert fills["Card 2 icon part 1"] == "004F71"       # the leader is untouched
    assert "matched 1 icon" in result.line()


def test_icons_in_different_rows_are_not_a_set(tmp_path: Path) -> None:
    deck = _deck(tmp_path, lambda slide: (
        _icon(slide, 1.0, 1.0, "9A5918", "FDCF85", "Top icon"),
        _icon(slide, 6.0, 5.5, "004F71", "A4D65E", "Bottom icon"),
    ))

    result = sweep_to_palette(deck, PALETTE, 5.0)

    assert result.matched_icons == 0


def test_legend_swatches_are_not_icons(tmp_path: Path) -> None:
    """A row of small squares in different colours is a key, not a set of
    icons that disagree."""
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    def build(slide):
        for i, colour in enumerate(("004F71", "4C8C2B", "A4D65E")):
            swatch = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1 + i),
                                            Inches(6), Inches(0.2), Inches(0.2))
            swatch.name = f"Swatch {i}"
            swatch.fill.solid()
            swatch.fill.fore_color.rgb = RGBColor.from_string(colour)

    deck = _deck(tmp_path, build)
    sweep_to_palette(deck, PALETTE, 5.0)

    assert _fills(deck) == {
        "Swatch 0": "004F71", "Swatch 1": "4C8C2B", "Swatch 2": "A4D65E",
    }


def test_a_swap_inside_one_svg_does_not_undo_itself() -> None:
    from formatting_tool.svgicon import recolor_many

    markup = '<path fill="#004F71"/><path fill="#A4D65E"/>'
    out, changed = recolor_many(markup, {"004F71": "A4D65E", "A4D65E": "004F71"})

    assert changed == 2
    assert out == '<path fill="#A4D65E"/><path fill="#004F71"/>'
