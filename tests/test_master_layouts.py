"""Resolving a planned layout name against the layouts PowerPoint loaded.

The bug these exist for: `Designs.Load` brings ONE design from a template, the
one that template's own slides sit on. A master file with three slide masters
therefore offered three layouts of eight, so the matcher's picks -- read from
every master via python-pptx -- were reported as missing from a master that
plainly contained them. Sixteen of seventeen slides on a real deck came back
unrestyled, and every one of them kept the deck's own design alive, which is
why the messy layouts survived into the output.

The load itself needs desktop PowerPoint and is not testable here. The name
resolution and the fallback are pure and are what decides whether a slide
lands at all.
"""

from __future__ import annotations

from formatting_tool.rebuild.master_apply import (
    _fallback,
    _resolve,
    _undecorated,
)


class _Layout:
    """Stands in for a COM CustomLayout, which is only ever passed through."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<Layout {self.name!r}>"


def _layouts(*names: str) -> dict[str, _Layout]:
    return {name: _Layout(name) for name in names}


def test_an_exact_name_wins() -> None:
    layouts = _layouts("One Column", "Two Columns")
    assert _resolve("One Column", layouts) is layouts["One Column"]


def test_a_name_that_drifted_through_a_round_trip_still_matches() -> None:
    """Case, spacing and punctuation vary between a designer's master and the
    same layout after PowerPoint has had it."""
    layouts = _layouts("Title Only - White")
    assert _resolve("title only white", layouts) is layouts["Title Only - White"]
    assert _resolve("Title_Only_White", layouts) is layouts["Title Only - White"]


def test_powerpoints_duplicate_prefix_is_not_a_different_layout() -> None:
    """Loading a design whose layout name is already taken produces
    '1_One Column'. It is the same layout by every meaning except the string,
    and treating it as missing is what strands a slide."""
    assert _undecorated("1_One Column") == "One Column"
    assert _undecorated("2_CV Layout") == "CV Layout"
    # Not a duplicate marker: the underscore is part of the name.
    assert _undecorated("title_content") == "title_content"
    assert _undecorated("One Column") == "One Column"

    layouts = _layouts("1_One Column")
    assert _resolve("One Column", layouts) is layouts["1_One Column"]

    layouts = _layouts("One Column")
    assert _resolve("1_One Column", layouts) is layouts["One Column"]


def test_a_name_the_master_really_does_not_have_resolves_to_nothing() -> None:
    assert _resolve("Agenda", _layouts("One Column", "Two Columns")) is None


def test_the_fallback_is_where_the_rest_of_the_deck_is_landing() -> None:
    """A slide whose pick is missing goes where its neighbours went, which is
    the closest thing to "where it belongs" knowable without re-matching."""
    layouts = _layouts("One Column", "Two Columns")
    plans = {1: "One Column", 2: "One Column", 3: "Two Columns", 4: "Agenda"}

    assert _fallback(layouts, plans) == "One Column"


def test_the_fallback_ignores_picks_the_master_cannot_satisfy() -> None:
    """'Agenda' is the most planned layout and the one that does not exist.
    Counting it would send every stranded slide to a layout that is missing."""
    layouts = _layouts("One Column")
    plans = {1: "Agenda", 2: "Agenda", 3: "Agenda", 4: "One Column"}

    assert _fallback(layouts, plans) == "One Column"


def test_a_master_sharing_no_name_with_the_plans_still_places_every_slide() -> None:
    """Nothing matched, so nothing is "closest" -- but leaving the slides
    behind keeps the deck's own design alive, which is the outcome the whole
    change exists to prevent."""
    layouts = _layouts("Cover", "title_content")
    plans = {1: "Agenda", 2: "One Column"}

    assert _fallback(layouts, plans) in layouts


def test_a_master_with_no_layouts_at_all_has_no_fallback() -> None:
    assert _fallback({}, {1: "One Column"}) is None
