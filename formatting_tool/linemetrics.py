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
from typing import Any, Iterable, Optional, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShapeKey:
    """Addresses one text box in one deck."""

    slide: int          # 1-based
    shape_id: int


@dataclass(frozen=True)
class TextBounds:
    """The rectangle the renderer actually drew a shape's text into, in inches.

    Not the shape's box. The two differ whenever the text does not fit, and
    the difference is the defect: a box 0.22in tall holding three lines of 8pt
    draws 0.40in of text, and the 0.18in that does not fit lands on whatever
    is underneath. Nothing in the .pptx says so -- the file stores a paragraph
    and a box, and the renderer decides -- so this can only be measured.
    """

    left_in: float
    top_in: float
    width_in: float
    height_in: float

    @property
    def right_in(self) -> float:
        return self.left_in + self.width_in

    @property
    def bottom_in(self) -> float:
        return self.top_in + self.height_in


class LineMetricsProvider(Protocol):
    """Supplies rendered line breaks and text bounds for a text box."""

    @property
    def available(self) -> bool:
        """False when this provider cannot produce metrics for this deck."""
        ...

    def lines(self, key: ShapeKey) -> Optional[list[str]]:
        """Rendered lines for one shape, or None if unknown."""
        ...

    def bounds(self, key: ShapeKey) -> Optional[TextBounds]:
        """Where the text was actually drawn, or None if unknown."""
        ...

    def refresh(
        self, keys: Iterable[ShapeKey], deck_path: Optional[str | Path] = None
    ) -> bool:
        """Measure these shapes again, on the deck as it now stands.

        The seam a fix that changes TYPE needs. Everything else this module
        supplies is read once, up front, because the rules are asking about a
        file nobody is editing. A fix that resizes type is different: how much
        copy fits changes with the size, so the plan is an estimate until the
        renderer has been asked again, and the applier has to be able to ask
        about the four boxes it touched without paying for a re-read of every
        shape on every slide.

        `deck_path` points the provider at a different file, which is the
        normal case here: the fixes were planned against the input deck and
        have to be verified against the output.

        Returns False when nothing could be measured, which a caller treats
        the way it treats an unavailable provider: it says nothing rather
        than guessing.
        """
        ...


