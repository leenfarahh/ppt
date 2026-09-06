"""Render slides to images, so the AI layer can look instead of infer.

A .pptx stores a paragraph and a box, not the lines they render into, and it
stores two bounding boxes, not whether the text inside them actually collides.
Everything measurable is better read from the XML: "the left edge is 0.06in off
the grid line" is exact, and no vision model will ever see 0.06in. What needs a
picture is the class of defect that only exists once rendered.

    text clipped by its box       the box holds more copy than it can show
    a real collision             two boxes overlap; does the type touch?
    contrast over an image       no palette check can answer this
    z-order occlusion            geometrically fine, visually hidden
    optical alignment            what the eye reads as level

PowerPoint is the renderer because it is the one that will open the deck. A
LibreOffice conversion wraps a few percent differently, which is exactly the
margin these questions turn on.

Rendering is opt-in and degrades: with no renderer available the AI layer runs
on the JSON alone, as it always has, and says so rather than pretending to
have looked.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger(__name__)

# Wide enough that 9pt type is legible to the model, small enough that ten of
# them fit in one request without crowding out the payload.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720


@dataclass
class SlideImages:
    """Rendered slides, by 1-based slide number."""

    renderer: str
    directory: Optional[Path] = None
    images: dict[int, Path] = field(default_factory=dict)
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.images)

    def for_slides(self, numbers: list[int]) -> list[tuple[int, Path]]:
        return [(n, self.images[n]) for n in numbers if n in self.images]

    def cleanup(self) -> None:
        if self.directory and self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)


class Renderer(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def render(self, deck: Path, out: Path) -> dict[int, Path]: ...


# --------------------------------------------------------------------------- #
# PowerPoint, through COM
# --------------------------------------------------------------------------- #

class PowerPointRenderer:
    """Export every slide as PNG by driving PowerPoint. Windows only.

    `Presentation.Export` writes the whole deck in one call and names the files
    itself (Slide1.PNG, Slide2.PNG, ...), which is both faster and more robust
    than exporting slide by slide: per-slide `Slide.Export` fails on some paths
    with an unhelpful "can't save ^0 to ^1".
    """

    name = "powerpoint"

    @property
    def available(self) -> bool:
        try:
            import win32com.client  # noqa: F401, PLC0415
        except ImportError:
            return False
        return os.name == "nt"

    def render(self, deck: Path, out: Path) -> dict[int, Path]:
        import pythoncom  # noqa: PLC0415
        import win32com.client  # noqa: PLC0415

        out.mkdir(parents=True, exist_ok=True)
        pythoncom.CoInitialize()
        app = win32com.client.Dispatch("PowerPoint.Application")
        presentation = None
        try:
            presentation = app.Presentations.Open(
                str(deck.resolve()), ReadOnly=True, WithWindow=False
            )
            presentation.Export(str(out), "PNG", DEFAULT_WIDTH, DEFAULT_HEIGHT)
        finally:
            # An orphaned POWERPNT.EXE per run accumulates fast, so the close
            # and the quit both happen whatever went wrong.
            if presentation is not None:
                _quietly(presentation.Close)
            _quietly(app.Quit)
            _quietly(pythoncom.CoUninitialize)

        return _collect(out)


def _quietly(call) -> None:
    try:
        call()
    except Exception:
        log.debug("ignoring an error while shutting PowerPoint down", exc_info=True)


def _collect(out: Path) -> dict[int, Path]:
    """Map the exported files back onto slide numbers.

    PowerPoint names them Slide1.PNG upward, in slide order, so the number in
    the filename is the slide number.
    """
    images: dict[int, Path] = {}
    for path in out.glob("*.PNG"):
        digits = "".join(c for c in path.stem if c.isdigit())
        if digits:
            images[int(digits)] = path
    for path in out.glob("*.png"):
        digits = "".join(c for c in path.stem if c.isdigit())
        if digits:
            images.setdefault(int(digits), path)
    return dict(sorted(images.items()))


# --------------------------------------------------------------------------- #
# Nothing available
# --------------------------------------------------------------------------- #

class NullRenderer:
    """The default when nothing can render. Says so rather than guessing."""

    name = "none"

    @property
    def available(self) -> bool:
        return False

    def render(self, deck: Path, out: Path) -> dict[int, Path]:
        return {}


# LibreOffice is the obvious portable fallback and is deliberately not written
# here. `soffice --convert-to png` renders only the first slide, so the real
# path is pptx -> pdf -> raster, which needs a PDF rasteriser as a second
# dependency, and LibreOffice wraps text a few percent differently from
# PowerPoint. Since the questions a render answers are exactly the borderline
# ones, a renderer that disagrees with the one the client will open is worse
# than none. Whoever adds it should treat its findings as weaker.


def available_renderer() -> Renderer:
    for renderer in (PowerPointRenderer(),):
        if renderer.available:
            return renderer
    return NullRenderer()


def render_deck(deck: str | Path, renderer: Optional[Renderer] = None) -> SlideImages:
    """Render a deck to PNGs in a temporary directory.

    Never raises. A renderer that is missing, or that fails halfway, gives back
    an empty SlideImages carrying the reason, and the caller carries on without
    pictures.
    """
    deck = Path(deck)
    renderer = renderer or available_renderer()

    if not renderer.available:
        return SlideImages(
            renderer=renderer.name,
            reason=(
                "no renderer available: PowerPoint and pywin32 are needed "
                "(pip install pywin32) and this must run on Windows"
            ),
        )

    directory = Path(tempfile.mkdtemp(prefix="formatting-tool-render-"))
    try:
        images = renderer.render(deck, directory)
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        log.warning("could not render %s: %s", deck.name, exc)
        return SlideImages(renderer=renderer.name, reason=f"rendering failed: {exc}")

    if not images:
        shutil.rmtree(directory, ignore_errors=True)
        return SlideImages(renderer=renderer.name, reason="the renderer produced no images")

    log.info("rendered %d slide(s) of %s with %s", len(images), deck.name, renderer.name)
    return SlideImages(renderer=renderer.name, directory=directory, images=images)
