"""Rendering only the slides that are going to be looked at.

The preview used to render the whole deck whichever slides the page was about
to show, and cache the result forever. Both were wrong once undo existed: an
undo writes the corrected deck again, so every image of it is of a file that no
longer exists, and re-rendering a hundred slides to show the one a designer
just changed their mind about is most of what "undo takes a long time" was.

Measured on a real 105-slide deck: the whole deck exports in 10.6s, three
named slides in 2.2s. Hence the share below which slide-by-slide is worth it,
and hence a cache that can be partly missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool import render
from formatting_tool.web import server as web


class _Fake:
    """A renderer that records what it was asked for."""

    name = "fake"
    available = True

    def __init__(self, produces=(1, 2, 3)) -> None:
        self.asked: list = []
        self.produces = produces

    def render(self, deck: Path, out: Path, slides=None) -> dict:
        self.asked.append(slides)
        out.mkdir(parents=True, exist_ok=True)
        made = {}
        for number in (slides or self.produces):
            path = out / f"Slide{number}.PNG"
            path.write_bytes(b"png")
            made[number] = path
        return made


class _Old:
    """A renderer written before slides could be asked for."""

    name = "old"
    available = True

    def __init__(self) -> None:
        self.calls = 0

    def render(self, deck: Path, out: Path) -> dict:
        self.calls += 1
        out.mkdir(parents=True, exist_ok=True)
        path = out / "Slide1.PNG"
        path.write_bytes(b"png")
        return {1: path}


def _a_deck(tmp_path: Path) -> Path:
    """A file on disk to point the renderer at.

    It only has to exist. `render_deck` stats the deck before it drives
    PowerPoint, so that a file removed underneath a run -- which is what a
    managed temp cleanup does to an uploaded deck -- is reported by name
    instead of as a COM tuple.
    """
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"not a real deck; the renderer here is a fake")
    return deck


# --------------------------------------------------------------------------- #
# Asking for slides
# --------------------------------------------------------------------------- #

def test_the_renderer_is_asked_for_the_slides_wanted(tmp_path: Path) -> None:
    fake = _Fake()

    images = render.render_deck(_a_deck(tmp_path), fake, slides=[4, 9])

    assert fake.asked == [[4, 9]]
    assert sorted(images.images) == [4, 9]
    images.cleanup()


def test_a_renderer_that_cannot_narrow_still_works(tmp_path: Path) -> None:
    """The request is a request. A renderer free to ignore it comes back with
    the whole deck and the caller takes what it needs, so nothing downstream
    has to know which it got."""
    old = _Old()

    images = render.render_deck(_a_deck(tmp_path), old, slides=[4, 9])

    assert old.calls == 1
    assert sorted(images.images) == [1]
    images.cleanup()


# --------------------------------------------------------------------------- #
# Slide by slide, or the whole deck
# --------------------------------------------------------------------------- #

class _Presentation:
    """Enough of a COM presentation to answer "how many slides"."""

    def __init__(self, count: int, fails: bool = False) -> None:
        self.Slides = _Slides(count, fails)


class _Slides:
    def __init__(self, count: int, fails: bool) -> None:
        self.Count = count
        self._fails = fails
        self.exported: list[int] = []

    def __call__(self, number: int):
        return _Slide(self, number)


class _Slide:
    def __init__(self, slides: _Slides, number: int) -> None:
        self._slides = slides
        self._number = number

    def Export(self, path, fmt, width, height) -> None:
        if self._slides._fails:
            raise RuntimeError("can't save ^0 to ^1")
        self._slides.exported.append(self._number)
        Path(path).write_bytes(b"png")


def test_a_few_slides_are_exported_one_at_a_time(tmp_path: Path) -> None:
    presentation = _Presentation(105)

    assert render._export_some(presentation, tmp_path, [5, 9, 40]) is True
    assert presentation.Slides.exported == [5, 9, 40]


def test_most_of_a_deck_is_exported_whole(tmp_path: Path) -> None:
    """Per slide costs about five times per-slide-of-the-whole-deck, so a
    third of the deck one at a time is slower than all of it in one call."""
    presentation = _Presentation(10)

    assert render._export_some(presentation, tmp_path, list(range(1, 6))) is False
    assert presentation.Slides.exported == []


def test_a_failed_per_slide_export_falls_back_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    """Per-slide export is the fragile one -- it fails on some paths with an
    unhelpful "can't save ^0 to ^1" -- so it is the one that falls back. A
    half-written set would otherwise sit beside the full export that follows."""
    presentation = _Presentation(105, fails=True)

    assert render._export_some(presentation, tmp_path, [5, 9]) is False
    assert list(tmp_path.glob("*.PNG")) == []


# --------------------------------------------------------------------------- #
# The cache on disk
# --------------------------------------------------------------------------- #

def test_only_the_missing_slides_are_rendered(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "after"
    directory.mkdir()
    (directory / "4.png").write_bytes(b"already here")
    fake = _Fake()
    monkeypatch.setattr(
        render, "available_renderer", lambda: fake
    )
    monkeypatch.setattr(web, "render_deck", render.render_deck)

    found = web._render_into(_a_deck(tmp_path), directory, [4, 9])

    assert fake.asked == [[9]]                 # 4 was on disk already
    assert sorted(found) == [4, 9]
    assert (directory / "9.png").exists()


def test_nothing_is_rendered_when_every_wanted_slide_is_on_disk(
    tmp_path: Path, monkeypatch
) -> None:
    directory = tmp_path / "after"
    directory.mkdir()
    for number in (4, 9):
        (directory / f"{number}.png").write_bytes(b"png")
    fake = _Fake()
    monkeypatch.setattr(render, "available_renderer", lambda: fake)
    monkeypatch.setattr(web, "render_deck", render.render_deck)

    found = web._render_into(_a_deck(tmp_path), directory, [4, 9])

    assert fake.asked == []
    assert sorted(found) == [4, 9]


def test_a_renderer_that_produces_nothing_keeps_what_was_there(
    tmp_path: Path, monkeypatch
) -> None:
    """A render that fails is not a reason to lose the images already made:
    the page can still show the ones it has."""
    directory = tmp_path / "after"
    directory.mkdir()
    (directory / "4.png").write_bytes(b"png")
    monkeypatch.setattr(render, "available_renderer", lambda: render.NullRenderer())
    monkeypatch.setattr(web, "render_deck", render.render_deck)

    found = web._render_into(_a_deck(tmp_path), directory, [4, 9])

    assert sorted(found) == [4]


# --------------------------------------------------------------------------- #
# Rendering one slide again on purpose
# --------------------------------------------------------------------------- #

def _session(tmp_path: Path, run: int = 3):
    return type("S", (), {"directory": tmp_path, "run": run})()


def test_forcing_a_slide_drops_both_sides_of_its_pair(tmp_path: Path) -> None:
    """A designer asking for a slide again is asking whether the comparison in
    front of them is true, and refreshing half of it answers half of that."""
    session = _session(tmp_path)
    sides = (tmp_path / "before", web._after_dir(session))
    for side in sides:
        side.mkdir(parents=True)
        for number in (1, 2):
            (side / f"{number}.png").write_bytes(b"png")

    web._forget(session, [1])

    for side in sides:
        assert not (side / "1.png").exists()
        assert (side / "2.png").exists()        # untouched


def test_each_run_renders_into_its_own_directory(tmp_path: Path) -> None:
    """Which is what makes a stale picture unreachable rather than merely
    deleted: deletion is allowed to fail, and this is not."""
    assert web._after_dir(_session(tmp_path, run=1))         != web._after_dir(_session(tmp_path, run=2))


def test_the_sweep_leaves_the_current_run_alone(tmp_path: Path) -> None:
    session = _session(tmp_path, run=2)
    for run in (1, 2):
        directory = tmp_path / "after" / str(run)
        directory.mkdir(parents=True)
        (directory / "1.png").write_bytes(b"png")

    web._sweep(session)

    assert (tmp_path / "after" / "2" / "1.png").exists()
    assert not (tmp_path / "after" / "1").exists()


def test_forcing_a_slide_that_was_never_rendered_is_not_an_error(
    tmp_path: Path,
) -> None:
    web._forget(_session(tmp_path), [3])       # nothing on disk at all


# --------------------------------------------------------------------------- #
# A render a person is waiting on
# --------------------------------------------------------------------------- #

def test_a_render_does_not_get_the_batch_timeout(tmp_path: Path, monkeypatch) -> None:
    """Half an hour is what a batch job can afford. A page with somebody
    looking at it needs an error it can show them, and it needs it while they
    are still there."""
    from formatting_tool import powerpoint

    asked = {}

    def fake_run(work, timeout=None):
        asked["timeout"] = timeout
        return None

    monkeypatch.setattr(powerpoint, "run", fake_run)
    monkeypatch.setattr(powerpoint, "available", lambda: True)

    render.PowerPointRenderer().render(tmp_path / "deck.pptx", tmp_path / "out")

    assert asked["timeout"] == render.PowerPointRenderer.TIMEOUT_S
    assert render.PowerPointRenderer.TIMEOUT_S < powerpoint._PowerPointHost.TIMEOUT_S


def test_a_wedged_powerpoint_says_so_rather_than_hanging() -> None:
    """The message reaches a designer, so it names what to look at: PowerPoint
    shows dialogs with no window when it is driven this way, and a dialog
    nobody can see is indistinguishable from a hang."""
    from formatting_tool import powerpoint

    host = powerpoint._PowerPointHost()
    host._start = lambda: None          # no thread, so nothing answers

    with pytest.raises(TimeoutError, match="showing a dialog"):
        host.call(lambda app: None, timeout=0.05)
