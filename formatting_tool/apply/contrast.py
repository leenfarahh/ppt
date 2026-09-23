"""Make the copy readable off what it ends up sitting on. Last, after colour.

WHY IT RUNS HERE AND NOT AS A FIXER. `rules.contrast` measures the deck it was
handed and `fixers.fix_text_contrast` corrects the pairings it named, and both
are right about the file as it stood WHEN THE RULES RAN. What neither can see
is the pairing the run itself created: a card is snapped from a pale tint to a
mid brand blue, the white label on it was fine before and is now at 2.1:1, and
no finding exists for it because the colour it is measured against did not
exist when anything was measured.

That is not a gap in the rules. It is the ordering: the last thing to change a
colour has to be the last thing to check one. `colorplan._keeps_text_legible`
already refuses an entry that would break a label it knows about, and it knows
about a fill's own runs and nothing else -- not a caption sitting on a card it
does not own, not a heading over a band, not a cell's copy against a shade the
palette sweep moved. This is the pass that measures the deck as it finally
stands.

WHAT IT CHANGES IS THE TEXT, NEVER THE FILL. By the time this runs every
colour in the deck is on the palette, and moving a fill would put it back to
being a question about the brand. The text is the half with a free choice:
every master writes words in more than one colour, and picking the one that
can be read on the fill underneath is a decision with an answer.

WHAT IT REFUSES TO ANSWER is the same list `rules.contrast` refuses, and for
the same reason -- a photograph, a gradient, a pattern and an inherited fill
have no one colour to measure against, and reporting the slide behind a
photograph would give the worst slide in a deck a clean bill of health. Those
stay with the model, which is looking at a picture.

Never fatal. A deck this cannot read comes back unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..colorutil import contrast_ratio, delta_e

log = logging.getLogger(__name__)

# WCAG AA, and both of its numbers. 4.5:1 for body copy, 3.0:1 for large text,
# where large is 18pt or 14pt bold. The same figures `rules.contrast` reports
# against, so a deck this pass leaves alone is one that rule finds nothing on.
_BODY_FLOOR = 4.5
_LARGE_FLOOR = 3.0
_LARGE_PT = 18.0
_LARGE_BOLD_PT = 14.0

# A run that states no size is held to the body floor: its size is inherited
# and could be anything, and the two ways of being wrong are not worth the
# same. Holding a 24pt heading to 4.5:1 costs a colour nobody minds; letting a
# 9pt caption through at 3.2:1 costs the thing this pass exists for.
_UNSTATED_FLOOR = _BODY_FLOOR

# A fill whose colour is the colour drawn. Everything else -- a gradient, a
# picture fill, a pattern, a shape that states no fill at all -- is a colour
# this cannot name, and a pairing it cannot name is one it must not guess at.
_KNOWN_FILL = frozenset({"SOLID"})

# How far outside a shape another may sit and still be sitting ON it. A label
# drawn flush to the edge of its card is on the card, and two boxes agreeing
# to the thousandth of an inch is not something a deck built by hand does.
_EDGE_SLACK = 18288                     # 0.02in in EMU

# Placeholders whose colour is the master's business. A page number set in a
# deliberate pale grey is furniture, and recolouring it on every slide of a
# ninety slide deck is the tool overruling the brand.
_CHROME = ("FOOTER", "SLIDE_NUMBER", "DATE")


@dataclass
class ContrastResult:
    """What this pass recoloured, and what it could not."""

    recoloured: int = 0
    pairs: dict[str, str] = field(default_factory=dict)      # ink -> new ink
    unresolved: list[str] = field(default_factory=list)      # for the report
    reason: str = ""

    @property
    def applied(self) -> bool:
        return self.recoloured > 0

    def line(self) -> str:
        if not self.applied:
            return self.reason or "every pairing in the deck is readable"
        moved = ", ".join(
            f"#{was} -> #{now}" for was, now in sorted(self.pairs.items())
        )
        return (
            f"recoloured {self.recoloured} run(s) so the copy can be read off "
            f"what it sits on ({moved})"
        )


def enforce_contrast(
    path: Path, inks: dict[str, str], tolerance: float = 3.0
) -> ContrastResult:
    """Recolour unreadable copy in `path` to a colour from `inks`.

    `inks` is the set this may choose from, labelled: the colours the MASTER
    WRITES WORDS IN, as `rules.colors.master_text_colors` reads them. Narrower
    than the palette on purpose and for the reason that function gives at
    length -- a theme is twelve swatches, most of them meant for fills, and
    picking a text colour out of all twelve is how a heading ends up in an
    accent no layout in the master sets type in. Legible and wrong is not a
    correction.

    `tolerance` is unused for the choice and kept for the caller's sake: the
    choice is made on contrast first and colour distance second, which needs
    no tolerance. It is here so this signature matches the rest of the colour
    stages and a caller cannot pass the palette's tolerance to the wrong one.

    Rewrites the file only when something changed. Never raises.
    """
    result = ContrastResult()
    choices = {
        label: str(value).strip().lstrip("#").upper()
        for label, value in (inks or {}).items()
        if value and len(str(value).strip().lstrip("#")) == 6
    }
    if not choices:
        result.reason = (
            "the master states no colour it writes text in, so there is "
            "nothing to move unreadable copy to"
        )
        return result

    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    try:
        presentation = Presentation(str(path))
    except Exception:
        log.warning("could not open %s to check its contrast", path.name,
                    exc_info=True)
        result.reason = "the deck could not be opened to check its contrast"
        return result

    background = _deck_background(presentation)
    for number, slide in enumerate(presentation.slides, start=1):
        ordered = list(_walk(_safely(lambda s=slide: list(s.shapes)) or []))
        for index, shape in enumerate(ordered):
            if _is_chrome(shape):
                continue
            behind = _behind(shape, ordered, index, background)
            # A TABLE IS ASKED SEPARATELY AND WITHOUT THE GUARD ABOVE. Its
            # copy is on its cells and so is the fill under that copy, so the
            # frame having nothing behind it -- which is the ordinary case,
            # since a graphic frame has no fill and a blank layout names no
            # background -- says nothing at all about whether the table's
            # header row can be read.
            _fix_table(shape, behind, choices, result, number)
            if not behind:
                continue
            _fix_shape(shape, behind, choices, result, number)

    if not result.applied:
        return result
    try:
        presentation.save(str(path))
    except Exception:
        log.warning("could not write the recoloured copy back to %s",
                    path.name, exc_info=True)
        return ContrastResult(reason="the recoloured deck could not be written")
    log.info("%s: %s", path.name, result.line())
    return result


def _fix_shape(
    shape: Any,
    behind: str,
    choices: dict[str, str],
    result: ContrastResult,
    number: int,
) -> None:
    """Recolour the runs on one shape that cannot be read off `behind`."""
    if not _safely(lambda: shape.has_text_frame):
        return
    for paragraph in _safely(lambda: list(shape.text_frame.paragraphs)) or []:
        for run in _safely(lambda p=paragraph: list(p.runs)) or []:
            _fix_run(run, behind, choices, result, number, shape)


def _fix_table(
    shape: Any,
    behind: Optional[str],
    choices: dict[str, str],
    result: ContrastResult,
    number: int,
) -> None:
    """The same for a table, whose copy is on its cells rather than on it.

    A TABLE IS NOT A SHAPE WITH PARAGRAPHS ON IT, which is why every pass that
    walks `text_frame` sees a table as an empty graphic frame. Its header row
    filled in the brand colour with white type on it is the single commonest
    place this defect lives on a client deck, and the cell states its own fill
    -- so the background here is the cell's, not the frame's.
    """
    if not _safely(lambda: shape.has_table):
        return
    for row in _safely(lambda: list(shape.table.rows)) or []:
        for cell in _safely(lambda r=row: list(r.cells)) or []:
            if _safely(lambda c=cell: c.is_spanned):
                continue
            under = _fill_of(cell) or behind
            if not under:
                # A cell that states no fill takes the table style's, which a
                # banded style paints per row and says nowhere this can read.
                # Measuring it against whatever is behind the table would be
                # a confident answer about the wrong pairing.
                continue
            for paragraph in _safely(
                lambda c=cell: list(c.text_frame.paragraphs)
            ) or []:
                for run in _safely(lambda p=paragraph: list(p.runs)) or []:
                    _fix_run(run, under, choices, result, number, shape)


def _fix_run(
    run: Any,
    behind: str,
    choices: dict[str, str],
    result: ContrastResult,
    number: int,
    shape: Any,
) -> None:
    """One run, measured and moved or left alone.

    A RUN THAT STATES NO COLOUR IS LEFT ALONE. It is inheriting from the
    master's placeholder, which is the master having already decided what
    colour words in that slot are -- and writing a literal onto the run would
    freeze that decision into the file, so a master that changed later would
    no longer reach it. The pairings that matter here are the ones somebody
    typed a colour into.
    """
    if not str(_safely(lambda: run.text) or "").strip():
        return
    ink = _run_hex(run)
    if not ink:
        return
    floor = _floor_for(run)
    ratio = contrast_ratio(ink, behind)
    if ratio is None or ratio >= floor:
        return

    target = _legible(ink, behind, floor, choices)
    if target is None:
        result.unresolved.append(
            f"slide {number}: #{ink} on #{behind} reads at {ratio:.1f}:1 and "
            f"no colour this master writes text in clears {floor:g}:1 there -- "
            f"move the copy off this fill or recolour the fill"
        )
        return
    if target == ink:
        return
    from pptx.dml.color import RGBColor  # noqa: PLC0415 - lazy heavy dependency

    try:
        run.font.color.rgb = RGBColor.from_string(target)
    except Exception:
        log.debug("could not recolour a run on slide %d", number, exc_info=True)
        return
    result.recoloured += 1
    result.pairs[ink] = target


def _legible(
    ink: str, behind: str, floor: float, choices: dict[str, str]
) -> Optional[str]:
    """The colour to set this run to, or None when the master offers none.

    THE NEAREST ONE THAT CLEARS THE FLOOR, not the highest contrast available.
    The correction should be the smallest visible change that makes the copy
    readable: a caption in a pale grey that has to move becomes the darkest
    grey the master sets on text, not black. Same rule as
    `rules.contrast._legible_ink`, so the pass that reports and the pass that
    corrects cannot disagree about what the answer is.

    None is a real outcome. A master that writes in three mid-tones has
    nothing readable on a mid-tone card, and the answer there is to move the
    copy or recolour the card -- both a designer's call, and both said in
    `ContrastResult.unresolved`.
    """
    best: Optional[tuple[float, str]] = None
    for value in choices.values():
        ratio = contrast_ratio(value, behind)
        if ratio is None or ratio < floor:
            continue
        distance = delta_e(ink, value)
        if distance is None:
            continue
        if best is None or distance < best[0]:
            best = (distance, value)
    return best[1] if best else None


def _floor_for(run: Any) -> float:
    """The ratio this run has to clear, by WCAG's definition of large text."""
    size = _safely(lambda: run.font.size)
    points = None
    if size is not None:
        points = _safely(lambda: size.pt)
    if points is None:
        return _UNSTATED_FLOOR
    bold = bool(_safely(lambda: run.font.bold))
    if points >= _LARGE_PT or (bold and points >= _LARGE_BOLD_PT):
        return _LARGE_FLOOR
    return _BODY_FLOOR