class NullLineMetrics:
    """The default: knows nothing, admits it."""

    reason = "no renderer configured"

    @property
    def available(self) -> bool:
        return False

    def lines(self, key: ShapeKey) -> Optional[list[str]]:
        return None

    def bounds(self, key: ShapeKey) -> Optional[TextBounds]:
        return None

    def refresh(
        self, keys: Iterable[ShapeKey], deck_path: Optional[str | Path] = None
    ) -> bool:
        return False


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
        self._bounds: dict[ShapeKey, TextBounds] = {}
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

    def bounds(self, key: ShapeKey) -> Optional[TextBounds]:
        if not self._loaded:
            self._load()
        return self._bounds.get(key)

    def refresh(
        self, keys: Iterable[ShapeKey], deck_path: Optional[str | Path] = None
    ) -> bool:
        """Re-read just these shapes, from `deck_path` if one is given.

        Only the slides the keys name are opened for, and only the shapes on
        them whose id is asked for. On a forty-slide deck with four shapes to
        check that is one slide walked instead of forty, and the saving is
        the whole reason this exists: verifying a type fix must not cost what
        measuring the deck costs.

        A provider that had not loaded counts as loaded afterwards, knowing
        these keys and nothing else, so asking whether it is `available` does
        not then trigger the full read this call exists to avoid. That is a
        working state rather than a half-broken one -- `bounds` already
        answers None for a shape the renderer said nothing about, and every
        rule treats that as unknown -- but it is why this is `refresh` rather
        than `load`: it is for a caller that knows which shapes it cares
        about.
        """
        wanted: dict[int, set[int]] = {}
        for key in keys:
            wanted.setdefault(key.slide, set()).add(key.shape_id)
        if not wanted:
            return False

        path = Path(deck_path) if deck_path is not None else self.deck_path
        from . import powerpoint      # noqa: PLC0415 - lazy, Windows only

        if not powerpoint.available():
            return False
        try:
            powerpoint.run(lambda app: self._read_some(app, path, wanted))
        except Exception:
            log.warning(
                "could not re-measure %d shape(s) in %s",
                sum(len(ids) for ids in wanted.values()), path.name,
                exc_info=True,
            )
            return False
        # Whatever it found, it found on `path`, so a provider that was
        # holding measurements of another file is no longer describing one
        # deck. Say which, so a caller reading `deck_path` is not misled.
        self.deck_path = path
        self._loaded = True
        self._ok = True
        return any(
            ShapeKey(slide, shape_id) in self._bounds
            for slide, ids in wanted.items()
            for shape_id in ids
        )

    def _read_some(self, app: Any, path: Path, wanted: dict[int, set[int]]) -> None:
        from . import powerpoint      # noqa: PLC0415

        presentation = None
        try:
            presentation = app.Presentations.Open(
                str(path.resolve()), True, False, False
            )
            for slide in _com_each(presentation.Slides):
                number = int(slide.SlideNumber)
                ids = wanted.get(number)
                if not ids:
                    continue
                # Stale first. A shape the renderer will not talk about this
                # time must not answer with what it said last time, which
                # would be a measurement of the file before the fix.
                for shape_id in ids:
                    self._bounds.pop(ShapeKey(number, shape_id), None)
                    self._lines.pop(ShapeKey(number, shape_id), None)
                for shape in _com_each(slide.Shapes):
                    self._read(number, shape, only=ids)
            presentation.Close()
            presentation = None
        finally:
            if presentation is not None:
                powerpoint.quietly(presentation.Close)

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

    def _read(
        self, slide: int, shape: Any, only: Optional[set[int]] = None
    ) -> None:
        """One shape's rendered lines, descending into groups.

        Groups are walked because that is where a messy deck keeps its text,
        and `ShapeProfile.shape_id` is assigned the same way, so the keys line
        up without either side knowing about the other.

        `only` narrows this to a set of shape ids, for `refresh`. Groups are
        still descended into: a wanted shape can be inside one, and the group
        itself carries a different id.
        """
        try:
            if int(shape.Type) == _MSO_GROUP:
                for child in _com_each(shape.GroupItems):
                    self._read(slide, child, only)
                return
            if only is not None and int(shape.Id) not in only:
                return
            if not int(shape.HasTextFrame):
                return
            text_range = shape.TextFrame.TextRange
            count = int(text_range.Lines().Count)
            if not count:
                return
            key = ShapeKey(slide, int(shape.Id))
            self._lines[key] = [
                str(text_range.Lines(i + 1).Text) for i in range(count)
            ]
            # Read in the same pass, off the same range. Opening the deck is
            # what costs seconds; two more property reads per shape cost
            # nothing, and asking again later would mean opening it twice.
            bounds = _bounds_of(text_range)
            if bounds is not None:
                self._bounds[key] = bounds
        except Exception:
            # A shape with a frame but no range, a placeholder PowerPoint will
            # not talk about, a picture pretending to have text. None of them
            # is a failure of the deck.
            log.debug("no line metrics for a shape on slide %d", slide, exc_info=True)


_POINTS_PER_INCH = 72.0


def _bounds_of(text_range: Any) -> Optional[TextBounds]:
    """The rendered text rectangle off a COM TextRange, in inches.

    PowerPoint reports these in points and relative to the slide, which is
    the same frame of reference `ShapeProfile.geometry` uses once converted,
    so the two can be compared directly.
    """
    try:
        return TextBounds(
            left_in=float(text_range.BoundLeft) / _POINTS_PER_INCH,
            top_in=float(text_range.BoundTop) / _POINTS_PER_INCH,
            width_in=float(text_range.BoundWidth) / _POINTS_PER_INCH,
            height_in=float(text_range.BoundHeight) / _POINTS_PER_INCH,
        )
    except Exception:
        return None


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
