"""An answer cut off by the output budget must not cost a batch its findings.

Calls are packed to a budget of INPUT tokens and nothing measures the answer.
The answer is as long as the model has things to say, so a batch that is a
comfortable size going out can be cut off coming back -- and a cut-off answer
is not partial, it is unparseable, so every slide in the batch loses its
findings at once. On a real deck that was four slides, reported to the designer
as "the AI layer did not run".
"""

from __future__ import annotations

import pytest

from formatting_tool.ai.gemini import AIValidationError, Truncated
from formatting_tool.ai.payload import split_batch


def _payload(numbers):
    return {
        "deck": "deck.pptx",
        "batch": {"index": 1, "of": 1, "slides": list(numbers)},
        "slide_size_in": [13.333, 7.5],
        "rule_findings": (
            [{"slide": None, "id": "deck-level"}]
            + [{"slide": n, "id": f"f{n}"} for n in numbers]
        ),
        "slides": [{"slide": n} for n in numbers],
    }


def test_truncation_is_its_own_error() -> None:
    """The caller can do something about this one and nothing about the rest:
    the request was fine and the model simply ran out of room."""
    assert issubclass(Truncated, AIValidationError)


def test_a_batch_is_halved_with_each_slide_keeping_its_findings() -> None:
    first, second = split_batch(_payload([9, 10, 11, 12]))

    assert first["batch"]["slides"] == [9, 10]
    assert second["batch"]["slides"] == [11, 12]
    assert [s["slide"] for s in first["slides"]] == [9, 10]
    assert [f["id"] for f in first["rule_findings"] if f["slide"] is not None] \
        == ["f9", "f10"]
    assert [f["id"] for f in second["rule_findings"] if f["slide"] is not None] \
        == ["f11", "f12"]


def test_deck_level_findings_go_to_both_halves() -> None:
    """They name no slide and are the context every call carries, not content
    being divided up."""
    for half in split_batch(_payload([1, 2])):
        assert any(f["slide"] is None for f in half["rule_findings"])


def test_an_odd_batch_splits_without_losing_a_slide() -> None:
    halves = split_batch(_payload([3, 4, 5]))
    covered = [n for half in halves for n in half["batch"]["slides"]]

    assert sorted(covered) == [3, 4, 5]


def test_a_single_slide_cannot_be_split() -> None:
    """A real limit rather than a packing accident, and the caller says so."""
    assert split_batch(_payload([7])) == []
    assert split_batch(_payload([])) == []


def test_everything_else_about_the_payload_survives() -> None:
    half = split_batch(_payload([1, 2]))[0]

    assert half["deck"] == "deck.pptx"
    assert half["slide_size_in"] == [13.333, 7.5]
