"""Where a session's uploads and renders are kept.

The system temporary directory is the right default until something else is
managing it. On one machine every session directory vanished mid-run -- the
uploaded deck, its renders, and the deck written from it -- and PowerPoint
reported it as 0x80070003, which reads like a fault in the renderer. Windows
Storage Sense sweeps that directory on a schedule and so do the remote
management agents an IT department installs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from formatting_tool.web.server import WORKDIR_ENV, _workroot


def test_nothing_set_means_the_system_temporary_directory(monkeypatch) -> None:
    monkeypatch.delenv(WORKDIR_ENV, raising=False)
    assert _workroot() is None


def test_a_directory_that_is_set_is_used_and_made(tmp_path, monkeypatch) -> None:
    wanted = tmp_path / "sessions" / "here"
    monkeypatch.setenv(WORKDIR_ENV, str(wanted))

    root = _workroot()

    assert root == str(wanted)
    assert wanted.is_dir()
    # And it is somewhere mkdtemp will actually put a session.
    made = Path(tempfile.mkdtemp(prefix="formatting-tool-ui-", dir=root))
    assert made.parent == wanted


def test_a_directory_that_cannot_be_made_falls_back(tmp_path, monkeypatch) -> None:
    """A page that runs in the wrong directory is better than one that will not
    start."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv(WORKDIR_ENV, str(blocker / "under" / "a" / "file"))

    assert _workroot() is None


def test_an_empty_setting_is_the_default(monkeypatch) -> None:
    monkeypatch.setenv(WORKDIR_ENV, "   ")
    assert _workroot() is None
