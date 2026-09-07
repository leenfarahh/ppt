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

import atexit
import logging
import os
import queue
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger(__name__)

# Wide enough that 9pt type is legible to the model. The old second half of
# this rationale -- small enough that ten fit in one request -- stopped applying
# when the default became one slide per call, so there is now room to send a
# bigger image. Left at 1280 anyway: on the one defect this was measured
# against, doubling to 2560 made the model's answers worse rather than better,
# so a larger render is a change to make on its own evidence, not as a
# side effect of the batch size.
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

# PowerPoint's Application is a COM singleton: every Dispatch, in every process
# on the machine, reaches the same POWERPNT.EXE. Two things follow, and between
# them they decide the shape of everything below.
#
# Quitting after a render poisons the next one. `Quit` starts a shutdown that
# outlives the call returning, so the Dispatch that follows attaches to an
# instance already on its way down and hands back objects that fail the moment
# they are used -- "PowerPoint could not open the file", or a Presentation that
# raises on `.Slides`. A preview renders the deck before and after in one pass,
# which is precisely that pattern, and it failed about half the time: the
# before image appeared, the after one did not.
#
# And the instance is not ours to quit in the first place. It may be the
# designer's own PowerPoint, with unsaved work open in it.
#
# So: one instance, made on first use and kept; driven from a single thread
# that owns its COM apartment, because an interface pointer is not usable from
# a thread other than the one it was made on and the web UI answers on
# ThreadingHTTPServer; and quit at exit only if we were the ones who started
# it. The single thread serialises concurrent renders as a side effect, which
# is what we want anyway -- there is only one PowerPoint to go around.


def _connect():
    """The PowerPoint already running, or a new one.

    Returns the instance and whether we started it, because that is what says
    whether we are allowed to quit it later.
    """
    import win32com.client  # noqa: PLC0415

    try:
        return win32com.client.GetActiveObject("PowerPoint.Application"), False
    except Exception:
        return win32com.client.Dispatch("PowerPoint.Application"), True


class _PowerPointHost:
    """The one thread allowed to talk to PowerPoint, and the queue into it."""

    # Long enough for the largest deck anyone has rendered, short enough that
    # a COM call wedged forever fails the request instead of the whole server.
    TIMEOUT_S = 1800

    def __init__(self) -> None:
        self._requests: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._broken: Optional[Exception] = None

    def call(self, work):
        """Run `work(app)` on the PowerPoint thread; raise or return as it did."""
        self._start()
        if self._broken is not None:
            raise self._broken
        done = threading.Event()
        box: dict = {}
        self._requests.put((work, box, done))
        if not done.wait(self.TIMEOUT_S):
            raise TimeoutError(
                f"PowerPoint did not answer within {self.TIMEOUT_S}s"
            )
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._serve, name="powerpoint", daemon=True
                )
                self._thread.start()
                atexit.register(self._stop)

    def _stop(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._requests.put(None)
            thread.join(timeout=30)

    def _serve(self) -> None:
        import pythoncom  # noqa: PLC0415

        try:
            pythoncom.CoInitialize()
        except Exception as exc:  # nothing can run; do not leave callers waiting
            self._broken = exc
            self._drain(exc)
            return

        app = None
        ours = False
        try:
            while True:
                item = self._requests.get()
                if item is None:
                    return
                work, box, done = item
                try:
                    if app is None:
                        app, ours = _connect()
                    try:
                        box["value"] = work(app)
                    except Exception:
                        # A failure mid-conversation can mean the instance is
                        # gone -- the designer closed PowerPoint under us -- so
                        # let the reference go and reconnect once. Note what is
                        # deliberately absent: we do not quit it first. Quit
                        # then re-Dispatch is the very race this module exists
                        # to avoid, and against a singleton it buys nothing,
                        # since the reconnect returns the same instance anyway.
                        log.debug("reconnecting to PowerPoint", exc_info=True)
                        app, ours = _connect()
                        box["value"] = work(app)
                except Exception as exc:
                    box["error"] = exc
                    app = None
                finally:
                    done.set()
        finally:
            # The only quit in the module, and only for an instance we started.
            if app is not None and ours:
                _quietly(app.Quit)
            _quietly(pythoncom.CoUninitialize)

    def _drain(self, exc: Exception) -> None:
        while True:
            try:
                item = self._requests.get_nowait()
            except queue.Empty:
                return
            if item is None:
                return
            _work, box, done = item
            box["error"] = exc
            done.set()


_HOST = _PowerPointHost()


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
        out.mkdir(parents=True, exist_ok=True)
        _HOST.call(lambda app: _export(app, deck, out))
        return _collect(out)


def _export(app, deck: Path, out: Path) -> None:
    # A backslash path: PowerPoint reads a forward-slash path containing spaces
    # as a URL and cannot find it. `resolve` gives the native form.
    presentation = app.Presentations.Open(
        str(deck.resolve()), ReadOnly=True, WithWindow=False
    )
    try:
        presentation.Export(str(out), "PNG", DEFAULT_WIDTH, DEFAULT_HEIGHT)
    except Exception:
        _quietly(presentation.Close)  # the export failure is the interesting one
        raise
    # Not housekeeping: a deck left open turns the next Open of the same path
    # into "PowerPoint could not open the file", so a close that fails means
    # the instance must not be reused, and the raise is what says so.
    presentation.Close()


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
