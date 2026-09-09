"""Which palette entry a colour should become, which is not which is nearest.

Two questions were one function, and that is how a red came to be recoloured
to an orange 33 delta-E away and a page of blue headings came out grey.
"Which entry is nearest" decides whether a colour is off-palette and always
has an answer. "Which entry was it meant to be" is allowed to have none: a
palette holding no red has no answer for a red, and saying so is the correct
answer rather than a failure to produce one.

The metric moved too. CIE76 is a plain Euclidean distance in Lab and
overstates distance in the blues, so a brand blue measured further from every
other blue than it really is and the nearest entry to it came out a neutral.
"""

from __future__ import annotations

import pytest

from formatting_tool.colorutil import (
    HUE_TOLERANCE,
    NEUTRAL_CHROMA,
    _ciede2000,
    chroma_of,
    delta_e,
    hue_of,
    intended_palette_entry,
    nearest_palette_entry,
)

# The Office theme palette a run gets when no brand file is supplied: a few
# chromatic entries and four neutrals. The neutrals are the hazard.
PALETTE = {
    "theme:dk1": "000000",
    "theme:lt1": "FFFFFF",
    "theme:dk2": "0E2841",
    "theme:lt2": "E8E8E8",
    "theme:accent1": "156082",
    "theme:accent2": "E97132",
    "theme:accent4": "0F9ED5",
    "theme:accent6": "4EA72E",
}
GATE = 12.0


# --------------------------------------------------------------------------- #
# The metric
# --------------------------------------------------------------------------- #

# Sharma, Wu and Dalal's reference pairs. They exist because the hue-angle
# wrap-arounds in CIEDE2000 are easy to get subtly wrong and hard to notice.
SHARMA = [
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
    ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
    ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
    ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
]


@pytest.mark.parametrize("lab_a,lab_b,expected", SHARMA)
def test_ciede2000_matches_the_reference_pairs(lab_a, lab_b, expected) -> None:
    assert _ciede2000(lab_a, lab_b) == pytest.approx(expected, abs=0.0002)


def test_a_colour_is_zero_from_itself() -> None:
    assert delta_e("156082", "#156082") == pytest.approx(0.0)


def test_an_unreadable_colour_measures_nothing() -> None:
    assert delta_e("not a colour", "156082") is None


# --------------------------------------------------------------------------- #
# Neutral and hue
# --------------------------------------------------------------------------- #

def test_greys_blacks_and_whites_read_as_neutral() -> None:
    for value in ("000000", "FFFFFF", "808080", "464646", "E8E8E8"):
        assert chroma_of(value) < NEUTRAL_CHROMA, value
        assert hue_of(value) is None


def test_a_colour_a_designer_would_name_is_not_neutral() -> None:
    for value in ("156082", "A32020", "4EA72E", "0070C0"):
        assert chroma_of(value) > NEUTRAL_CHROMA, value
        assert hue_of(value) is not None


# --------------------------------------------------------------------------- #
# Nearest still answers everything
# --------------------------------------------------------------------------- #

def test_nearest_always_names_something() -> None:
    """It decides whether a colour is off-palette, which is a question that
    always has an answer even when nothing is close."""
    label, distance = nearest_palette_entry("A32020", PALETTE)

    assert label is not None
    assert distance > GATE          # nothing near, and it says so


# --------------------------------------------------------------------------- #
# Intended is allowed to decline
# --------------------------------------------------------------------------- #

def test_a_close_colour_of_the_same_family_is_the_target() -> None:
    label, distance = intended_palette_entry("1A6688", PALETTE, GATE)

    assert label == "theme:accent1"
    assert distance < GATE


def test_a_red_does_not_become_an_orange() -> None:
    """The palette holds no red. Nearest says accent2 anyway, because nearest
    is still nearest when nothing is near."""
    assert nearest_palette_entry("A32020", PALETTE)[0] is not None
    assert intended_palette_entry("A32020", PALETTE, GATE) == (None, None)


def test_a_blue_heading_does_not_become_a_neutral() -> None:
    """The screenshot this was found in: a page of blue headings and two-tone
    icons came back grey. A desaturated blue and a dark grey sit close in Lab
    and are not the same decision."""
    label, _ = intended_palette_entry("2E4A5C", PALETTE, GATE)

    assert label != "theme:dk1"
    assert label != "theme:dk2" or chroma_of("0E2841") > NEUTRAL_CHROMA


def test_a_neutral_does_not_become_a_colour() -> None:
    """#464646 is a mid grey; theme:dk2 is a navy 16.6 away. Nearest picked
    it, which put a colour where the designer had a grey."""
    assert intended_palette_entry("464646", PALETTE, GATE) == (None, None)


def test_a_neutral_may_become_another_neutral() -> None:
    label, _ = intended_palette_entry("222222", PALETTE, GATE)

    assert label == "theme:dk1"


def test_nothing_qualifies_past_the_limit() -> None:
    """The limit is the distance past which the rule already declined to
    recommend anything. Naming a target it would not recommend, and then
    applying it, was the whole defect."""
    near = intended_palette_entry("1A6688", PALETTE, GATE)
    assert near[0] is not None

    assert intended_palette_entry("1A6688", PALETTE, 0.5) == (None, None)


def test_hue_family_is_bounded() -> None:
    """A red and an orange are about 40 degrees apart on the wheel and are not
    the same decision."""
    assert HUE_TOLERANCE < 40.0


# --------------------------------------------------------------------------- #
# What the rule does with a colour that has no intended entry
# --------------------------------------------------------------------------- #
#
# `intended_palette_entry` declining is not the end of it. The rule falls back
# to the nearest entry so the finding is applicable, and marks it so the
# fallback is visible in the report and in the applied line. The tests above
# pin the primitive's judgement; these pin what is done with it.

def _target_for(value: str, palette=None):
    from formatting_tool.models import RuleTuning, Tolerances
    from formatting_tool.rules.colors import _target

    return _target(
        value, palette if palette is not None else PALETTE,
        Tolerances().color_delta_e, RuleTuning(),
    )


def test_the_default_limit_is_the_gate_these_tests_use() -> None:
    """The tests pass a limit of 12 directly; the rule multiplies a tolerance
    by a factor to get there. If those drift apart, every judgement above is
    being asserted against a gate production does not use."""
    from formatting_tool.models import RuleTuning, Tolerances

    assert Tolerances().color_delta_e * RuleTuning().suggestion_factor == GATE


def test_a_red_falls_back_to_the_nearest_entry_and_says_so() -> None:
    """The palette holds no red, so there is no intended entry and the rule
    snaps it to the nearest anyway. What keeps that honest is the mark: a
    designer reading the finding is told it reads as a different colour."""
    expected, suggestion = _target_for("A32020")

    assert expected.startswith("nearest ")
    assert "#E97132" in expected               # accent2, the nearest
    assert "reads as a different colour" in suggestion


def test_an_intended_entry_is_not_marked_as_a_fallback() -> None:
    """The mark has to mean something, so it is absent when the rule is sure."""
    expected, _ = _target_for("1A6688")

    assert not expected.startswith("nearest ")
    assert "#156082" in expected


def test_a_palette_with_nothing_in_it_still_names_no_colour() -> None:
    """The one case with no target at all: nothing to be nearest to."""
    expected, _ = _target_for("A32020", {})

    assert expected == "brand palette"
