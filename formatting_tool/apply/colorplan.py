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
the tool ever knowing what it meant, so this maps colours ONE TO ONE.

Which raises the question the one-to-one rule creates: what does the second
colour become, once the first has taken the entry they both measured closest
to? Leaving it alone was the first answer and it was the wrong one -- it left
an off-palette colour in a deck whose whole purpose is to be on the palette.
The answer is to stop matching the colour and match the RELATIONSHIP instead:
the contrast the two originals had between them is what made them read as two
steps, so the second colour takes the free entry whose contrast against the
first one's new colour comes closest to that. Colour distance then decides
between the entries that score equally. See `_by_contrast`.

Deck-wide, not per slide. The same three pills appear on eleven slides and
must come out the same colour on all of them, and a colour that stays put on
slide 9 still constrains what another colour may become on slide 2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from ..colorutil import (
    TEXT_CONTRAST_FLOOR,
    contrast_ratio,
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
    # Set when no palette entry could be found that keeps the text on this
    # colour readable, and one was taken anyway because being ON the palette
    # is the harder requirement. The text needs a person; saying so is the
    # least this can do. See the two passes in `_by_contrast`.
    text_at_risk: bool = False

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
        """Colours the palette could not hold at all, for the run log.

        Should be empty on a palette with any range in it. A colour lands here
        only when every entry is either claimed by a colour this one has to
        stay distinct from, or would leave the text on it unreadable.
        """
        return [c for c in self._choices.values() if not c.applies]


def build_color_plan(
    selected: Iterable[str],
    everything: Iterable[str],
    palette: dict[str, str],
    tolerance: float,
    limit: float,
    inks: Optional[dict[str, set[str]]] = None,
) -> ColorPlan:
    """Decide every selected colour's target at once.

    `selected` are the colours a fix was asked for; `everything` is every
    off-palette colour the report found, selected or not. Both are needed: a
    colour nobody ticked keeps the colour it has, and a shape that stays green
    still means no other shape may be recoloured to that green.

    `tolerance` is the distance below which two colours read as the same one,
    so it is the floor two final colours have to clear. `limit` is how far a
    colour may reasonably be moved, which decides what counts as a candidate.

    `inks` maps a fill colour to the text colours found sitting on it, so an
    entry that would make that text unreadable is never chosen in the first
    place. Without it the plan can pick a colour the applier then refuses.
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
            if not _keeps_text_legible(candidate, source, (inks or {}).get(source)):
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
                inks=(inks or {}).get(source),
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


# How close two contrast ratios have to be to count as the same relationship.
# Contrast is a ratio from 1 to 21 and a tenth of it is invisible, so this is
# a band inside which the choice is handed to colour distance instead.
_CONTRAST_BAND = 0.5


def _by_contrast(
    source: str,
    anchor: Optional[str],
    palette: dict[str, str],
    final: dict[str, str],
    used: set[str],
    tolerance: float,
    inks: Optional[set[str]] = None,
) -> ColorChoice:
    """The palette entry that keeps this colour's RELATIONSHIP to another.

    Reached when every entry a colour measures close to is already taken. The
    old answer was to leave it alone, which put an off-palette colour in a
    deck the whole exercise is meant to put on the palette; and the colour it
    wanted is gone, so matching the colour is not on the table either.

    What is still on the table is the relationship. Green and tan were two
    steps of a scale, and what made them read as two steps was the contrast
    between them. So: measure the contrast the two ORIGINAL colours had, then
    take the free entry whose contrast against where the ANCHOR ENDED UP comes
    closest to it. The pair keeps its spacing even though neither keeps its
    hue, which is what a designer transposing a scale into a new palette does
    by eye.

    Contrast alone would not be enough. It is a function of lightness only, so
    a red and a blue of the same lightness score identically against the
    anchor, and picking between them by contrast is picking at random. Inside
    `_CONTRAST_BAND` the decision therefore goes to the nearest colour, which
    is the other half of the instruction: contrast to choose the RANK, distance
    to choose WHICH.
    """
    anchor_before = _norm(anchor)
    anchor_after = _norm(final.get(anchor_before)) or anchor_before
    want = contrast_ratio(source, anchor_before) if anchor_before else None

    def candidates(require_legible: bool) -> list[tuple[float, float, str, str]]:
        out: list[tuple[float, float, str, str]] = []
        for label, entry in palette.items():
            candidate = _norm(entry)
            if not candidate or label in used:
                continue
            if _too_close(candidate, source, final, tolerance) is not None:
                continue
            if require_legible and not _keeps_text_legible(
                candidate, source, inks
            ):
                continue
            distance = delta_e(candidate, source)
            if distance is None:
                continue
            got = contrast_ratio(candidate, anchor_after) if anchor_after else None
            miss = (
                abs(got - want) if (got is not None and want is not None)
                else float("inf")
            )
            out.append((miss, distance, label, candidate))
        return out

    # Legible entries first. If the palette holds none -- a four-entry palette
    # of two darks and two lights cannot carry three pale steps AND keep dark
    # labels on them -- take one anyway. Being on the palette is the
    # requirement; the label that now needs a lighter colour is a thing to
    # report, not a reason to leave a client deck off-brand.
    scored = candidates(require_legible=True)
    at_risk = False
    if not scored:
        scored = candidates(require_legible=False)
        at_risk = bool(scored)

    if not scored:
        # Every entry is either taken or would read as a colour already in
        # play. There is nothing left to assign that would not merge two
        # colours the deck keeps apart, so this one is reported rather than
        # forced.
        return ColorChoice(
            source=source,
            reason=(
                "every palette entry is already taken by another colour this "
                "one has to stay distinct from, so the palette cannot express "
                "them all"
            ),
        )

    # Inside the band every candidate holds the relationship equally well, so
    # the nearest colour wins. Outside it, nothing holds the relationship, so
    # the closest attempt wins. Label breaks both ties, for determinism.
    in_band = [row for row in scored if row[0] <= _CONTRAST_BAND]
    if in_band:
        miss, distance, label, candidate = min(
            in_band, key=lambda row: (row[1], row[2])
        )
    else:
        miss, distance, label, candidate = min(
            scored, key=lambda row: (row[0], row[1], row[2])
        )

    if want is None:
        reason = f"{label}, the nearest entry still free ({distance:.1f} away)"
    else:
        got = contrast_ratio(candidate, anchor_after)
        reason = (
            f"{label}: #{anchor_before} took the entry this was nearest, so "
            f"this keeps their contrast instead -- {want:.1f}:1 originally, "
            f"{got:.1f}:1 against #{anchor_after} now"
        )
    if at_risk:
        reason += (
            "; no free entry keeps the text on this colour readable, so the "
            "text needs recolouring by hand"
        )
    return ColorChoice(
        source=source, target=candidate, label=label, fallback=True,
        reason=reason, text_at_risk=at_risk,
    )


def _nearest_other(source: str, sources: set[str]) -> Optional[str]:
    """The colour in the deck this one is likeliest to be confused with.

    The anchor of last resort. When no single colour blocked this one there is
    no obvious partner, and the nearest other colour is the one whose spacing
    from this one is doing the most work.
    """
    others = [
        (delta_e(source, other), other)
        for other in sources
        if other != source and delta_e(source, other) is not None
    ]
    return min(others)[1] if others else None


def _holder_of(
    label: str, used: set[str], choices: dict[str, ColorChoice]
) -> Optional[str]:
    """Which colour took a palette entry, for the explanation."""
    if label not in used:
        return None
    for choice in choices.values():
        if choice.label == label:
            return choice.source
    return None


def _keeps_text_legible(
    candidate: str, source: str, inks: Optional[set[str]]
) -> bool:
    """Whether text sitting on this colour would still be readable.

    A filter on candidates rather than a veto after the fact, which is the
    whole point of doing it here. Vetoing at apply time left the colour
    off-palette -- the one outcome this module exists to avoid -- because by
    then the choice had been made and there was nothing to fall back to.
    Filtering means the plan simply picks a different entry.

    Only what the recolour would BREAK. A pill whose label is already
    unreadable has a finding of its own, and refusing to put it on the palette
    as well would leave the deck wrong twice over.
    """
    if not inks:
        return True
    for ink in inks:
        after = contrast_ratio(candidate, ink)
        before = contrast_ratio(source, ink)
        if after is None:
            continue
        if after < TEXT_CONTRAST_FLOOR and (
            before is None or before >= TEXT_CONTRAST_FLOOR
        ):
            return False
    return True


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
