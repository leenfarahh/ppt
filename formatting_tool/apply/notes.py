"""Keeping a copy of every production note this run deletes.

`remove_note` deletes the shape, and that is deliberately not what this
module touches. What it adds is the copy: the note's text goes into a real
PowerPoint comment on the slide it came from, anchored where the shape sat, so
the message survives even though the shape does not. A designer opens the
Comments pane and finds "Design - redo the map" waiting; a client opens the
deck and finds nothing.

It runs as one pass over the WRITTEN file, after the applier has finished with
it, for two reasons. A comment is added by the desktop application, which
needs a file on disk rather than the object tree python-pptx is holding; and
the second round re-saves the deck through python-pptx, so a comment written
before that would be written into a file about to be rewritten.

Which means the copy is made after the deletion, not before it, and the
guarantee is weaker than it sounds: if PowerPoint cannot be reached, the shape
is already gone. The note is not lost -- the report quotes it verbatim, which
is where it has always been recoverable from -- but it is not in the deck
either, and the run says so rather than letting that pass unremarked.

The comment is filed under a name rather than a person, so a designer reading
the pane can tell at a glance which notes they wrote and which one a tool
moved for them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

log = logging.getLogger(__name__)

COMMENT_AUTHOR = "Deck check"
COMMENT_INITIALS = "DC"

# What the note is prefixed with, so it reads as a record of something taken
# off the slide rather than as a fresh instruction from nobody.
PREFIX = "Production note, removed from the slide by Deck check:"

_EMU_PER_POINT = 12700


@dataclass(frozen=True)
class NoteCopy:
    """One deleted note, and where its shape sat."""

    slide: int                          # 1-based
    shape: Optional[str]
    text: str
    left_pt: float = 0.0
    top_pt: float = 0.0

    @property
    def body(self) -> str:
        return f"{PREFIX}\n{self.text}"


@dataclass(frozen=True)
class CopyResult:
    note: NoteCopy
    copied: bool
    detail: str


def emu_to_points(value: Optional[int]) -> float:
    return float(value or 0) / _EMU_PER_POINT


def copy_notes_to_comments(deck: Path, notes: Sequence[NoteCopy]) -> list[CopyResult]:
    """Write one PowerPoint comment per deleted note. Never raises.

    A failure here costs the copy, not the run: the fixes are applied and the
    file is written by the time this is called.
    """
    if not notes:
        return []

    from .. import powerpoint      # noqa: PLC0415 - Windows only, lazy

    if not powerpoint.available():
        return [
            CopyResult(
                note, False,
                "no comment was made: desktop PowerPoint is needed for that "
                "and is not available here",
            )
            for note in notes
        ]

    try:
        return powerpoint.run(lambda app: _write(app, deck, notes))
    except Exception as exc:
        log.warning(
            "could not write %d production note(s) into comments on %s",
            len(notes), deck.name, exc_info=True,
        )
        return [
            CopyResult(note, False, f"no comment could be made ({exc})")
            for note in notes
        ]


def _write(app: Any, deck: Path, notes: Sequence[NoteCopy]) -> list[CopyResult]:
    """One session, one save, a comment per note.

    Anchored at the position the shape occupied, which is why that was
    recorded before the deletion: by the time this runs there is nothing on
    the slide left to ask.
    """
    from ..rebuild.master_apply import _retry   # noqa: PLC0415 - shared COM retry
    from .. import powerpoint                   # noqa: PLC0415

    results: list[CopyResult] = []
    presentation = _retry(
        lambda: app.Presentations.Open(str(deck.resolve()), False, False, False)
    )
    try:
        count = int(presentation.Slides.Count)
        for note in notes:
            if not 1 <= note.slide <= count:
                results.append(CopyResult(
                    note, False,
                    f"slide {note.slide} is not in the written deck",
                ))
                continue
            slide = presentation.Slides(note.slide)
            try:
                _retry(lambda s=slide, n=note: s.Comments.Add(
                    n.left_pt, n.top_pt, COMMENT_AUTHOR, COMMENT_INITIALS, n.body
                ))
            except Exception as exc:
                results.append(CopyResult(
                    note, False, f"the comment could not be added ({exc})"
                ))
                continue
            results.append(CopyResult(
                note, True,
                f"its text was copied into a PowerPoint comment on slide "
                f"{note.slide}",
            ))
        _retry(presentation.Save)
    finally:
        powerpoint.quietly(presentation.Close)
    return results
