"""A rendered picture that is gone by the time its batch asks for it.

THE RUN THIS IS FOR. A 29-slide deck rendered fine, the AI layer got sixteen
batches back, and then `Slide20.PNG` was not there any more:

    FileNotFoundError: [WinError 3] The system cannot find the path specified:
    'C:\\...\\Temp\\formatting-tool-render-y0nikrcx\\Slide20.PNG'

The PNGs are exported to the system temporary directory and the batches are
reviewed over several minutes. Windows Storage Sense and the managed cleanup
tools an IT department installs both delete from there on a schedule with no
regard for a process that is using it -- `render._explain` already names that
as the known hazard on this machine. So a file can be listed when the
directory is collected and gone when its turn comes.

What it cost is the point. One missing picture on one slide took down the
whole request: 2,619 findings from pass 1, 227 from pass 2, and the sixteen
batches that had already come back. Two things were wrong and both are tested
here -- the picture read was not tolerant of a file that had gone, and
`_review_one` promised in its own docstring that "a failure costs that batch
and no other" while catching only the two failures somebody had foreseen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool.ai.client import AIConfig, AIValidator
from formatting_tool.models import BrandGuidelines, MasterSpec


def _validator() -> AIValidator:
    return AIValidator(
        MasterSpec(
            source="m.pptx", width_in=13.333, height_in=7.5,
            guidelines=BrandGuidelines(),
        ),
        AIConfig(),
    )


def _payload() -> dict:
    return {"deck": "d.pptx", "batch": {"index": 1, "of": 1, "slides": [20, 21]},
            "rule_findings": []}


# --------------------------------------------------------------------------- #
# The picture
# --------------------------------------------------------------------------- #

def test_a_picture_that_vanished_costs_the_picture_not_the_batch(
    tmp_path: Path,
) -> None:
    """The narrow fix. That slide is reviewed on its geometry, which is what a
    batch with no render has always done."""
    here = tmp_path / "Slide21.PNG"
    here.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    gone = tmp_path / "Slide20.PNG"          # never written

    contents, attached = _validator()._contents(
        _payload(), client=None, images=[(20, gone), (21, here)]
    )

    assert attached == {21}
    assert isinstance(contents, list)
    body = " ".join(p for p in contents if isinstance(p, str))
    assert "Slide 21" in body
    assert "Slide 20" not in body            # not labelled, because none went


def test_the_slide_whose_picture_went_is_not_counted_as_rendered(
    tmp_path: Path,
) -> None:
    """The evidence label is the whole value of the render: a designer weighing
    "the text does not collide" needs to know whether that was seen or
    supposed. `attached` is what feeds that, so it has to be the pictures that
    really went rather than the ones that were meant to."""
    gone = tmp_path / "Slide20.PNG"

    _contents, attached = _validator()._contents(
        _payload(), client=None, images=[(20, gone)]
    )

    assert attached == set()


def test_a_batch_that_lost_every_picture_still_goes_as_text(
    tmp_path: Path,
) -> None:
    contents, attached = _validator()._contents(
        _payload(), client=None,
        images=[(20, tmp_path / "a.PNG"), (21, tmp_path / "b.PNG")],
    )

    assert attached == set()
    assert isinstance(contents, str) and contents        # the payload alone


def test_a_failure_that_is_not_a_missing_file_is_not_swallowed(
    tmp_path: Path, monkeypatch,
) -> None:
    """OSError and not Exception. Swallowing anything else would hide a real
    fault behind a slide that quietly lost its picture."""
    from formatting_tool.ai import client as client_module

    def boom(client, path, mime_type):
        raise ValueError("the SDK changed under us")

    monkeypatch.setattr(client_module, "file_part", boom)
    here = tmp_path / "Slide20.PNG"
    here.write_bytes(b"png")

    with pytest.raises(ValueError):
        _validator()._contents(_payload(), client=None, images=[(20, here)])


# --------------------------------------------------------------------------- #
# The backstop
# --------------------------------------------------------------------------- #

def _images():
    from formatting_tool.render import SlideImages

    return SlideImages(renderer="powerpoint", images={20: Path("gone.PNG")})


def _deck():
    from formatting_tool.models import DeckProfile

    return DeckProfile(path="d.pptx", width_in=13.333, height_in=7.5, slides=[])


def test_one_batch_raising_does_not_take_the_run_down() -> None:
    """The promise `_review_one` already made in its docstring, now kept for
    every failure rather than the two that were foreseen."""
    from formatting_tool.pipeline import _review_one

    class Exploding:
        def validate_batch(self, payload, deck_name, images):
            raise FileNotFoundError(3, "The system cannot find the path")

    assert _review_one(Exploding(), _deck(), _payload(), _images()) is None


def test_the_other_batches_still_come_back(monkeypatch) -> None:
    """What the crash actually cost: sixteen batches were already home and
    were thrown away with the seventeenth."""
    from formatting_tool.ai.client import AIResult
    from formatting_tool.pipeline import RunConfig, _review_batches

    class Flaky:
        def validate_batch(self, payload, deck_name, images):
            if payload["batch"]["index"] == 3:
                raise FileNotFoundError(3, "gone")
            return AIResult(calls=1)

    payloads = [
        {"deck": "d.pptx", "batch": {"index": i, "of": 5, "slides": [i]}}
        for i in range(1, 6)
    ]
    config = RunConfig(master=Path("m.pptx"), decks=[], ai_concurrency=4)

    got = list(_review_batches(Flaky(), _deck(), payloads, _images(), config))

    assert [index for index, _part in got] == [0, 1, 3, 4]   # 2 is the casualty
    assert all(part.calls == 1 for _index, part in got)


def test_it_survives_the_single_threaded_path_too(monkeypatch) -> None:
    """Concurrency 1 is its own branch of `_review_batches`, and a `break`
    there would end the layer rather than skip the batch."""
    from formatting_tool.ai.client import AIResult
    from formatting_tool.pipeline import RunConfig, _review_batches

    class Flaky:
        def validate_batch(self, payload, deck_name, images):
            if payload["batch"]["index"] == 1:
                raise FileNotFoundError(3, "gone")
            return AIResult(calls=1)

    payloads = [
        {"deck": "d.pptx", "batch": {"index": i, "of": 2, "slides": [i]}}
        for i in range(1, 3)
    ]
    config = RunConfig(master=Path("m.pptx"), decks=[], ai_concurrency=1)

    got = list(_review_batches(Flaky(), _deck(), payloads, _images(), config))

    assert [index for index, _part in got] == [1]


def test_a_spent_account_still_stops_the_layer() -> None:
    """The one failure the `break` was right about. Seventeen slides each
    learning separately that the account is out is seventeen copies of one
    message, and with a retry attached it was half an hour of sleeping to
    reach the same report."""
    from formatting_tool.ai.client import AIResult
    from formatting_tool.ai.gemini import Exhausted
    from formatting_tool.pipeline import RunConfig, _review_batches

    asked: list[int] = []

    class Broke:
        def validate_batch(self, payload, deck_name, images):
            asked.append(payload["batch"]["index"])
            raise Exhausted("the free tier is used up")

    payloads = [
        {"deck": "d.pptx", "batch": {"index": i, "of": 5, "slides": [i]}}
        for i in range(1, 6)
    ]
    config = RunConfig(master=Path("m.pptx"), decks=[], ai_concurrency=1)

    assert list(_review_batches(Broke(), _deck(), payloads, _images(), config)) == []
    # Asked once, then not again.
    assert asked == [1]
