"""One decision about every off-palette colour in the deck, made together.

Snapping colours one at a time is what broke a table of High / Medium / Low
pills. Three fills, three findings, three fixes, each correct on its own: each
colour's nearest palette entry was the same tan, so all three became that tan
and a legend with three steps came out with one. Nothing in the run was wrong
except that nothing in the run was looking at more than one shape.

The fix is not to recognise the legend. Detecting that three swatches sit
beside the words High, Medium and Low, and that a fourth column of pills
reads from them, is a hard problem and the wrong one: a heatmap, a chart's
series, a RAG status column and a two-tone icon all carry meaning in being
different from each other, and each would need its own detector.

What they have in common is the only thing that needs to be true. Distinct
colours must stay distinct. Enforce that and every encoding survives without
the tool ever knowing what it meant -- so this maps colours ONE TO ONE, and
where the palette cannot express a set that finely, it says so and leaves
those colours alone rather than merging them.

Deck-wide, not per slide. The same three pills appear on eleven slides and
must come out the same colour on all of them, and a colour that stays put on
slide 9 still constrains what another colour may become on slide 2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from ..colorutil import (
    delta_e,
    nearest_palette_entry,
    plausible_palette_entries,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColorChoice:
    """What one colour in the deck becomes, and on what grounds."""

    source: str                      # the colour as the deck carries it
    target: Optional[str] = None     # what to recolour it to; None = leave it
    label: Optional[str] = None      # the palette entry's name
    fallback: bool = False           # nearest, not the entry it was meant to be
    reason: str = ""                 # why, in words the outcome line can use

    @property
    def applies(self) -> bool:
        return self.target is not None


class ColorPlan:
    """The whole deck's colour decisions, keyed by the colour as found."""

    def __init__(self, choices: Optional[dict[str, ColorChoice]] = None) -> None:
        self._choices = choices or {}

    def __len__(self) -> int:
        return len(self._choices)

    def choice_for(self, source: Optional[str]) -> Optional[ColorChoice]:
        if not source:
            return None
        return self._choices.get(source.strip().lstrip("#").upper())

    @property
    def declined(self) -> list[ColorChoice]:
        """Colours left alone to keep a set apart, for the run log."""
        return [c for c in self._choices.values() if not c.applies]


def build_color_plan(
    selected: Iterable[str],
    everything: Iterable[str],
    palette: dict[str, str],
    tolerance: float,
    limit: float,
) -> ColorPlan:
    """Decide every selected colour's target at once.

    `selected` are the colours a fix was asked for; `everything` is every
    off-palette colour the report found, selected or not. Both are needed: a
    colour nobody ticked keeps the colour it has, and a shape that stays green
    still means no other shape may be recoloured to that green.

    `tolerance` is the distance below which two colours read as the same one,
    so it is the floor two final colours have to clear. `limit` is how far a
    colour may reasonably be moved, which decides what counts as a candidate.
    """
    sources = {_norm(s) for s in everything if _norm(s)}
    wanted = {_norm(s) for s in selected if _norm(s)} & sources
    if not wanted or not palette:
        return ColorPlan()

    # Candidates per colour, nearest first. A colour with none defensible
    # falls back to its nearest entry however far off it is -- see
    # rules.colors._target -- which is a single candidate flagged as such.
    options: dict[str, list[tuple[str, float, bool]]] = {}
    for source in wanted:
        ranked = [
            (label, distance, False)
            for label, distance in plausible_palette_entries(source, palette, limit)
        ]
        if not ranked:
            label, distance = nearest_palette_entry(source, palette)
            if label is not None and distance is not None:
                ranked = [(label, distance, True)]
        options[source] = ranked

    # Where each colour ends up. A colour not being fixed ends where it is,
    # and that is what stops a fix from colliding with it.
    final: dict[str, str] = {source: source for source in sources}
    choices: dict[str, ColorChoice] = {}
    used: set[str] = set()          # palette labels already handed out

    # Closest first, so the colour that most clearly IS a palette entry keeps
    # it and the ones merely near it have to work around that. Ties break on
    # the hex, so a rerun of the same deck decides the same way.
    order = sorted(
        wanted,
        key=lambda s: (options[s][0][1] if options[s] else float("inf"), s),
    )

    for source in order:
        chosen: Optional[ColorChoice] = None
        blocker: Optional[str] = None
        for label, distance, fallback in options[source]:
            candidate = _norm(palette.get(label))
            if not candidate or label in used:
                blocker = blocker or _holder_of(label, used, choices)
                continue
            clash = _too_close(candidate, source, final, tolerance)
            if clash is not None:
                blocker = blocker or clash
                continue
            chosen = ColorChoice(
                source=source,
                target=candidate,
                label=label,
                fallback=fallback,
                reason=(
                    f"the nearest palette entry, {distance:.1f} away"
                    if fallback else f"{label}, {distance:.1f} away"
                ),
            )
            break

        if chosen is None:
            # Nothing it measures close to is free. Rather than leave the
            # colour off-palette, keep the RELATIONSHIP instead of the colour:
            # see `_by_contrast`.
            chosen = _by_contrast(
                source=source,
                anchor=blocker or _nearest_other(source, sources),
                palette=palette,
                final=final,
                used=used,
                tolerance=tolerance,
            )

        choices[source] = chosen
        if chosen.target:
            final[source] = chosen.target
            if chosen.label:
                used.add(chosen.label)

    kept = sum(1 for c in choices.values() if c.applies)
    if len(choices) != kept:
        log.warning(
            "colour plan: %d of %d colours recoloured; %d could not be put on "
            "the palette at all",
            kept, len(choices), len(choices) - kept,
        )
    return ColorPlan(choices)


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lstrip("#").upper()


def _too_close(
    candidate: str,
    source: str,
    final: dict[str, str],
    tolerance: float,
) -> Optional[str]:
    """The other colour this candidate would be confused with, if any.

    Measured against where the other colours END UP, not where they started,
    and skipping the colour being placed: a colour is allowed to land on the
    entry it is already nearly identical to, which is the ordinary case.
    """
    for other, other_final in final.items():
        if other == source:
            continue
        distance = delta_e(candidate, other_final)
        if distance is not None and distance <= tolerance:
            return other
    return None
