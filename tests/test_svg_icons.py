"""Icon colours, which are not shape colours.

An icon from PowerPoint's own library is a `p:pic`. PowerPoint calls its
colour a Graphics Fill and gives it a ribbon tab of its own, but there is no
`a:solidFill` on the shape: the colour is inside the SVG the picture draws
from. `shape.fill` answers nothing, `fore_color.rgb` raises, and every colour
rule looked straight past every icon in every deck.

Setting `Shape.Fill` on one through PowerPoint automation writes byte-identical
XML -- verified against desktop PowerPoint -- so the only way to recolour an
icon is to rewrite its drawing, which is what these cover.
"""

from __future__ import annotations

from formatting_tool.svgicon import GraphicColor, colors_in, recolor

# A Microsoft icon states its colour twice: once as a class rule naming the
# theme slot it reads, once as a literal on each element for anything drawing
# it that does not apply stylesheets.
THEMED = """<svg xmlns="http://www.w3.org/2000/svg"><style>
.MsftOfcThm_Accent1_Stroke_v2 { stroke:#A32020; }
</style><g>
<path class="MsftOfcThm_Accent1_Stroke_v2" stroke="#A32020" fill="none"/>
<line class="MsftOfcThm_Accent1_Stroke_v2" stroke="#A32020" fill="none"/>
</g></svg>"""

HAND_PICKED = """<svg xmlns="http://www.w3.org/2000/svg"><g>
<path stroke="#C00000" fill="#1f4e79"/>
</g></svg>"""


def test_the_theme_slot_is_read_off_the_class_name() -> None:
    """The difference between an icon that is wrong and one that is showing
    you a theme that is."""
    assert colors_in(THEMED) == [GraphicColor(hex="A32020", theme="accent1")]


def test_a_hand_picked_colour_carries_no_slot() -> None:
    """No class, so nobody said it should follow the theme. It is the icon's
    own colour and wrong on its own terms, which is the one worth a fixer."""
    assert colors_in(HAND_PICKED) == [
        GraphicColor(hex="C00000"),
        GraphicColor(hex="1F4E79"),      # three- and six-digit both fold up
    ]


def test_one_colour_stated_nine_times_is_one_colour() -> None:
    """The style rule and every element repeat it. Reporting each would be
    nine ways of saying the same thing about one icon."""
    assert len(colors_in(THEMED)) == 1


def test_short_hex_is_expanded() -> None:
    assert colors_in('<svg><path fill="#abc"/></svg>') == [
        GraphicColor(hex="AABBCC")
    ]


def test_a_slot_is_paired_with_its_own_colour_not_its_neighbour() -> None:
    """An icon with an accent stroke and a background fill declares two rules
    and two colours; pairing them by position swaps them on any icon that
    happens to declare them the other way round."""
    markup = """<svg><style>
    .MsftOfcThm_Background1_Fill { fill:#FFFFFF; }
    .MsftOfcThm_Accent1_Stroke_v2 { stroke:#A32020; }
    </style><path class="MsftOfcThm_Accent1_Stroke_v2" stroke="#A32020"/></svg>"""

    found = {c.hex: c.theme for c in colors_in(markup)}

    assert found["A32020"] == "accent1"
    assert found["FFFFFF"] == "lt1"


def test_a_recolour_rewrites_the_rule_and_the_literals_together() -> None:
    """Writing one and not the other leaves an icon whose colour depends on
    whether the thing drawing it applies stylesheets -- worse than the defect
    being fixed, because it is inconsistent between viewers."""
    out, changed = recolor(THEMED, "A32020", "156082")

    assert changed == 3                  # one rule, two elements
    assert "A32020" not in out
    assert out.count("#156082") == 3
    assert colors_in(out) == [GraphicColor(hex="156082", theme="accent1")]


def test_a_recolour_leaves_the_other_colours_alone() -> None:
    out, changed = recolor(HAND_PICKED, "C00000", "156082")

    assert changed == 1
    assert "#1f4e79" in out              # the fill was not the target
    assert "#156082" in out


def test_a_colour_the_icon_does_not_draw_with_changes_nothing() -> None:
    out, changed = recolor(THEMED, "00FF00", "156082")

    assert changed == 0
    assert out == THEMED


def test_the_hash_is_optional_on_either_end() -> None:
    a, _ = recolor(HAND_PICKED, "#C00000", "#156082")
    b, _ = recolor(HAND_PICKED, "c00000", "156082")

    assert a == b
