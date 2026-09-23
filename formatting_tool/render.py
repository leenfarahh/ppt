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
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from . import powerpoint
from .workdir import workroot

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

# What the design check renders at, which is bigger on purpose. That review is
# one slide per call and the whole question is what the slide LOOKS like, so
# the model wants every pixel it can be given: 1568 is the long edge past
# which a picture is downsized before it is looked at, and 1536x864 is the
# largest 16:9 frame under it. The validation layer stays at 1280 -- see above
# for the measurement that keeps it there.
DESIGN_QA_SIZE = (1536, 864)


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

    def render(
        self, deck: Path, out: Path, slides: Optional[list[int]] = None
    ) -> dict[int, Path]: ...


# --------------------------------------------------------------------------- #
# PowerPoint, through COM
# --------------------------------------------------------------------------- #

# The instance and the thread that owns it live in `formatting_tool.powerpoint`,
# because applying a master needs the same one. There is only one PowerPoint on
# a machine to have; see that module for what follows from that.



# Above this share of a deck, exporting slide by slide costs more than
# exporting the lot. Measured on a real 105-slide deck: the whole deck goes in
# 11.1s, 0.106s a slide, and one slide on its own takes 0.52s -- five times the
# per-slide cost, because each call reopens the export path. So a handful of
# slides is much cheaper one at a time and a third of the deck is not.
_PER_SLIDE_SHARE = 0.2


class PowerPointRenderer:
    """Export slides as PNG by driving PowerPoint. Windows only.

    `Presentation.Export` writes the whole deck in one call and names the files
    itself (Slide1.PNG, Slide2.PNG, ...). For the whole deck that is both
    faster and more robust than going slide by slide -- per-slide
    `Slide.Export` fails on some paths with an unhelpful "can't save ^0 to ^1".

    ASKED FOR A FEW SLIDES, it exports those instead, and that is the case the
    UI is nearly always in: a designer applied eleven fixes across three slides
    and wants to see three slides. Rendering the other hundred to answer that
    is most of what "the preview takes forever" was, and all of what an undo
    added, since an undo writes the deck again and every image of it is then of
    a file that no longer exists.

    Per-slide export is the fragile one, so it is the one that falls back: any
    failure drops through to the whole-deck export, which is what this did
    before and still does for everything else.
    """

    name = "powerpoint"

    def __init__(self, size: Optional[tuple[int, int]] = None) -> None:
        """`size` is the pixel frame to export into, long edge first.

        A renderer rather than a caller decides this because PowerPoint is
        told the size at export time and nothing downstream can change it
        afterwards. The default is what the validation layer has always sent;
        the design check asks for `DESIGN_QA_SIZE`.
        """
        self.width, self.height = size or (DEFAULT_WIDTH, DEFAULT_HEIGHT)

    @property
    def available(self) -> bool:
        return powerpoint.available()

    # A person is watching a render, so it does not get the half hour a batch
    # job can afford. Five minutes is far longer than any deck measured here
    # takes -- a 31-slide, 31MB deck exports in 5.7s -- so reaching this means
    # PowerPoint is wedged or showing a dialog, and saying so beats a page that
    # never answers.
    TIMEOUT_S = 300

    def render(
        self, deck: Path, out: Path, slides: Optional[list[int]] = None
    ) -> dict[int, Path]:
        out.mkdir(parents=True, exist_ok=True)
        powerpoint.run(
            lambda app: _export(app, deck, out, slides, (self.width, self.height)),
            timeout=self.TIMEOUT_S,
        )
        return _collect(out)


def _export(
    app,
    deck: Path,
    out: Path,
    slides: Optional[list[int]] = None,
    size: Optional[tuple[int, int]] = None,
) -> None:
    # A backslash path: PowerPoint reads a forward-slash path containing spaces
    # as a URL and cannot find it. `resolve` gives the native form.
    presentation = powerpoint.retry(lambda: app.Presentations.Open(
        str(deck.resolve()), ReadOnly=True, WithWindow=False
    ))
    width, height = size or (DEFAULT_WIDTH, DEFAULT_HEIGHT)
    try:
        if not _export_some(presentation, out, slides, (width, height)):
            presentation.Export(str(out), "PNG", width, height)
    except Exception:
        powerpoint.close(presentation)  # the export failure is the interesting one
        raise
    # Not housekeeping: a deck left open turns the next Open of the same path
    # into "PowerPoint could not open the file", so a close that fails means
    # the instance must not be reused, and the raise is what says so.
    powerpoint.retry(lambda: presentation.Close())


def _export_some(
    presentation,
    out: Path,
    slides: Optional[list[int]],
    size: Optional[tuple[int, int]] = None,
) -> bool:
    """Export just these slides, or say it did not.

    False rather than an exception, because the caller's answer to both "not
    worth it" and "PowerPoint would not" is the same: export the whole deck.
    A half-written set is cleaned up first, so the fallback cannot leave one
    slide from this attempt sitting beside the full export.
    """
    if not slides:
        return False
    try:
        count = int(presentation.Slides.Count)
    except Exception:
        return False
    wanted = sorted({n for n in slides if 1 <= n <= count})
    if not wanted or len(wanted) > max(1, int(count * _PER_SLIDE_SHARE)):
        return False

    written: list[Path] = []
    try:
        for number in wanted:
            target = out / f"Slide{number}.PNG"
            presentation.Slides(number).Export(
                str(target), "PNG", *(size or (DEFAULT_WIDTH, DEFAULT_HEIGHT))
            )
            written.append(target)
    except Exception:
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        log.debug("per-slide export failed; exporting the whole deck", exc_info=True)
        return False
    return True


