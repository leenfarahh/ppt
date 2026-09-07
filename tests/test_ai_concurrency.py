"""Reviewing several batches at once.

The calls are independent -- one slide each, with its own findings -- and each
spends its time waiting on the model. Run one after another they were 74% of a
run's wall clock: five slides at sixty seconds, five minutes of a seven-minute
run with nothing happening locally.

Nothing about a call changes, so nothing about the answers may change. These
pin the two things that could: that the report comes out in the same order
however the calls finish, and that one batch failing costs only that batch.
"""

from __future__ import annotations

import threading
import time

from formatting_tool.ai.client import AIResult
from formatting_tool.ai.gemini import AIValidationError, Exhausted
from formatting_tool.models import Category, DeckProfile, Issue, Severity, Source
from formatting_tool.pipeline import RunConfig, _review_batches
from formatting_tool.render import SlideImages


class _Exhausted:
    """Every call refused because the account is out, counting the attempts."""

    def __init__(self):
        self.attempts = 0
        self._lock = threading.Lock()

    def validate_batch(self, payload, deck_name, images):
        with self._lock:
            self.attempts += 1
        raise Exhausted("monthly spending cap reached")


class _Validator:
    """Answers with the slide number it was given, after a wait.

    The waits are reversed on purpose: the last batch finishes first, so a
    caller that yielded results as they arrived would produce them backwards.
    """

    def __init__(self, delays=None, fail_on=()):
        self.delays = delays or {}
        self.fail_on = set(fail_on)
        self.concurrent = 0
        self.peak = 0
        self._lock = threading.Lock()

    def validate_batch(self, payload, deck_name, images):
        number = payload["batch"]["slides"][0]
        with self._lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        try:
            time.sleep(self.delays.get(number, 0.0))
            if number in self.fail_on:
                raise AIValidationError("rate limited")
            return AIResult(issues=[_issue(number)], summaries=[f"slide {number}"])
        finally:
            with self._lock:
                self.concurrent -= 1


def _issue(number: int) -> Issue:
    return Issue(
        category=Category.SPACE, severity=Severity.WARNING,
        message=f"finding on slide {number}", source=Source.AI, slide=number,
    )


def _payloads(count: int) -> list[dict]:
    return [{"batch": {"slides": [n]}} for n in range(1, count + 1)]


def _config(concurrency: int) -> RunConfig:
    return RunConfig(master="m.pptx", decks=["d.pptx"], ai_concurrency=concurrency)


DECK = DeckProfile(path="d.pptx", width_in=13.333, height_in=7.5)
NO_IMAGES = SlideImages(renderer="none")


def test_results_come_back_in_batch_order_however_they_finish() -> None:
    """Slide 5 answers first here. A report whose findings reorder themselves
    run to run is a report nobody can diff."""
    validator = _Validator(delays={1: 0.20, 2: 0.15, 3: 0.10, 4: 0.05, 5: 0.0})

    got = list(_review_batches(validator, DECK, _payloads(5), NO_IMAGES, _config(5)))

    assert [index for index, _part in got] == [0, 1, 2, 3, 4]
    assert [part.issues[0].slide for _index, part in got] == [1, 2, 3, 4, 5]


def test_the_calls_really_do_overlap() -> None:
    validator = _Validator(delays={n: 0.15 for n in range(1, 6)})

    list(_review_batches(validator, DECK, _payloads(5), NO_IMAGES, _config(4)))

    assert validator.peak > 1


def test_concurrency_is_bounded() -> None:
    """The ceiling is what keeps a long deck from opening ninety sockets and
    running into the model's rate limit, which turns a speed-up into waiting."""
    validator = _Validator(delays={n: 0.10 for n in range(1, 13)})

    list(_review_batches(validator, DECK, _payloads(12), NO_IMAGES, _config(3)))

    assert validator.peak <= 3


def test_one_batch_failing_costs_only_that_batch() -> None:
    """It used to stop the whole layer, which made sense when the calls ran in
    order. Running several at once, the rest are already in flight and throwing
    their answers away would lose work for nothing."""
    validator = _Validator(fail_on={3})

    got = list(_review_batches(validator, DECK, _payloads(5), NO_IMAGES, _config(5)))

    assert [part.issues[0].slide for _index, part in got] == [1, 2, 4, 5]


def test_a_single_worker_still_works() -> None:
    """The sequential path is kept, and has to behave the same."""
    validator = _Validator(fail_on={2})

    got = list(_review_batches(validator, DECK, _payloads(4), NO_IMAGES, _config(1)))

    # Sequential stops at the first failure, which is the older behaviour and
    # the right one when nothing else is in flight to be wasted.
    assert [part.issues[0].slide for _index, part in got] == [1]
    assert validator.peak == 1


# --------------------------------------------------------------------------- #
# An account that is out
# --------------------------------------------------------------------------- #

def test_a_spent_account_stops_the_layer_instead_of_asking_again() -> None:
    """A spend cap arrives as 429, exactly like a rate limit, and is the
    opposite kind of problem: it clears when somebody raises a cap, not in
    thirty seconds. Treating it as transient meant every slide of a 17-slide
    deck waited 5+15+30+60 seconds before failing anyway.

    Only the calls already in flight may be spent; the rest return at once.
    """
    validator = _Exhausted()

    got = list(_review_batches(validator, DECK, _payloads(17), NO_IMAGES, _config(3)))

    assert got == []
    # Three could not have known. The other fourteen had been told.
    assert validator.attempts <= 3


def test_a_spent_account_stops_the_sequential_path_too() -> None:
    validator = _Exhausted()

    got = list(_review_batches(validator, DECK, _payloads(17), NO_IMAGES, _config(1)))

    assert got == []
    assert validator.attempts == 1
