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

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol


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

    Sketch of the implementation:

        import win32com.client
        app = win32com.client.Dispatch("PowerPoint.Application")
        pres = app.Presentations.Open(str(path), WithWindow=False, ReadOnly=True)
        for slide in pres.Slides:
            for shape in slide.Shapes:
                if not shape.HasTextFrame:
                    continue
                rng = shape.TextFrame.TextRange
                lines = [rng.Lines(i + 1).Text for i in range(rng.Lines().Count)]

    Notes for whoever picks this up:
    - Open read-only and without a window; still expect a few seconds per deck.
    - COM indices are 1-based and Lines() with no argument returns the
      collection, not line 1.
    - Shape.Id matches ShapeProfile.shape_id, so ShapeKey maps cleanly.
    - Wrap every call: COM raises pywintypes.com_error for a shape whose text
      frame exists but holds no text range.
    - Kill the app instance in a finally block, or orphaned POWERPNT.EXE
      processes accumulate.
    """

    def __init__(self, deck_path: str | Path) -> None:
        self.deck_path = Path(deck_path)
        self._lines: dict[ShapeKey, list[str]] = {}
        self._loaded = False

    @property
    def available(self) -> bool:
        return False  # TODO: return True once _load() is implemented

    def lines(self, key: ShapeKey) -> Optional[list[str]]:
        if not self._loaded:
            self._load()
        return self._lines.get(key)

    def _load(self) -> None:
        raise NotImplementedError(
            "PowerPoint COM line metrics are not implemented yet; "
            "see the docstring in formatting_tool/linemetrics.py"
        )


def default_provider(deck_path: str | Path) -> LineMetricsProvider:
    """Provider used unless the caller supplies one."""
    return NullLineMetrics()
