"""Render the master's layouts, so the model can look at them too.

WHY THIS EXISTS. `ai.layout` shows the model a picture of the slide and a
written list of the layouts, and asks which one the slide belongs on. That is
half a comparison. Measured on a real master of fourteen layouts, seven of them
-- `Title with Content 01` through `07` -- are IDENTICAL in everything the
written list can say: each offers one title and one content region, and six of
the seven put them at the same inches. What separates them is decoration no
placeholder records:

    01  plain, full width
    02  a graphic across the top third
    03  a 3.65in panel down the right
    04  an image band across the top half
    05  a half-canvas image on the left
    06  a band across the bottom quarter
    07  a 6.19in panel on the left, and a small title

A designer tells those apart at a glance and no description tells them apart at
all, so the model was answering `01` every time -- correctly, given what it was
shown. Asked to choose between seven descriptions that read the same, there is
no better answer than the first.

So the layouts are rendered once and shown. One image, not fourteen: a contact
sheet with a number burned onto each tile, keyed to the same numbers in the
written list. Fourteen separate parts would be fourteen uploads on every one of
seventeen calls; a sheet is one.

WHAT IS DRAWN ON THEM. A layout renders empty -- PowerPoint does not export the
"Click to edit" prompts -- so the decoration would show and the regions would
not, which is exactly backwards for the question being asked. Each region is
filled with its own name before rendering, and a picture region, which cannot
hold text, is covered with a labelled block. The tile then shows both halves of
what a layout is: where its content goes, and what is already on the page.

EVERYTHING HERE DEGRADES TO NOTHING. No renderer, no Pillow, a master that will
not open: the sheet is None and `ai.layout` asks its question the way it did
before, with the written list alone.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..models import MARGIN_CHROME, LayoutProfile
from ..render import DEFAULT_HEIGHT, DEFAULT_WIDTH, Renderer, render_deck

log = logging.getLogger(__name__)

# Tiles across the sheet. Four keeps a 14-layout master to four rows and each
# tile at 480px wide, which is enough to see a panel down one side and a band
# across the top -- the whole of what this is for. Narrower tiles start losing
# the thin rules some layouts differ by.
_COLUMNS = 4
_TILE_W = 480
_TILE_H = int(_TILE_W * DEFAULT_HEIGHT / DEFAULT_WIDTH)
_CAPTION_H = 30
_PAD = 8


@dataclass
class LayoutSheet:
    """One image of every layout, and the numbers written on it."""

    path: Path
    numbers: dict[str, int] = field(default_factory=dict)
    directory: Optional[Path] = None

    def number_of(self, layout: str) -> Optional[int]:
        return self.numbers.get(layout)

    def cleanup(self) -> None:
        if self.directory and self.directory.exists():
            from shutil import rmtree  # noqa: PLC0415 - only on the way out

            rmtree(self.directory, ignore_errors=True)


def build_sheet(
    master: Path,
    layouts: Sequence[LayoutProfile],
    directory: Optional[Path] = None,
    renderer: Optional[Renderer] = None,
) -> Optional[LayoutSheet]:
    """A contact sheet of these layouts, or None when one cannot be made.

    Never raises. Every failure along the way -- no Pillow, no renderer, a
    master that will not open, a layout that will not take a slide -- leaves
    the caller with None and a line in the log, and `ai.layout` then asks its
    question from the written inventory alone, which is what it did before.
    """
    if not layouts:
        return None
    try:
        from PIL import Image  # noqa: F401, PLC0415 - checked before any work
    except ImportError:
        log.info("no layout sheet: Pillow is not installed")
        return None

    own = directory is None
    directory = Path(directory or tempfile.mkdtemp(prefix="formatting-tool-sheet-"))
    directory.mkdir(parents=True, exist_ok=True)

    try:
        sample = directory / "layouts.pptx"
        ordered = _write_sample(master, layouts, sample)
        if not ordered:
            return None

        images = render_deck(sample, renderer=renderer)
        if not images:
            log.info("no layout sheet: %s", images.reason)
            return None
        try:
            sheet = _compose(
                directory / "layouts.png",
                [(name, images.images[n]) for n, name in ordered
                 if n in images.images],
            )
        finally:
            images.cleanup()
    except Exception:
        log.warning("could not render the master's layouts; the model will be "
                    "given the written list alone", exc_info=True)
        return None

    if sheet is None:
        return None
    sheet.directory = directory if own else None
    log.info("rendered %d of the master's layouts onto one sheet",
             len(sheet.numbers))
    return sheet


def _write_sample(
    master: Path, layouts: Sequence[LayoutProfile], out: Path
) -> list[tuple[int, str]]:
    """A deck holding one labelled slide per layout, in the master's order.

    Returns (slide number, layout name) pairs, which is what maps a rendered
    file back onto the layout it shows. A layout that will not take a slide is
    skipped rather than failing the sheet: thirteen tiles beats none.
    """
    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    base = Presentation(str(master))
    _drop_slides(base)

    live = list(_live_layouts(base))
    ordered: list[tuple[int, str]] = []
    for layout in layouts:
        if layout.index >= len(live):
            continue
        try:
            slide = base.slides.add_slide(live[layout.index])
        except Exception:
            log.debug("layout %r would not take a slide", layout.name,
                      exc_info=True)
            continue
        _label(slide)
        ordered.append((len(ordered) + 1, layout.name))

    if not ordered:
        return []
    base.save(str(out))
    return ordered


def _live_layouts(base: Any) -> list[Any]:
    """Every layout of every master, in the order the profiles were read.

    By position and not by name, for the reason `rebuild.builder` gives: one
    file may carry two slide masters using the same layout name, and the
    profile's index is what identifies which.
    """
    return [layout for master in base.slide_masters for layout in master.slide_layouts]


def _drop_slides(base: Any) -> None:
    slide_ids = base.slides._sldIdLst
    for slide_id in list(slide_ids):
        base.part.drop_rel(slide_id.rId)
        slide_ids.remove(slide_id)


def _label(slide: Any) -> None:
    """Write each region's own name into it, so the render shows where it is.

    An empty placeholder exports as nothing at all, so a layout rendered as it
    stands shows its decoration and hides the thing being asked about. The
    words are the region kinds rather than sample copy on purpose: the question
    is where a title goes and how wide the body is, and lorem ipsum answers it
    less clearly than the word TITLE does.
    """
    for shape in list(slide.placeholders):
        token = _token(shape)
        if token in MARGIN_CHROME:
            continue
        if token == "PICTURE":
            _cover(slide, shape, "IMAGE")
            continue
        try:
            shape.text_frame.text = _word(token)
        except Exception:                       # a region that holds no text
            _cover(slide, shape, _word(token))


def _word(token: str) -> str:
    if "TITLE" in token and token != "SUBTITLE":
        return "TITLE"
    if token == "SUBTITLE":
        return "SUBTITLE"
    return "CONTENT"


def _cover(slide: Any, shape: Any, word: str) -> None:
    """Draw a labelled block over a region that cannot show text of its own."""
    from pptx.enum.shapes import MSO_SHAPE  # noqa: PLC0415 - lazy
    from pptx.dml.color import RGBColor  # noqa: PLC0415 - lazy

    box = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, shape.left, shape.top, shape.width, shape.height
    )
    box.fill.solid()
    box.fill.fore_color.rgb = RGBColor(0xD0, 0xD0, 0xD0)
    box.line.color.rgb = RGBColor(0x60, 0x60, 0x60)
    box.text_frame.text = word
    for paragraph in box.text_frame.paragraphs:
        for run in paragraph.runs:
            run.font.color.rgb = RGBColor(0x20, 0x20, 0x20)


def _token(shape: Any) -> str:
    try:
        return str(shape.placeholder_format.type).split()[0].upper()
    except Exception:
        return "BODY"


def tile_images(
    out: Path,
    tiles: Sequence[tuple[str, Path]],
    columns: int = _COLUMNS,
    tile_w: int = _TILE_W,
) -> Optional[list[str]]:
    """Tile renders into one image with a caption under each. Never raises.

    Returns the captions actually placed, in order, or None when nothing could
    be. Shared with `ai.designqa`, which puts slides on a sheet rather than
    layouts and for a different question -- one picture of the whole deck is
    what a mismatch between two slides is visible in, and twenty separate
    uploads of the same slides would be twenty times the bytes for an answer
    that needs them side by side.

    `columns` and `tile_w` are the caller's because the two uses want different
    shapes, and the ceiling on both is the same: the long edge of the finished
    sheet past 1568px is downsized before anything looks at it, so a wider
    sheet is a smaller tile, not a clearer one.
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415 - lazy

    if not tiles:
        return None
    columns = max(1, min(columns, len(tiles)))
    tile_h = int(tile_w * DEFAULT_HEIGHT / DEFAULT_WIDTH)
    rows = (len(tiles) + columns - 1) // columns
    cell_w = tile_w + 2 * _PAD
    cell_h = tile_h + _CAPTION_H + 2 * _PAD
    sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)

    placed: list[str] = []
    for i, (name, path) in enumerate(tiles):
        x = (i % columns) * cell_w + _PAD
        y = (i // columns) * cell_h + _PAD
        try:
            with Image.open(path) as image:
                sheet.paste(image.convert("RGB").resize((tile_w, tile_h)), (x, y))
        except Exception:
            log.debug("could not place the render of %r", name, exc_info=True)
            continue
        # A border, because a tile that is white to its edge runs into its
        # neighbour without one.
        draw.rectangle([x, y, x + tile_w - 1, y + tile_h - 1], outline=(0, 0, 0))
        draw.text((x + 2, y + tile_h + 6), f"{len(placed) + 1}. {name}", fill=(0, 0, 0))
        placed.append(name)

    if not placed:
        return None
    sheet.save(out, "PNG")
    return placed


def _compose(
    out: Path, tiles: Sequence[tuple[str, Path]]
) -> Optional[LayoutSheet]:
    """Tile the renders into one image, a number and a name under each."""
    placed = tile_images(out, tiles)
    if not placed:
        return None
    return LayoutSheet(path=out, numbers={name: i + 1 for i, name in enumerate(placed)})
