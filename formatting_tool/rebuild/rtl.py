"""Turning a rebuilt deck round, for copy that reads right to left.

Applying an English master to an Arabic deck and stopping there gets the type
and the colour right and the layout backwards. The master puts its title at
0.92in from the left because that is where an English reader starts; an Arabic
reader starts at the other side, so the title belongs at 0.92in from the
right, the logo moves across, and a row of cards that reads 1-2-3 left to
right has to read 1-2-3 right to left.

None of that is a matter of taste. A right-to-left deck laid out left to right
reads as a deck somebody forgot to finish.

WHAT IS MIRRORED IS THE FRAME, NOT THE CONTENT, and getting that the wrong
way round is the first thing this did. Mirroring every shape on the slide
moved an Arabic deck's own copy -- already set flush right by whoever wrote it
-- over to the left, while correctly bringing the master's title across from
the left. The deck was right and the master was wrong, so turning both round
fixed one and broke the other.

So: layouts, the slide master, and the placeholders on a slide are the frame,
and they are what turns. A shape the designer placed themselves is content,
and it stays where they put it -- an Arabic deck's author has already decided
which side their copy sits on, and the rebuild has no business arguing.

Nothing inside a shape is mirrored either. A photograph is not flipped -- a
mirrored face is a different photograph, and a mirrored chart is a wrong chart
-- and neither is the arrangement inside a group, because a group is one drawn
object and whoever drew it decided how its parts sit. Mirroring its position
moves the object; mirroring its insides would redraw it.

Paragraph alignment turns with the page. Flush left becomes flush right and
the other way round; centred and justified stay as they are, having no side.

Two implementations, one per rebuild route, and for the reason `master_apply`
exists at all: that route deliberately re-serialises nothing, so its mirror
goes through automation rather than through the file. The XML route already
rewrites the file, so its mirror is part of that.
"""

from __future__ import annotations

import logging
from typing import Any, Collection

log = logging.getLogger(__name__)

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
EMU_PER_POINT = 12700

# python-pptx alignment names, and what each becomes when the page turns
# round. Absent from the map means it has no side: CENTER reads the same
# either way, and JUSTIFY justifies to whichever direction the paragraph runs.
_FLIP = {"LEFT": "RIGHT", "RIGHT": "LEFT"}


# --------------------------------------------------------------------------- #
# The XML route
# --------------------------------------------------------------------------- #

def mirror_presentation(presentation: Any) -> int:
    """Turn an open python-pptx presentation's frame round. Best effort.

    Returns the number of shapes moved.
    """
    width = int(presentation.slide_width or 0)
    if width <= 0:
        return 0

    moved = 0
    # The frame itself: every layout and every master. Once these are round,
    # a placeholder that inherits its position is already correct and nothing
    # on the slide needs touching for it.
    for master in _each(presentation.slide_masters):
        moved += _mirror_container(master, width)
        for layout in _each(master.slide_layouts):
            moved += _mirror_container(layout, width)

    for slide in _each(presentation.slides):
        moved += mirror_slide(slide, width)
    return moved


def mirror_slide(slide: Any, width_emu: int) -> int:
    """Turn round the placeholders that carry their own position.

    Placeholders only, because everything else on the slide is content
    somebody placed. And only the ones with an `a:xfrm` of their own, because
    turning a placeholder that INHERITS its position turns it a second time:
    the layout has already moved, the placeholder reads the layout's new
    position, and mirroring that puts it back where it started. That is
    exactly what happened -- a title correctly moved to the right by the
    layout came back to the left by the slide.
    """
    moved = 0
    for shape in _top_level(slide):
        if (
            _is_placeholder(shape)
            and _has_own_position(shape)
            and _mirror_shape(shape, width_emu)
        ):
            moved += 1
        _turn_text(shape)
    return moved


def _has_own_position(shape: Any) -> bool:
    """Whether this shape states where it sits, rather than inheriting it.

    python-pptx reports the effective value either way, which is what makes
    this necessary: `shape.left` cannot tell you whether the number came from
    the shape or from the layout behind it, and the answer decides whether
    moving it is a correction or a second mirror.
    """
    try:
        properties = shape._element.spPr
        return properties is not None and properties.xfrm is not None
    except Exception:
        return False