def _rendered(
    renderer: Renderer, deck: Path, directory: Path, slides: Optional[list[int]]
) -> dict[int, Path]:
    """Ask for the slides wanted, and accept a renderer that cannot narrow."""
    try:
        return renderer.render(deck, directory, slides)      # type: ignore[call-arg]
    except TypeError:
        return renderer.render(deck, directory)

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

    def render(
        self, deck: Path, out: Path, slides: Optional[list[int]] = None
    ) -> dict[int, Path]:
        return {}


# LibreOffice is the obvious portable fallback and is deliberately not written
# here. `soffice --convert-to png` renders only the first slide, so the real
# path is pptx -> pdf -> raster, which needs a PDF rasteriser as a second
# dependency, and LibreOffice wraps text a few percent differently from
# PowerPoint. Since the questions a render answers are exactly the borderline
# ones, a renderer that disagrees with the one the client will open is worse
# than none. Whoever adds it should treat its findings as weaker.


# What PowerPoint says when it is handed a path that is not there. It arrives
# as the scode of a COM tuple, which is how `0x80070003` -- the plain Win32
# "the system cannot find the path specified" -- reaches a log looking like a
# fault in the renderer.
_PATH_NOT_FOUND = -2147024893


def _explain(exc: Exception, deck: Path, directory: Path) -> str:
    """A renderer failure in words, where the words are knowable.

    ONE CASE EARNS THIS. A deck uploaded to the page lives in the system
    temporary directory, and so does the directory the PNGs are exported to.
    Windows Storage Sense and the managed cleanup tools an IT department
    installs both delete from there on a schedule, with no regard for a process
    that is using it -- on this machine every session directory went while the
    server was running. What the page then showed was the COM tuple, which
    names neither the file nor the cause and reads like a bug in the tool.
    """
    # Searched in the text, because the code arrives NESTED: pywin32 gives
    # `(-2147352567, 'Exception occurred.', (0, None, None, None, 0,
    # -2147024893), None)`, so the part worth reading is two levels down inside
    # the arguments rather than among them.
    if str(_PATH_NOT_FOUND) in str(exc):
        missing = deck if not deck.is_file() else directory
        return (
            f"PowerPoint could not find {missing}. Files in the system "
            "temporary directory are removed on a schedule by Windows Storage "
            "Sense and by managed cleanup tools, whether or not something is "
            "using them. Upload the deck again"
        )
    return f"rendering failed: {exc}"


def available_renderer(size: Optional[tuple[int, int]] = None) -> Renderer:
    """The renderer this host has, exporting at `size` where it can honour it."""
    for renderer in (PowerPointRenderer(size),):
        if renderer.available:
            return renderer
    return NullRenderer()


def render_deck(
    deck: str | Path,
    renderer: Optional[Renderer] = None,
    slides: Optional[list[int]] = None,
) -> SlideImages:
    """Render a deck to PNGs in a temporary directory.

    `slides` names the ones actually wanted. It is a request, not a promise:
    a renderer free to ignore it comes back with the whole deck, and the caller
    takes what it needs from that, so nothing downstream has to know which it
    got.

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

    # Checked here, where it can be said in words. PowerPoint answers a missing
    # file with a COM tuple -- `(-2147352567, 'Exception occurred.', (0, None,
    # None, None, 0, -2147024893), None)` -- which is 0x80070003, "the system
    # cannot find the path specified", and reads like a fault in the renderer.
    # It is worth the one stat call to say which file is gone instead.
    if not deck.is_file():
        log.warning("could not render %s: it is no longer there", deck)
        return SlideImages(
            renderer=renderer.name,
            reason=(
                f"{deck} is not there any more. A deck uploaded to this page "
                "lives in the system temporary directory, and something has "
                "removed it -- Windows Storage Sense and managed cleanup tools "
                "both do. Upload it again"
            ),
        )

    # Inside FORMATTING_TOOL_WORKDIR when it is set. This used to be a bare
    # `mkdtemp`, so the renders went to the system temporary directory whatever
    # that variable said -- and the renders are what vanishes: a run lost
    # `Slide20.PNG` mid-review after sixteen batches were already home. The
    # escape hatch existed and did not cover the file that went. See
    # `formatting_tool.workdir`.
    directory = Path(
        tempfile.mkdtemp(prefix="formatting-tool-render-", dir=workroot())
    )
    try:
        images = _rendered(renderer, deck, directory, slides)
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        reason = _explain(exc, deck, directory)
        log.warning("could not render %s: %s", deck.name, reason)
        return SlideImages(renderer=renderer.name, reason=reason)

    if not images:
        shutil.rmtree(directory, ignore_errors=True)
        return SlideImages(renderer=renderer.name, reason="the renderer produced no images")

    log.info("rendered %d slide(s) of %s with %s", len(images), deck.name, renderer.name)
    return SlideImages(renderer=renderer.name, directory=directory, images=images)