def _behind(
    shape: Any, ordered: list, index: int, background: Optional[str]
) -> Optional[str]:
    """The colour drawn under this shape's text, or None when it is not one.

    Three answers in order, and the order is what a reader's eye does: the
    shape's own fill, then the nearest filled shape drawn before it that
    contains it, then the slide's background.

    ANYTHING DRAWN THAT IS NOT ONE COLOUR ENDS THE SEARCH WITH None rather
    than being stepped over. A picture is the case this most wants to report
    and is least able to -- reporting the white slide BEHIND a photograph
    would call the worst slide in the deck clean -- and a gradient card is the
    same lie by a quieter route.
    """
    own = _fill_of(shape)
    if own:
        return own
    if _opaque_unknown(shape):
        return None

    for candidate in reversed(ordered[:index]):
        if candidate is shape or not _contains(candidate, shape):
            continue
        if _opaque_unknown(candidate):
            return None
        fill = _fill_of(candidate)
        if fill:
            return fill
    return background


def _fill_of(shape: Any) -> Optional[str]:
    """A shape's or a cell's own solid fill, as six hex digits, or None."""
    try:
        fill = shape.fill
        if str(fill.type or "").split(".")[-1].split(" ")[0].upper() not in _KNOWN_FILL:
            return None
        rgb = fill.fore_color.rgb
    except Exception:
        return None
    return str(rgb).upper() if rgb is not None else None