def _mirror_container(container: Any, width_emu: int) -> int:
    """Mirror a layout or a master: all of it is frame, so all of it turns.

    Except what inherits. A layout placeholder that states no position of its
    own follows the master, and the master has already turned -- moving it as
    well turns it twice and puts it back. The rule is the same one the slides
    use: turn what states its own position, and let everything else follow.
    """
    moved = 0
    for shape in _top_level(container):
        if _has_own_position(shape) and _mirror_shape(shape, width_emu):
            moved += 1
        _turn_text(shape)
    return moved


def _is_placeholder(shape: Any) -> bool:
    try:
        return bool(shape.is_placeholder)
    except Exception:
        return False


def _each(collection: Any) -> list:
    try:
        return list(collection)
    except Exception:
        return []


def _top_level(slide: Any) -> list[Any]:
    try:
        return list(slide.shapes)
    except Exception:
        return []


def _mirror_shape(shape: Any, width_emu: int) -> bool:
    """Put a shape the same distance from the other edge.

    Left and width only. A mirror about the vertical centre changes nothing
    about how high a shape sits or how tall it is, and touching those would be
    a second edit hiding inside this one.
    """
    try:
        left, top = shape.left, shape.top
        box, height = shape.width, shape.height
        if left is None or box is None:
            return False
        # All four written, not just the one that changes. Assigning `left`
        # alone to a shape with no `a:xfrm` of its own makes python-pptx
        # create one -- and the three values nobody set come out as zero, so
        # the shape jumps to the top of the slide. Reading them first and
        # writing them back keeps the mirror to one axis.
        shape.left = width_emu - (left + box)
        if top is not None:
            shape.top = top
        shape.width = box
        if height is not None:
            shape.height = height
        return True
    except Exception:
        log.debug("could not mirror a shape", exc_info=True)
        return False


def _turn_text(shape: Any) -> None:
    """Flush left becomes flush right, and Arabic paragraphs are marked."""
    try:
        if not shape.has_text_frame:
            return
    except Exception:
        return

    from ..script import is_rtl  # noqa: PLC0415 - avoids an import cycle

    try:
        for paragraph in shape.text_frame.paragraphs:
            _flip_alignment(paragraph)
            if is_rtl(paragraph.text or ""):
                paragraph._p.get_or_add_pPr().set("rtl", "1")
    except Exception:
        log.debug("could not turn a paragraph round", exc_info=True)


def _flip_alignment(paragraph: Any) -> None:
    from pptx.enum.text import PP_ALIGN  # noqa: PLC0415 - lazy

    try:
        current = paragraph.alignment
    except Exception:
        return
    if current is None:
        # Says nothing, so it inherits, and what it inherits is now correct:
        # a right-to-left paragraph inherits flush right. Setting it here
        # would bake in a value the layout is entitled to change.
        return
    wanted = _FLIP.get(str(current).split(" ")[0].upper())
    if wanted is None:
        return
    try:
        paragraph.alignment = getattr(PP_ALIGN, wanted)
    except Exception:
        log.debug("could not flip a paragraph's alignment", exc_info=True)


# --------------------------------------------------------------------------- #
# The PowerPoint route
# --------------------------------------------------------------------------- #

