"""The last pass: no colour left in the deck that is not on the palette.

WHY A SWEEP AND NOT MORE FIXERS. `rules.colors` reports a colour and
`apply.fixers` corrects it, and between them they reach a great deal: a shape's
fill, its outline, a run of text, a table's cells, the colours inside an SVG
icon. What that pairing cannot reach is everything it has no finding for, and
on a real deck that is most of what is left:

    a gradient's stops             `fill_hex` is None for a gradient, so no
                                   rule ever names its colours
    a shadow, a glow, an outer     `a:effectLst` carries colours nothing reads
    a connector's line end
    a bullet's colour              `a:buChar/a:buClr`
    a cell's borders               `a:lnL`, `a:lnR`, ... on the cell
    a hyperlink's colour
    a colour on a shape a finding  a run the rule skipped, a fill inside a
    was refused for                group nothing walked

Each one of those could be its own rule and its own fixer, and the list would
never be finished -- every version of the format adds another place a colour
can sit. So this asks the only question that has a complete answer: what does
the FILE say, everywhere, and is every one of those values on the palette.

HOW. Every `a:srgbClr/@val` in every slide part, plus the colours inside the
SVG parts the icons draw from. A literal RGB is the only way a colour can be
off the palette at all -- a theme-bound colour resolves through the master's
theme and is on the palette by construction -- so sweeping the literals is
sweeping everything.

WHAT IT WILL NOT TOUCH.

  - A colour already on the palette, within tolerance. Snapping a value that
    is a hundredth off would rewrite a file for no visible change.
  - Black and white, which are the two values a deck uses for reasons that
    have nothing to do with a brand: a shadow is black at 40% alpha, a
    highlight is white, and a table's default borders are black. Snapping
    those to the nearest brand entry is how a drop shadow comes out maroon.
    They are swept only where the palette itself holds a near-black or a
    near-white to snap them to, which is what most brand palettes do.
  - Anything inside a picture. A photograph is not a colour decision.
  - Anything inside a chart. A chart is one element and crosses the restyle
    exactly as it arrived: its series colours are how a reader tells one line
    from another, and snapping each to the nearest brand entry landed two
    series on one colour and turned bars the colour of the plot behind them.
    See `rebuild.charts`.

The plan decides where a colour goes wherever it has an opinion (see
`apply.colorplan`), so a colour this sweep meets for the first time on slide 9
lands where the same colour landed on slide 2. Everything else takes the
nearest entry.

Never fatal. A file this cannot read or write comes back with nothing changed
and says so in the log; the deck is then exactly what the fixers made of it,
which is what it was before this existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..colorutil import delta_e, nearest_palette_entry

log = logging.getLogger(__name__)

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SRGB = f"{_A_NS}srgbClr"

# The two colours a deck states for reasons that are not brand decisions: a
# shadow is black, a highlight is white, and a table's default rules are
# black. Swept only when the palette has something near enough to stand in --
# see `_target_for`.
_NEUTRALS = {"000000", "FFFFFF"}

# How close a neutral's replacement has to be for the swap to be an
# improvement rather than a repaint. A brand's "near black" at 4 delta-E from
# true black is the colour the deck meant; an entry 30 away is a different
# colour and a shadow drawn in it is a mistake.
_NEUTRAL_LIMIT = 12.0

@dataclass
class SweepResult:
    """What the sweep changed, in the terms the report and the log need."""

    changed: int = 0                     # colour statements rewritten
    colors: dict[str, str] = field(default_factory=dict)   # from -> to
    icons: int = 0                       # statements rewritten inside SVGs
    reason: str = ""                     # why nothing happened, when nothing did
    # Related icons given one scheme after the sweep. See `apply.icons`.
    matched_icons: int = 0
    matched: list[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return self.changed > 0 or self.icons > 0 or self.matched_icons > 0

    def line(self) -> str:
        if not self.applied:
            return self.reason or "every colour in the deck was already on the palette"
        moved = ", ".join(
            f"#{was} -> #{now}" for was, now in sorted(self.colors.items())
        )
        swept = (
            f"put {self.changed} colour statement(s) and {self.icons} icon "
            f"statement(s) onto the palette ({moved})"
        )
        if self.matched_icons:
            swept += (
                f"; then matched {self.matched_icons} icon(s) to the related "
                "icons beside them"
            )
        return swept


def sweep_to_palette(
    path: Path,
    palette: dict[str, str],
    tolerance: float,
    plan: Optional[Any] = None,
) -> SweepResult:
    """Snap every literal colour in `path` onto `palette`. Rewrites the file.

    `tolerance` is the delta-E below which a colour already reads as a palette
    entry and is left alone. `plan` is `apply.colorplan.ColorPlan`, consulted
    first wherever it has a decision, so a colour the fixers moved to one
    entry does not land somewhere else here.

    Best effort throughout. A part that cannot be parsed is stepped over with
    the rest still swept, and a file that cannot be opened at all comes back
    unchanged.
    """
    result = SweepResult()
    if not palette:
        result.reason = "there is no palette to sweep onto"
        return result

    entries = {
        label: str(value).strip().lstrip("#").upper()
        for label, value in palette.items()
        if value and len(str(value).strip().lstrip("#")) == 6
    }
    if not entries:
        result.reason = "the palette holds no colour this could snap to"
        return result

    from pptx import Presentation  # noqa: PLC0415 - lazy heavy dependency

    try:
        presentation = Presentation(str(path))
    except Exception:
        log.warning("could not open %s to sweep its colours", path.name,
                    exc_info=True)
        result.reason = "the deck could not be opened to sweep its colours"
        return result

    # Decided once per colour and reused, so the same value cannot land in two
    # places on two slides -- which is the whole reason the colour plan exists
    # and the one property a per-part sweep would quietly lose.
    decided: dict[str, Optional[str]] = {}

    def target_for(value: str) -> Optional[str]:
        if value not in decided:
            decided[value] = _target_for(value, entries, tolerance, plan)
        return decided[value]

    # Which icons belong together, read before anything moves: the one that
    # leads a set is the one whose own colours were already the brand's.
    from .icons import apply_harmony, plan_harmony  # noqa: PLC0415

    harmony = _safely(lambda: plan_harmony(presentation, entries)) or []

    for part in _swept_parts(presentation):
        result.changed += _sweep_element(
            part, target_for, result.colors, entries
        )
    result.icons += _sweep_icons(presentation, target_for, result.colors)

    # Then the related icons take the leader's colours as the sweep left them,
    # so every icon is on the palette AND a set reads as one set.
    matched = _safely(lambda: apply_harmony(harmony))
    if matched is not None:
        result.matched_icons = matched.icons
        result.matched = matched.lines
        for line in matched.lines:
            log.info("%s: %s", path.name, line)

    if not result.applied:
        return result
    try:
        presentation.save(str(path))
    except Exception:
        log.warning("could not write the swept colours back to %s", path.name,
                    exc_info=True)
        return SweepResult(reason="the swept deck could not be written")
    log.info("%s: %s", path.name, result.line())
    return result


def _swept_parts(presentation: Any) -> Iterable[Any]:
    """The XML roots whose colours are the deck's own to change.

    Slides, and deliberately not layouts, masters or the theme: those came
    from the approved file, and rewriting them would move the brand to match
    the palette that was read off it.

    The slide's own tree and anything it points at that carries drawing
    colours -- a diagram's data -- because those are not on any shape. NOT a
    chart's parts; see the module docstring.
    """
    for slide in _safely(lambda: list(presentation.slides)) or []:
        element = getattr(slide, "_element", None)
        if element is not None:
            yield element
        for related in _related_parts(slide):
            yield related


def _related_parts(slide: Any) -> Iterable[Any]:
    """The XML of the parts this slide's shapes draw from.

    A SmartArt diagram's colours live in its data and drawing parts, as
    `a:srgbClr` in the same namespace as everything else, so once the element
    is in hand the sweep is identical -- what differs is only that nothing on
    the slide names them. Chart parts are deliberately not among them.
    """
    try:
        related = slide.part.rels
    except Exception:
        return
    for rel in _safely(lambda: list(related.values())) or []:
        if _safely(lambda r=rel: r.is_external):
            continue
        part = _safely(lambda r=rel: r.target_part)
        name = str(_safely(lambda p=part: p.partname) or "")
        if "/diagrams/" not in name:
            continue
        element = getattr(part, "_element", None)
        if element is not None:
            yield element


# The parents whose child colours are a SET and have to stay one. A gradient
# is the case that makes this necessary rather than nice: its two stops are
# routinely nearest to one palette entry, and snapping each on its own turns
# a band into a flat rectangle -- visible at a glance, and the sweep's doing.
_A_GS_LST = f"{_A_NS}gsLst"

_GROUPED = (_A_GS_LST,)


def _sweep_element(
    root: Any, target_for, moved: dict[str, str], entries: dict[str, str]
) -> int:
    """Rewrite every `a:srgbClr/@val` under this root. Returns how many.

    THE SETS FIRST, then everything else. A gradient's stops are colours
    whose whole job is to differ from each other, and taking the nearest
    entry for each of them independently is how a two-stop gradient comes out
    as one flat colour. They are assigned one to one, the
    same way `apply.colorplan` assigns the deck's colours, and the rest of the
    file is swept a value at a time because there is nothing relating those.
    """
    changed = 0
    try:
        nodes = list(root.iter(_SRGB))
    except Exception:
        return 0

    done: set[int] = set()
    for parent in _groups(root):
        members = [n for n in parent.iter(_SRGB) if id(n) not in done]
        by_value: dict[str, list[Any]] = {}
        for node in members:
            value = str(node.get("val") or "").strip().upper()
            if len(value) == 6:
                by_value.setdefault(value, []).append(node)
        if len(by_value) < 2:
            continue
        chosen = _distinct(list(by_value), target_for, entries)
        for value, target in chosen.items():
            if not target or target == value:
                done.update(id(n) for n in by_value[value])
                continue
            for node in by_value[value]:
                node.set("val", target)
                done.add(id(node))
                changed += 1
            moved[value] = target

    for node in nodes:
        if id(node) in done:
            continue
        value = str(node.get("val") or "").strip().upper()
        if len(value) != 6:
            continue
        target = target_for(value)
        if not target or target == value:
            continue
        node.set("val", target)
        moved[value] = target
        changed += 1
    return changed


def _groups(root: Any):
    """The elements whose descendant colours have to stay distinct."""
    for tag in _GROUPED:
        try:
            yield from root.iter(tag)
        except Exception:
            continue


def _distinct(
    values: list[str], target_for, entries: dict[str, str]
) -> dict[str, Optional[str]]:
    """One palette entry each, for colours that have to remain different.

    NEAREST FIRST, so the colour that most clearly IS an entry keeps it and
    the ones merely near it work around that -- the same ordering, and for the
    same reason, as `apply.colorplan.build_color_plan`.

    A colour with no free entry left keeps the one it measured to rather than
    being left off the palette. Two stops of a gradient landing on one entry
    is a worse gradient; a stop left off the palette is the defect this whole
    stage exists to remove, and between the two the palette wins.
    """
    ranked = sorted(
        values,
        key=lambda value: (
            nearest_palette_entry(value, entries)[1] or float("inf"), value
        ),
    )
    out: dict[str, Optional[str]] = {}
    taken: set[str] = set()
    for value in ranked:
        target = target_for(value)
        if not target:
            out[value] = None
            taken.add(value)            # it stays as it is and blocks its own
            continue
        if target not in taken:
            out[value] = target
            taken.add(target)
            continue
        free = _next_free(value, entries, taken)
        out[value] = free or target
        taken.add(free or target)
    return out


def _next_free(
    value: str, entries: dict[str, str], taken: set[str]
) -> Optional[str]:
    """The nearest palette entry to `value` that nothing in the set has."""
    best: Optional[tuple[float, str]] = None
    for entry in entries.values():
        if entry in taken:
            continue
        distance = delta_e(value, entry)
        if distance is None:
            continue
        if best is None or distance < best[0]:
            best = (distance, entry)
    return best[1] if best else None


def _sweep_icons(presentation: Any, target_for, moved: dict[str, str]) -> int:
    """Rewrite the colours inside the SVG parts the deck's icons draw from.

    An icon's colour is not on its shape: PowerPoint calls it a Graphics Fill
    and it lives in the SVG the picture points at, which `svgicon` reads and
    writes. Nothing else in this sweep reaches it, and on a deck of attribute
    cards the icons are most of what a reader sees.

    Swept once per PART rather than once per shape, because the part is shared
    by every copy of the icon -- so recolouring it recolours all of them, and
    walking the shapes would do the same work as many times as the icon
    appears.
    """
    from .. import svgicon  # noqa: PLC0415 - keeps the import off the hot path

    changed = 0
    parts: dict[int, Any] = {}
    for slide in _safely(lambda: list(presentation.slides)) or []:
        for shape in _walk(_safely(lambda s=slide: list(s.shapes)) or []):
            part = _safely(lambda s=shape: svgicon.svg_part(s))
            if part is not None:
                parts.setdefault(id(part), part)

    for part in parts.values():
        try:
            markup = part.blob.decode("utf-8", "ignore")
        except Exception:
            continue
        rewrote = 0
        for colour in _safely(lambda m=markup: svgicon.colors_in(m)) or []:
            # A theme-bound statement inside an SVG resolves through the
            # master's theme, exactly as one on a shape does, so it is on the
            # palette already and rewriting it would hardcode an exception.
            if getattr(colour, "theme", None) or not getattr(colour, "hex", ""):
                continue
            value = str(colour.hex).strip().lstrip("#").upper()
            target = target_for(value)
            if not target or target == value:
                continue
            markup, count = svgicon.recolor(markup, value, target)
            if count:
                moved[value] = target
                rewrote += count
        if not rewrote:
            continue
        try:
            part._blob = markup.encode("utf-8")
        except Exception:
            continue
        changed += rewrote
    return changed


def _walk(shapes: Iterable[Any]) -> Iterable[Any]:
    for shape in shapes:
        yield shape
        children = _safely(lambda s=shape: list(s.shapes))
        if children:
            yield from _walk(children)


def _target_for(
    value: str,
    entries: dict[str, str],
    tolerance: float,
    plan: Optional[Any],
) -> Optional[str]:
    """Where one colour goes, or None to leave it exactly as it is.

    THE PLAN FIRST, WHEREVER IT HAS AN OPINION. It is the only thing that saw
    every off-palette colour in the deck at once, and it mapped them one to
    one so that three steps of a legend stay three steps. A sweep that took
    the nearest entry for each of them independently would collapse exactly
    the distinctions the plan was written to keep.

    THEN THE NEAREST ENTRY, which is the right answer for everything the plan
    never heard of: a gradient stop, a shadow, a bullet. There
    is no finding behind these and so no intended entry to look up; nearest is
    what a designer transposing them would start from.

    None for a colour that is already an entry, and for a black or a white the
    palette has nothing near enough to stand in for -- see `_NEUTRALS`.
    """
    if plan is not None:
        choice = _safely(lambda: plan.choice_for(value))
        if choice is not None and getattr(choice, "target", None):
            return str(choice.target).strip().lstrip("#").upper()

    label, distance = nearest_palette_entry(value, entries)
    if label is None or distance is None:
        return None
    if distance <= tolerance:
        return None                     # already reads as that entry
    if value in _NEUTRALS and distance > _NEUTRAL_LIMIT:
        # A shadow's black and a highlight's white are not brand decisions,
        # and a palette with nothing near them has nothing to say about them.
        return None
    target = entries.get(label)
    if not target:
        return None
    # Never a swap that is no improvement: a value whose nearest entry is
    # further away than the tolerance but which IS that entry to the eye has
    # already been returned above, so anything reaching here moves.
    return target if delta_e(value, target) is not None else None


def _safely(call):
    try:
        return call()
    except Exception:
        return None
