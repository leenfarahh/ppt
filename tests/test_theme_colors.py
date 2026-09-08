"""Colours the deck reads from a theme rather than carries itself.

These were invisible. `_fill_hex` asked python-pptx for `fore_color.rgb`, which
raises for a scheme colour, so a theme-bound fill came back as None and no
colour rule ever looked at it; the run reader did the same and said so in a
TODO. On a five-slide deck that is 51 shape fills and 35 text runs unmeasured,
about a third of the coloured surface.

It matters because "bound to the theme" is not the same as "correct". A deck
built from another file brings that file's theme with it, so accent1 resolves
to whatever THAT designer chose. One real deck read accent1 as red where the
master defines it as navy, on 55 shapes and runs, and the report was silent.
"""

from __future__ import annotations

import pytest

from formatting_tool.extract.deck_reader import _theme_slot
from formatting_tool.apply import fixer_for
from formatting_tool.apply.fixers import LeaveAlone, _THEME_BOUND
from formatting_tool.models import Category, Issue, Severity, Source


def _issue(rule_id: str, **kwargs) -> Issue:
    issue = Issue(
        category=Category.COLOR,
        severity=Severity.ERROR,
        message=kwargs.pop("message", f"{rule_id} finding"),
        source=Source.RULE,
        rule_id=rule_id,
        deck="messy.pptx",
        **kwargs,
    )
    issue.id = issue.fingerprint()
    return issue


# --------------------------------------------------------------------------- #
# Naming the slot
# --------------------------------------------------------------------------- #

def test_the_two_spellings_of_a_theme_slot_are_folded_together() -> None:
    """python-pptx names the slots after the OOXML enum, the theme part names
    them as they appear in a:clrScheme. The comparison needs one spelling."""
    assert _theme_slot("ACCENT_1 (5)") == "accent1"
    assert _theme_slot("DARK_2") == "dk2"
    assert _theme_slot("LIGHT_1") == "lt1"
    # PowerPoint's own aliases for the same four slots.
    assert _theme_slot("TEXT_1") == "dk1"
    assert _theme_slot("BACKGROUND_2") == "lt2"
    assert _theme_slot("HYPERLINK") == "hlink"


def test_a_slot_nobody_recognises_is_not_invented() -> None:
    assert _theme_slot("MYSTERY_9") is None
    assert _theme_slot(None) is None
    assert _theme_slot("") is None


# --------------------------------------------------------------------------- #
# Who owns the fix
# --------------------------------------------------------------------------- #

def test_a_theme_bound_finding_is_told_apart_from_a_hardcoded_one() -> None:
    assert _THEME_BOUND.search("theme:accent1 #A32020")
    assert _THEME_BOUND.search("theme:dk2 #0E2841")
    # The label on an `expected`, which names a palette entry rather than a
    # slot the shape reads from. Not the same claim, must not match.
    assert not _THEME_BOUND.search("#A32020")
    assert not _THEME_BOUND.search("brand navy #1D252D")
    assert not _THEME_BOUND.search("")


@pytest.mark.parametrize(
    "rule_id", ["color.text.off_palette", "color.shape.off_palette"]
)
def test_a_colour_the_shape_does_not_own_is_not_written_over(rule_id: str) -> None:
    """Hardcoding the master's colour onto the shape would hide the real
    defect -- the deck's theme -- leave every other shape reading the same
    slot untouched, and survive a later rebuild as an exception to a palette
    that had since been corrected."""
    issue = _issue(
        rule_id, slide=1, shape="Rectangle 1", shape_id=7,
        expected="theme:accent1 #156082",
        found="theme:accent1 #A32020",
    )
    fixer = fixer_for(issue)
    assert fixer is not None

    with pytest.raises(LeaveAlone) as raised:
        fixer(object(), issue, None)
    assert "rebuild" in str(raised.value).lower()


@pytest.mark.parametrize(
    "rule_id", ["color.text.off_palette", "color.shape.off_palette"]
)
def test_a_hardcoded_colour_is_still_fixed(rule_id: str) -> None:
    """The refusal is about theme-bound colours only. A typed-in hex is the
    shape's own and stays correctable."""
    issue = _issue(
        rule_id, slide=1, shape="Rectangle 1", shape_id=7,
        expected="theme:accent1 #156082",
        found="#A32020",
    )
    fixer = fixer_for(issue)
    assert fixer is not None

    # It gets past the theme guard; what it does next needs a real shape, so
    # anything but LeaveAlone is the assertion.
    try:
        fixer(object(), issue, None)
    except LeaveAlone:  # pragma: no cover - the failure this test exists for
        pytest.fail("a hardcoded colour was refused as if it were theme-bound")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #

def test_a_theme_mismatch_is_reported_once_for_the_deck() -> None:
    """One defect, not one per shape. Reported per shape it put 180 identical
    findings on a five-slide deck, which buries every other finding and still
    leaves the designer with one thing to do."""
    from formatting_tool.rules.colors import ThemeMismatchRule

    deck_theme = {"accent1": "A32020", "dk1": "000000"}
    master_theme = {"accent1": "156082", "dk1": "000000"}
    ctx = _context(deck_theme, master_theme, accent_users=40)

    findings = list(ThemeMismatchRule().check(ctx))

    assert len(findings) == 1
    assert findings[0].slide is None and findings[0].shape is None
    assert "accent1" in findings[0].message
    assert findings[0].expected == "theme:accent1 #156082"
    assert findings[0].found == "theme:accent1 #A32020"
    # The scale is the part worth having.
    assert "40" in findings[0].message


def test_a_deck_already_on_the_masters_theme_reports_nothing() -> None:
    from formatting_tool.rules.colors import ThemeMismatchRule

    same = {"accent1": "156082", "dk1": "000000"}
    ctx = _context(same, dict(same), accent_users=40)

    assert list(ThemeMismatchRule().check(ctx)) == []


def test_a_slot_nothing_reads_is_not_reported() -> None:
    """The master and the deck can disagree about accent5 all day; if no shape
    reads it, nothing renders wrong and there is nothing to say."""
    from formatting_tool.rules.colors import ThemeMismatchRule

    ctx = _context(
        {"accent5": "A32020"}, {"accent5": "156082"}, accent_users=0
    )

    assert list(ThemeMismatchRule().check(ctx)) == []


def _context(deck_theme, master_theme, accent_users: int):
    """A RuleContext holding one slide whose shapes read accent1."""
    from formatting_tool.extract.master_spec import derive_master_spec
    from formatting_tool.models import (
        BrandGuidelines,
        DeckProfile,
        Geometry,
        ShapeProfile,
        SlideProfile,
    )
    from formatting_tool.rules.base import RuleContext

    shapes = [
        ShapeProfile(
            shape_id=i,
            name=f"Rectangle {i}",
            shape_type="AUTO_SHAPE",
            geometry=Geometry(0.0, 0.0, 1.0, 1.0),
            fill_theme="accent1",
        )
        for i in range(1, accent_users + 1)
    ]
    deck = DeckProfile(
        path="messy.pptx", width_in=13.333, height_in=7.5,
        slides=[SlideProfile(number=1, layout_name="Blank", shapes=shapes)],
        theme_colors=dict(deck_theme),
    )
    master = DeckProfile(
        path="master.pptx", width_in=13.333, height_in=7.5,
        theme_colors=dict(master_theme),
    )
    return RuleContext(
        deck=deck, spec=derive_master_spec(master, BrandGuidelines()),
    )
