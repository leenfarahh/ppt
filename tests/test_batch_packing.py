"""Slides packed into AI calls by size rather than by count.

One slide per call was the rule, for a good reason -- the model's attention
has to cover every shape on every slide in the batch. But a 105-slide deck was
105 calls, and the slides are nothing like each other: measured on a real
deck, 1,590 tokens for a cover and 10,610 for a diagram. Spending a whole
call's latency on the cover buys nothing.

So a call is filled to a token budget. The dense slide still travels nearly
alone, which is where the attention argument bites; the long tail collapses,
which is where it does not. Checked against the API rather than assumed: a
packed call the prose four-chars-a-token rule called 19,840 came back measured
at 44,732, and at that size the model returned no findings at all. Calibrated
to two characters a token, the same deck packs into calls that measure what
they claim and the findings come back.
"""

from __future__ import annotations

import pytest

from formatting_tool.ai.payload import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_BATCH_TOKENS,
    build_batches,
)
from formatting_tool.models import (
    Category,
    DeckProfile,
    Geometry,
    Issue,
    ParagraphProfile,
    RunProfile,
    Severity,
    ShapeProfile,
    SlideProfile,
    Source,
)


def _shape(index: int, words: str) -> ShapeProfile:
    shape = ShapeProfile(
        shape_id=index,
        name=f"TextBox {index}",
        shape_type="TEXT_BOX",
        geometry=Geometry(0.0, 0.0, 2.0, 1.0),
    )
    shape.text = words
    shape.paragraphs = [ParagraphProfile(text=words, runs=[RunProfile(text=words)])]
    return shape


def _slide(number: int, shapes: int, words: int = 6) -> SlideProfile:
    body = " ".join(["wellbeing"] * words)
    return SlideProfile(
        number=number,
        layout_name="Blank",
        shapes=[_shape(i, body) for i in range(shapes)],
    )


def _deck(slides: list[SlideProfile]) -> DeckProfile:
    return DeckProfile(
        path="messy.pptx", width_in=13.333, height_in=7.5, slides=slides
    )


def _calls(deck: DeckProfile, issues=(), **kwargs) -> list[list[int]]:
    batches = build_batches(deck, list(issues), **kwargs)
    return [b["batch"]["slides"] for b in batches]


# --------------------------------------------------------------------------- #
# Packing
# --------------------------------------------------------------------------- #

def test_light_slides_share_a_call() -> None:
    """The long tail: five covers should not cost five calls."""
    deck = _deck([_slide(n, shapes=1) for n in range(1, 6)])

    assert _calls(deck) == [[1, 2, 3, 4, 5]]


def test_a_slide_too_big_for_the_budget_travels_alone() -> None:
    """Which is where the one-slide-per-call reasoning actually applies."""
    deck = _deck([_slide(1, shapes=1), _slide(2, shapes=400), _slide(3, shapes=1)])

    calls = _calls(deck)

    assert [2] in calls
    assert len(calls) == 3


def test_slides_are_never_reordered() -> None:
    """Slides next to each other are about the same thing. Packed by size
    alone the model would be reading four unrelated slides."""
    deck = _deck([_slide(n, shapes=40) for n in range(1, 7)])

    calls = _calls(deck)

    assert [n for call in calls for n in call] == [1, 2, 3, 4, 5, 6]
    for call in calls:
        assert call == sorted(call)


def test_the_ceiling_caps_a_deck_of_tiny_slides() -> None:
    """Past it the attention is spread too thin whatever the payload
    measures."""
    deck = _deck([_slide(n, shapes=1, words=1) for n in range(1, 41)])

    calls = _calls(deck)

    assert all(len(call) <= DEFAULT_BATCH_SIZE for call in calls)


def test_one_per_call_is_still_reachable() -> None:
    """Nothing about the old behaviour is out of reach."""
    deck = _deck([_slide(n, shapes=1) for n in range(1, 6)])

    assert _calls(deck, batch_size=1) == [[1], [2], [3], [4], [5]]


def test_a_smaller_budget_makes_more_calls() -> None:
    deck = _deck([_slide(n, shapes=20) for n in range(1, 7)])

    wide = _calls(deck, budget_tokens=DEFAULT_BATCH_TOKENS)
    narrow = _calls(deck, budget_tokens=DEFAULT_BATCH_TOKENS // 8)

    assert len(narrow) > len(wide)


# --------------------------------------------------------------------------- #
# What each call carries
# --------------------------------------------------------------------------- #

def test_a_slides_findings_travel_with_it() -> None:
    deck = _deck([_slide(1, shapes=1), _slide(2, shapes=1)])
    issues = [
        Issue(
            category=Category.SPACE, severity=Severity.ERROR,
            message="on slide 2", source=Source.RULE,
            rule_id="space.overlap", slide=2, shape="TextBox 0",
        )
    ]

    batch = next(iter(build_batches(deck, issues)))
    refs = [f["rule_id"] for f in batch["rule_findings"]]

    assert batch["batch"]["slides"] == [1, 2]
    assert "space.overlap" in refs


def test_deck_level_findings_go_in_every_call() -> None:
    """They are about the deck, so every call needs them to avoid restating
    one."""
    deck = _deck([_slide(n, shapes=60) for n in range(1, 5)])
    issues = [
        Issue(
            category=Category.COLOR, severity=Severity.ERROR,
            message="the deck's theme", source=Source.RULE,
            rule_id="color.theme_mismatch",
        )
    ]

    batches = list(build_batches(deck, issues))

    assert len(batches) > 1
    for batch in batches:
        assert any(
            f["rule_id"] == "color.theme_mismatch" for f in batch["rule_findings"]
        )


def test_every_call_knows_where_it_sits() -> None:
    deck = _deck([_slide(n, shapes=60) for n in range(1, 5)])

    batches = list(build_batches(deck, []))

    for index, batch in enumerate(batches, start=1):
        assert batch["batch"]["index"] == index
        assert batch["batch"]["of"] == len(batches)


def test_a_deck_of_one_slide_is_one_call() -> None:
    assert _calls(_deck([_slide(1, shapes=1)])) == [[1]]
