"""Where rendered line breaks come from.

Orphans and widows are properties of the *rendered* text, and a .pptx does not
store them: it stores a paragraph and a box, and the renderer decides where the
lines fall. python-pptx therefore cannot answer "does this title wrap to two
lines with one word on the second". Nothing in the deterministic layer can,
without a renderer.

This module is the seam. Rules ask a LineMetricsProvider for line breaks; if
the provider cannot supply them, the rule reports nothing rather than guessing.

Three implementations are possible, in descending order of fidelity:

1. PowerPointComMetrics -- drive PowerPoint through COM on Windows and read
   TextRange.Lines(). Exact, because it is the same engine that renders the
   deck. Needs PowerPoint installed and a visible-ish app instance, so it is
   opt-in and slow (seconds per deck). This is the recommended path here, given
   the Microsoft 365 estate.
2. LibreOfficeMetrics -- headless `soffice --convert-to pdf`, then read the
   text layout back out of the PDF. Portable and CI-friendly, but LibreOffice
   wraps a few percent differently from PowerPoint, which produces both false
   positives and misses on borderline lines.
3. FontMetricsEstimator -- measure glyph advances from the installed TTF and
   simulate the wrap. No dependencies beyond the font file, but ignores
   kerning, ligatures, justification and Arabic shaping, so it is unusable for
   bilingual decks.

Until one is implemented, NullLineMetrics is the default and the orphan/widow
rules stay silent. Silent is the correct failure mode: a formatting report that
invents wrap positions is worse than one that omits them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShapeKey:
    """Addresses one text box in one deck."""

    slide: int          # 1-based
    shape_id: int


class LineMetricsProvider(Protocol):
    """Supplies rendered line breaks for a text box."""

    @property
    def available(self) -> bool:
        """False when this provider cannot produce metrics for this deck."""
        ...

    def lines(self, key: ShapeKey) -> Optional[list[str]]:
        """Rendered lines for one shape, or None if unknown."""
        ...


class NullLineMetrics:
    """The default: knows nothing, admits it."""

    reason = "no renderer configured"

    @property
    def available(self) -> bool:
        return False

    def lines(self, key: ShapeKey) -> Optional[list[str]]:
        return None


class PowerPointComMetrics:
    """Read real line breaks out of PowerPoint via COM. Windows only.

    The only honest source for this. Where a line breaks is not in the file:
    a .pptx stores a paragraph and a box, and the renderer decides. Asking the
    renderer is the whole idea, and it is the same engine that will draw the
    deck for the client.

    Read once and cached. Opening a deck costs seconds and a rule asks about
    every text box on every slide, so the alternative is seconds per shape.

    Never raises. A host with no PowerPoint, a deck it will not open, a shape
    whose text frame holds no range -- each comes back as "unknown", and a
    rule that cannot get metrics reports nothing rather than guessing.
    """

    def __init__(self, deck_path: str | Path) -> None:
        self.deck_path = Path(deck_path)
        self._lines: dict[ShapeKey, list[str]] = {}
        self._loaded = False
        self._ok = False

    @property
    def available(self) -> bool:
        if not self._loaded:
            self._load()
        return self._ok

    def lines(self, key: ShapeKey) -> Optional[list[str]]:
        if not self._loaded:
            self._load()
        return self._lines.get(key)

    def _load(self) -> None:
        self._loaded = True
        from . import powerpoint      # noqa: PLC0415 - lazy, Windows only

        if not powerpoint.available():
            log.debug("no PowerPoint on this host; line metrics unavailable")
            return

        try:
            # On the thread that owns PowerPoint, like every other COM caller
            # here; automation is single-threaded and reaching it from another
            # thread is how an instance ends up wedged.
            powerpoint.run(self._read_deck)
            self._ok = True
        except Exception:
            log.warning(
                "could not read line metrics from %s; the orphan and widow "
                "checks will not run", self.deck_path.name, exc_info=True,
            )

    def _read_deck(self, app: Any) -> None:
        from . import powerpoint      # noqa: PLC0415

        presentation = None
        try:
            presentation = app.Presentations.Open(
                str(self.deck_path.resolve()), True, False, False
            )
            for slide in _com_each(presentation.Slides):
                number = int(slide.SlideNumber)
                for shape in _com_each(slide.Shapes):
                    self._read(number, shape)
            presentation.Close()
            presentation = None
        finally:
            if presentation is not None:
                powerpoint.quietly(presentation.Close)

    def _read(self, slide: int, shape: Any) -> None:
        """One shape's rendered lines, descending into groups.

        Groups are walked because that is where a messy deck keeps its text,
        and `ShapeProfile.shape_id` is assigned the same way, so the keys line
        up without either side knowing about the other.
        """
        try:
            if int(shape.Type) == _MSO_GROUP:
                for child in _com_each(shape.GroupItems):
                    self._read(slide, child)
                return
            if not int(shape.HasTextFrame):
                return
            text_range = shape.TextFrame.TextRange
            count = int(text_range.Lines().Count)
            if not count:
                return
            self._lines[ShapeKey(slide, int(shape.Id))] = [
                str(text_range.Lines(i + 1).Text) for i in range(count)
            ]
        except Exception:
            # A shape with a frame but no range, a placeholder PowerPoint will
            # not talk about, a picture pretending to have text. None of them
            # is a failure of the deck.
            log.debug("no line metrics for a shape on slide %d", slide, exc_info=True)


_MSO_GROUP = 6


def _com_each(collection: Any):
    """A 1-based COM collection as an iterator, skipping what will not come."""
    try:
        count = int(collection.Count)
    except Exception:
        return
    for i in range(1, count + 1):
        try:
            yield collection(i)
        except Exception:
            continue


def default_provider(deck_path: str | Path) -> LineMetricsProvider:
    """Provider used unless the caller supplies one.

    PowerPoint where it can run, and nothing where it cannot. Nothing is a
    working answer: the rules that need metrics stay silent, which is the
    correct failure mode -- a report that invents wrap positions is worse than
    one that omits them.
    """
    try:
        from . import powerpoint      # noqa: PLC0415

        if powerpoint.available():
            return PowerPointComMetrics(deck_path)
    except Exception:
        log.debug("could not decide on a line-metrics provider", exc_info=True)
    return NullLineMetrics()