def _opaque_unknown(shape: Any) -> bool:
    """Whether this shape is drawn and is not one nameable colour."""
    if _safely(lambda: shape.shape_type is not None and "PICTURE" in str(
        shape.shape_type
    ).upper()):
        return True
    kind = _safely(lambda: str(shape.fill.type or "").upper())
    if kind is None:
        return False
    return any(
        token in kind
        for token in ("GRADIENT", "PICTURE", "PATTERNED", "TEXTURED")
    )


def _contains(outer: Any, inner: Any) -> bool:
    a, b = _box(outer), _box(inner)
    if a is None or b is None or a[2] <= 0 or a[3] <= 0:
        return False
    return (
        a[0] - _EDGE_SLACK <= b[0]
        and a[1] - _EDGE_SLACK <= b[1]
        and a[0] + a[2] + _EDGE_SLACK >= b[0] + b[2]
        and a[1] + a[3] + _EDGE_SLACK >= b[1] + b[3]
    )


def _box(shape: Any) -> Optional[tuple[int, int, int, int]]:
    try:
        values = (shape.left, shape.top, shape.width, shape.height)
    except Exception:
        return None
    if any(value is None for value in values):
        return None
    return tuple(int(value) for value in values)      # type: ignore[return-value]


def _run_hex(run: Any) -> Optional[str]:
    try:
        colour = run.font.color
        if colour is None or colour.type is None or colour.rgb is None:
            return None
    except Exception:
        return None
    return str(colour.rgb).upper()


