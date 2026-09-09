"""Taking a production note off a slide without throwing it away.

A note addressed to whoever is building the deck must not reach the client:
"Design - redo the map", "TBC with legal", "@Sara update these numbers". The
first version of this deleted the shape, which is correct about the slide and
wrong about the note. Somebody wrote it on purpose, it is usually the only
record that the thing it asks for is outstanding, and deletion is the one edit
a designer cannot check by looking at the result -- everything else this tool
does leaves evidence on the slide, and this leaves a gap.

So the note is moved rather than removed, and the order of the two halves is
the whole design: the copy is made FIRST and the shape comes off only once the
copy exists. A failure halfway leaves the note on the slide, which is a defect
somebody notices, rather than nowhere, which is a defect nobody can.

Three destinations, best first:

1. A real PowerPoint comment, through the desktop application's own API. It
   lands in the Comments pane where a designer already looks for work assigned
   to them, anchored at the spot the note occupied, and PowerPoint writes it in
   whatever format its own version uses -- a modern comment on current builds,
   which is a set of parts and author GUIDs not worth hand-rolling.

2. The slide's notes page, when PowerPoint is not there to ask. Off the slide,
   still in the file, still attached to the right slide.

3. Nothing: the shape stays where it is and the report says so. Reached when
   neither of the above worked, and it is a better answer than either losing
   the note or shipping it.

Whichever happened is reported per note, because "moved to a comment" and
"moved to the notes page" are different things for a designer to go and check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

log = logging.getLogger(__name__)

# The author a comment is filed under. Not a person: a designer reading the
# pane has to be able to tell at a glance which notes they wrote and which one
# a tool moved for them.
COMMENT_AUTHOR = "Deck check"
COMMENT_INITIALS = "DC"

# What the note is prefixed with wherever it lands, so it reads as a record of
# something moved rather than as a fresh instruction from nobody.
PREFIX = "Production note, moved off the slide by Deck check:"

_EMU_PER_POINT = 12700


@dataclass(frozen=True)
class NoteLift:
    """One production note to move, and where it sat."""

    slide: int                          # 1-based
    shape_id: Optional[int]
    shape: Optional[str]
    text: str
    left_pt: float = 0.0
    top_pt: float = 0.0

    @property
    def key(self) -> tuple[int, Optional[int]]:
        return (self.slide, self.shape_id)

    @property
    def body(self) -> str:
        return f"{PREFIX}\n{self.text}"


@dataclass(frozen=True)
class LiftResult:
    """What became of one note."""

    lift: NoteLift
    where: str                          # "comment", "notes" or "kept"
    detail: str

    @property
    def moved(self) -> bool:
        return self.where in ("comment", "notes")


def emu_to_points(value: Optional[int]) -> float:
    return float(value or 0) / _EMU_PER_POINT


def lift_notes(deck: Path, lifts: Sequence[NoteLift]) -> list[LiftResult]:
    """Move each note into a comment and take its shape off the slide.

    Returns one result per note, in the order given. Never raises: a note that
    could not be moved is reported as kept, and the deck is left with the note
    still on it, which is the safe end of the trade.
    """
    if not lifts:
        return []

    from .. import powerpoint      # noqa: PLC0415 - Windows only, lazy

    if powerpoint.available():
        try:
            return powerpoint.run(lambda app: _through_powerpoint(app, deck, lifts))
        except Exception:
            log.warning(
                "could not move %d production note(s) into comments through "
                "PowerPoint; falling back to the notes pages",
                len(lifts), exc_info=True,
            )

    try:
        return _through_notes_pages(deck, lifts)
    except Exception:
        log.warning(
            "could not move %d production note(s) off the slides at all; they "
            "are still on the deck", len(lifts), exc_info=True,
        )
        return [
            LiftResult(
                lift, "kept",
                "could not be moved, so it is still on the slide; take it off "
                "by hand before this goes out",
            )
            for lift in lifts
        ]


def _through_powerpoint(app: Any, deck: Path, lifts: Sequence[NoteLift]) -> list[LiftResult]:
    """Add a comment per note, then delete the shape it came from.

    One session, one save, and the comment written before the deletion. The
    two are not a transaction -- nothing here is -- but in this order the
    failure that matters cannot happen: there is no point at which the note
    exists in neither place.
    """
    from ..rebuild.master_apply import _retry   # noqa: PLC0415 - shared COM retry
    from .. import powerpoint                   # noqa: PLC0415

    results: list[LiftResult] = []
    presentation = _retry(
        lambda: app.Presentations.Open(str(deck.resolve()), False, False, False)
    )
    try:
        count = int(presentation.Slides.Count)
        for lift in lifts:
            if not 1 <= lift.slide <= count:
                results.append(LiftResult(
                    lift, "kept",
                    f"slide {lift.slide} is not in the deck any more",
                ))
                continue
            slide = presentation.Slides(lift.slide)
            try:
                _retry(lambda s=slide, l=lift: s.Comments.Add(
                    l.left_pt, l.top_pt, COMMENT_AUTHOR, COMMENT_INITIALS, l.body
                ))
            except Exception as exc:
                # The comment is what makes the deletion safe, so without it
                # the shape stays.
                results.append(LiftResult(
                    lift, "kept",
                    f"the comment could not be added ({exc}), so the note is "
                    "still on the slide",
                ))
                continue
            removed = _delete_shape(slide, lift)
            results.append(LiftResult(
                lift, "comment",
                "moved into a PowerPoint comment on slide "
                f"{lift.slide}" + ("" if removed else
                                   ", but its shape could not be deleted"),
            ))
        _retry(presentation.Save)
    finally:
        powerpoint.quietly(presentation.Close)
    return results


def _delete_shape(slide: Any, lift: NoteLift) -> bool:
    """Delete the note's shape, matched on the id the report named.

    PowerPoint's `Shape.Id` is the same number as the OOXML shape id the rest
    of the tool matches on, which is what lets a finding written by the python
    side be acted on here. Names are not used: a real deck carries sixteen
    shapes of the same name on one slide.
    """
    try:
        for shape in list(slide.Shapes):
            if lift.shape_id is not None and int(shape.Id) == lift.shape_id:
                shape.Delete()
                return True
    except Exception:
        log.debug("could not delete the note's shape", exc_info=True)
    return False


def _through_notes_pages(deck: Path, lifts: Sequence[NoteLift]) -> list[LiftResult]:
    """Move each note to its slide's notes page, with python-pptx alone.

    The destination when PowerPoint cannot be asked. A notes page is not the
    Comments pane and nobody is notified by it, but it is off the slide, still
    beside the right slide, and still there tomorrow.
    """
    from pptx import Presentation      # noqa: PLC0415 - lazy heavy dependency

    presentation = Presentation(str(deck))
    slides = list(presentation.slides)
    results: list[LiftResult] = []
    changed = False

    for lift in lifts:
        if not 1 <= lift.slide <= len(slides):
            results.append(LiftResult(
                lift, "kept", f"slide {lift.slide} is not in the deck any more"
            ))
            continue
        slide = slides[lift.slide - 1]
        try:
            frame = slide.notes_slide.notes_text_frame
            frame.text = (
                f"{frame.text}\n\n{lift.body}" if frame.text.strip() else lift.body
            )
        except Exception as exc:
            results.append(LiftResult(
                lift, "kept",
                f"the notes page could not be written ({exc}), so the note is "
                "still on the slide",
            ))
            continue

        shape = _find_by_id(slide, lift)
        if shape is not None:
            parent = shape._element.getparent()
            if parent is not None:
                parent.remove(shape._element)
        changed = True
        results.append(LiftResult(
            lift, "notes",
            f"moved to the notes page of slide {lift.slide}, because "
            "PowerPoint was not available to make it a comment",
        ))

    if changed:
        presentation.save(str(deck))
    return results


def _find_by_id(slide: Any, lift: NoteLift) -> Optional[Any]:
    for shape in slide.shapes:
        if lift.shape_id is not None and shape.shape_id == lift.shape_id:
            return shape
    return None