def mirror_com(presentation: Any, skip: Collection[int] = ()) -> int:
    """Turn the frame round through automation, writing no file.

    Separate from the python-pptx version because `master_apply` is built
    around serialising nothing: handing a PowerPoint-written deck back to
    python-pptx to rewrite is the class of failure that module exists to
    avoid. COM measures in points, which is the only other difference.

    `skip` is the slides that never got the master -- see
    `rebuild.quarantine`. They MUST be skipped, and for the reason this module
    opens with: the mirror exists because the MASTER is drawn for the other
    reading direction, and a quarantined slide is not on the master. It is on
    the design its author built it in, already reading the way they meant it
    to. Turning it round is precisely the mistake described above -- moving a
    deck's own copy off the side the author put it on -- done to the one slide
    that was promised it would be left exactly as it arrived.
    """
    try:
        width = float(presentation.PageSetup.SlideWidth)
    except Exception:
        log.debug("could not read the slide width", exc_info=True)
        return 0

    moved = 0
    try:
        count = int(presentation.Slides.Count)
    except Exception:
        return 0

    # The slides' placeholders, and not the layouts behind them. COM reports
    # the effective position of every shape and will not say whether it came
    # from the shape or from the layout, so turning both would turn an
    # inheriting placeholder twice and put it back where it started. One of
    # the two has to be chosen, and the slides are the ones a reader sees.
    #
    # The cost is that the layouts in the output still read left to right, so
    # a designer opening the master view sees an English frame behind Arabic
    # slides. The XML route, which can tell the two apart, turns both.
    for index in range(1, count + 1):
        if index in skip:
            continue
        try:
            slide = presentation.Slides(index)
            shapes = int(slide.Shapes.Count)
        except Exception:
            continue
        for i in range(1, shapes + 1):
            try:
                shape = slide.Shapes(i)
            except Exception:
                continue
            # Placeholders only on a slide; see the module docstring.
            try:
                if int(shape.Type) == _MSO_PLACEHOLDER:
                    shape.Left = width - (float(shape.Left) + float(shape.Width))
                    moved += 1
            except Exception:
                log.debug("could not mirror a shape through COM", exc_info=True)
            _turn_text_com(shape)
    return moved


def _turn_text_com(shape: Any) -> None:
    """Flush left becomes flush right, and Arabic paragraphs are marked.

    The COM half of `_turn_text`, and it was missing: `mirror_com` called this
    and nothing defined it, so the first shape of the first slide raised
    NameError, `apply_master` caught it as fatal, and EVERY right-to-left deck
    fell back to the XML route without saying so. The fallback is the louder
    half of the bug -- that route drops what it cannot recreate -- and the
    silence is the rest of it.

    WHERE IT DIFFERS FROM `_turn_text`, honestly rather than by accident. The
    python-pptx version leaves an alignment of None alone, because None means
    the paragraph inherits and what it inherits is already correct; writing a
    value there would bake in something the layout is entitled to change. COM
    has no way to say "inherits" -- it reports the effective alignment -- so
    that distinction cannot be made here and a paragraph that was inheriting
    comes out with an explicit value. That is the same trade the rest of this
    route makes, for the same reason: COM reports what is drawn, not where it
    came from.
    """
    from ..script import is_rtl  # noqa: PLC0415 - avoids an import cycle

    try:
        if not int(shape.HasTextFrame):
            return
        paragraphs = shape.TextFrame.TextRange.Paragraphs()
        count = int(paragraphs.Count)
    except Exception:
        return

    for i in range(1, count + 1):
        try:
            paragraph = shape.TextFrame.TextRange.Paragraphs(i)
        except Exception:
            continue
        try:
            current = int(paragraph.ParagraphFormat.Alignment)
            wanted = _FLIP_COM.get(current)
            if wanted is not None:
                paragraph.ParagraphFormat.Alignment = wanted
        except Exception:
            log.debug("could not flip a paragraph through COM", exc_info=True)
        try:
            if is_rtl(str(paragraph.Text) or ""):
                # On TextFrame2, which is where PowerPoint keeps it; TextFrame
                # has no vocabulary for reading direction at all.
                shape.TextFrame2.TextRange.Paragraphs(i).ParagraphFormat \
                    .RightToLeft = _MSO_TRUE
        except Exception:
            log.debug("could not mark a paragraph right to left", exc_info=True)


# msoPlaceholder
_MSO_PLACEHOLDER = 14

# ppAlignLeft / ppAlignRight, and what each becomes when the page turns round.
# Centre (2) and justify (4) are absent for the reason `_FLIP` gives: neither
# has a side. Mixed (-2) is absent because a run of paragraphs aligned several
# ways cannot be flipped as one.
_FLIP_COM = {1: 3, 3: 1}

# msoTrue. Spelled out rather than imported, like every other COM constant in
# this package, so it works without the type library generated.
_MSO_TRUE = -1
