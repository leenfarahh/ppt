"""What the renderer says when the file it was given is not there.

A deck uploaded to the page lives in the system temporary directory, and so
does the directory its PNGs are exported to. Windows Storage Sense and the
managed cleanup tools an IT department installs both delete from there on a
schedule, with no regard for a process that is using it: on one machine every
session directory went while the server was running. PowerPoint answers with a
COM tuple that names neither the file nor the cause.
"""

from __future__ import annotations

from pathlib import Path

from formatting_tool.render import _explain, render_deck


class _Renderer:
    """A renderer that fails the way PowerPoint does."""

    name = "powerpoint"
    available = True

    def __init__(self, error):
        self._error = error

    def render(self, deck, out, slides=None):
        raise self._error


COM_PATH_NOT_FOUND = Exception(
    -2147352567, "Exception occurred.",
    (0, None, None, None, 0, -2147024893), None,
)


def test_a_deck_that_is_gone_is_named_before_powerpoint_is_asked(tmp_path) -> None:
    """One stat call, and the answer is the filename rather than a COM tuple."""
    missing = tmp_path / "formatting-tool-ui-abc" / "deck.pptx"

    images = render_deck(missing, renderer=_Renderer(COM_PATH_NOT_FOUND))

    assert not images
    assert str(missing) in images.reason
    assert "Upload it again" in images.reason


def test_the_com_code_for_a_missing_path_is_read(tmp_path) -> None:
    """The code arrives nested two levels down in the arguments, so it is found
    in the text rather than among them."""
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"a deck")

    reason = _explain(COM_PATH_NOT_FOUND, deck, tmp_path / "gone")

    assert "could not find" in reason
    assert "temporary directory" in reason


def test_any_other_failure_is_still_reported_as_it_arrives(tmp_path) -> None:
    """Only the one case is translated. Inventing an explanation for a failure
    nobody has seen is how a real cause gets hidden behind a guess."""
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"a deck")

    reason = _explain(RuntimeError("PowerPoint is showing a dialog"), deck, tmp_path)

    assert reason == "rendering failed: PowerPoint is showing a dialog"