def _is_chrome(shape: Any) -> bool:
    try:
        if not shape.is_placeholder:
            return False
        token = str(shape.placeholder_format.type or "").split("(")[0].strip().upper()
    except Exception:
        return False
    return token in _CHROME


def _deck_background(presentation: Any) -> Optional[str]:
    """The colour the deck's own master paints behind everything, or None.

    Read off the master rather than off each slide: python-pptx does not model
    a slide's inherited background, and a deck that has just been restyled
    onto one master has one answer for the whole file. None where the master
    states a gradient or a picture, which is the honest answer -- a pairing
    against something that is not one colour is one this pass will not judge.
    """
    for master in _safely(lambda: list(presentation.slide_masters)) or []:
        element = getattr(master, "_element", None)
        if element is None:
            continue
        found = element.findall(
            ".//{http://schemas.openxmlformats.org/drawingml/2006/main}srgbClr"
        )
        background = element.find(
            "{http://schemas.openxmlformats.org/presentationml/2006/main}cSld/"
            "{http://schemas.openxmlformats.org/presentationml/2006/main}bg"
        )
        if background is None:
            continue
        found = background.findall(
            ".//{http://schemas.openxmlformats.org/drawingml/2006/main}srgbClr"
        )
        if len(found) == 1:
            value = str(found[0].get("val") or "").strip().upper()
            if len(value) == 6:
                return value
    return None


def _walk(shapes: Iterable[Any]) -> Iterable[Any]:
    for shape in shapes:
        yield shape
        children = _safely(lambda s=shape: list(s.shapes))
        if children:
            yield from _walk(children)


def _safely(call):
    try:
        return call()
    except Exception:
        return None
