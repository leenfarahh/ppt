"""Colour rules: text, fill and line colours against the brand palette.

Two ways a colour is set, and both are measured here.

A HARDCODED colour carries its own RGB and is wrong on its own terms. It is
also fixable on its own terms: the shape is recoloured and the defect is gone.

A THEME-BOUND colour carries no RGB at all, only the slot it reads from, and
is exactly as correct as the theme behind it. These used to be skipped as
"correct by construction", which is true only while the deck is on the
master's theme. A deck built from another file brings its own, so every
theme-bound colour in it resolves through that one -- on one real deck an
accent1 that the master defines as navy and the deck defines as red, on 51
shape fills and 35 runs, none of which the tool said a word about. It is not
a small blind spot: a third of the coloured surface of a messy deck is bound
to the theme rather than typed in.

The difference survives into the fix. A hardcoded colour is corrected on the
shape; a theme-bound one is not, because the shape is doing the right thing
and the theme under it is wrong. That is what `rebuild` is for, and the
finding says so rather than offering a recolour that papers over it.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..colorutil import intended_palette_entry, nearest_palette_entry
from ..models import Category, DeckProfile, Issue, Severity
from .base import Rule, RuleContext


def _resolved(
    hex_value: Optional[str], slot: Optional[str], deck: DeckProfile
) -> tuple[Optional[str], Optional[str]]:
    """The colour that will actually be drawn, and the slot it came from.

    A hardcoded value is itself. A theme-bound one is whatever THIS deck's
    theme says the slot holds, which is the whole point: the comparison has to
    be against what a reader sees, not against what the slot is called.
    """
    if hex_value:
        return hex_value, None
    if slot:
        return (deck.theme_colors.get(slot) or None), slot
    return None, None


def _theme_note(slot: Optional[str], spec) -> Optional[str]:
    """What the master holds in the same slot, when it holds anything.

    The most useful target there is: the deck and the master disagree about
    one named colour, and naming both ends of that is more use to a designer
    than "nearest palette entry" ever is.
    """
    if not slot:
        return None
    theirs = spec.theme_colors.get(slot)
    return f"theme:{slot} #{theirs}" if theirs else None


def _target(value: str, palette: dict, tolerance: float, tuning) -> tuple:
    """What a colour should become, and how sure the rule is about it.

    Two questions, kept apart. "Which entry is nearest" decides whether a
    colour is off-palette at all and always has an answer. "Which entry was it
    meant to be" is allowed to have none -- a palette holding no red has no
    answer for a red -- and it is the one that produces a confident target.

    Both end in a target now, and the difference is carried in the wording
    rather than in whether there is one. An intended entry is named plainly. A
    colour with no intended entry falls back to the nearest, prefixed
    `nearest`, and both the suggestion and the applied line say it reads as a
    different colour.

    That fallback is the reason this docstring is long. It is what recoloured
    a red to an orange 33 delta-E away and a page of blue headings to a
    neutral, and it is deliberately back: a deck left with its off-palette
    colours in place is the more common complaint, and a marked fallback a
    designer can see and reject beats a finding that does nothing. The prefix
    is what makes it rejectable, so it is load-bearing, not decoration.
    """
    limit = tolerance * tuning.suggestion_factor
    label, distance = intended_palette_entry(value, palette, limit)
    if label:
        return (
            f"{label} #{palette.get(label, '')}",
            f"Recolour to {label}, or bind it to the theme colour.",
        )

    near, far = nearest_palette_entry(value, palette)
    if near:
        return (
            # `nearest` marks it as the fallback; the hex is still in there,
            # so the fixer reads a target out of it the same way as any other.
            f"nearest {near} #{palette.get(near, '')}",
            f"No palette entry is this colour: the closest, {near} "
            f"#{palette.get(near, '')}, is {far:.1f} away and reads as a "
            "different colour. Applying this snaps it there anyway, so check "
            "the result -- picking the entry this was meant to be is a "
            "design call.",
        )
    return "brand palette", "Recolour to a palette entry."


_REBUILD = (
    "This colour is bound to the theme, so the shape is not what is wrong: "
    "the deck carries its own theme. Rebuild onto the master, which replaces "
    "it and corrects every colour bound to it at once."
)


class OffPaletteTextRule(Rule):
    id = "color.text.off_palette"
    category = Category.COLOR
    description = "Text colour is not in the brand palette."
    default_severity = Severity.ERROR
    # Not gated on a brand file. `spec.palette` is the authored palette
    # extended with the master's own theme scheme, so a master-only run -- which
    # is most runs -- has a palette to measure against: the one the master
    # declares. Requiring a brand file meant this never ran there, which is the
    # same stale gate the safe-margin check carried.

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.color_delta_e
        palette = ctx.spec.palette

        for slide, shape, _paragraph, run in ctx.runs():
            value, slot = _resolved(run.color_hex, run.color_theme, ctx.deck)
            if not value or slot:
                # A theme-bound run belongs to ThemeMismatchRule, which
                # reports the slot once for the deck instead of once per run.
                continue
            label, distance = nearest_palette_entry(value, palette)
            if distance is None or distance <= tolerance:
                continue
            expected, suggestion = _target(value, palette, tolerance, ctx.tuning)
            yield self.issue(
                f"Text colour #{value} is off-palette "
                f"(nearest: {label}, delta-E {distance:.1f}).",
                slide=slide,
                shape=shape,
                expected=expected,
                found=f"#{value}",
                suggestion=suggestion,
            )


class OffPaletteShapeRule(Rule):
    id = "color.shape.off_palette"
    category = Category.COLOR
    description = "Shape fill or outline colour is not in the brand palette."
    default_severity = Severity.ERROR
    # Not gated on a brand file. `spec.palette` is the authored palette
    # extended with the master's own theme scheme, so a master-only run -- which
    # is most runs -- has a palette to measure against: the one the master
    # declares. Requiring a brand file meant this never ran there, which is the
    # same stale gate the safe-margin check carried.

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.color_delta_e
        palette = ctx.spec.palette

        for slide, shape in ctx.shapes():
            sources = (
                ("fill", shape.fill_hex, shape.fill_theme),
                ("outline", shape.line_hex, shape.line_theme),
            )
            # An icon's colours live inside its SVG, not on the shape. They
            # are measured on the same terms as a fill: a theme-bound one
            # belongs to ThemeMismatchRule, a literal one is the shape's own
            # and is wrong on its own terms.
            sources = sources + tuple(
                ("graphic", None if colour.theme else colour.hex, colour.theme)
                for colour in shape.graphic_colors
            )
            for kind, raw, slot_name in sources:
                value, slot = _resolved(raw, slot_name, ctx.deck)
                if not value or slot:
                    continue        # ThemeMismatchRule's, see above
                label, distance = nearest_palette_entry(value, palette)
                if distance is None or distance <= tolerance:
                    continue
                expected, suggestion = _target(value, palette, tolerance, ctx.tuning)
                yield self.issue(
                    (
                        f"Icon draws in #{value}, which is off-palette "
                        f"(nearest: {label}, delta-E {distance:.1f})."
                        if kind == "graphic" else
                        f"Shape {kind} #{value} is off-palette "
                        f"(nearest: {label}, delta-E {distance:.1f})."
                    ),
                    slide=slide,
                    shape=shape,
                    expected=expected,
                    suggestion=suggestion,
                    # The word carries into the fixer: an icon is recoloured
                    # by rewriting the SVG it draws from, not by setting a
                    # fill the shape does not have.
                    found=f"graphic #{value}" if kind == "graphic" else f"#{value}",
                )


class ThemeMismatchRule(Rule):
    """A theme slot the deck defines differently from the master.

    Reported once per slot for the whole deck, not once per shape. It is one
    defect -- the deck brought its own theme -- and the shapes reading that
    theme are its symptoms. Saying it per shape put 180 identical findings on
    a five-slide deck, which buries every other finding in the report, costs a
    fortune in the AI payload, and still leaves the designer with one thing to
    do.

    The count is the part worth having: "accent1, on 37 shapes and 21 runs
    across 5 slides" is the scale of the problem, and the fix is the same
    single action whether it is 5 shapes or 500.

    Nothing here fires on a deck already on the master's theme, because then
    the slots agree and there is nothing to say.
    """

    id = "color.theme_mismatch"
    category = Category.COLOR
    description = "The deck's theme defines a colour differently from the master's."
    default_severity = Severity.ERROR

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        tolerance = ctx.spec.tolerances.color_delta_e
        theirs = ctx.spec.theme_colors
        ours = ctx.deck.theme_colors
        if not theirs or not ours:
            return

        shapes: dict[str, int] = {}
        runs: dict[str, int] = {}
        slides: dict[str, set[int]] = {}
        for slide, shape in ctx.shapes():
            icon_slots = [c.theme for c in shape.graphic_colors if c.theme]
            for slot in (shape.fill_theme, shape.line_theme, *icon_slots):
                if slot:
                    shapes[slot] = shapes.get(slot, 0) + 1
                    slides.setdefault(slot, set()).add(slide.number)
            for paragraph in shape.paragraphs:
                for run in paragraph.runs:
                    if run.color_theme and run.text.strip():
                        runs[run.color_theme] = runs.get(run.color_theme, 0) + 1
                        slides.setdefault(run.color_theme, set()).add(slide.number)

        for slot in sorted(set(shapes) | set(runs)):
            mine, master_hex = ours.get(slot), theirs.get(slot)
            if not mine or not master_hex:
                continue
            _label, distance = nearest_palette_entry(mine, {slot: master_hex})
            if distance is None or distance <= tolerance:
                continue
            used = _used(shapes.get(slot, 0), runs.get(slot, 0))
            yield self.issue(
                f"The deck's theme defines {slot} as #{mine}; the master "
                f"defines it as #{master_hex}. {used} read it.",
                expected=f"theme:{slot} #{master_hex}",
                found=f"theme:{slot} #{mine}",
                suggestion=_REBUILD,
            )


def _used(shapes: int, runs: int) -> str:
    parts = []
    if shapes:
        parts.append(f"{shapes} shape fill or outline{'s' if shapes != 1 else ''}")
    if runs:
        parts.append(f"{runs} text run{'s' if runs != 1 else ''}")
    return " and ".join(parts) or "Nothing"


class InconsistentColorUseRule(Rule):
    """Near-duplicate colours across the deck, whether or not they are on-brand.

    Fires when two colours sit close enough to read as the same intent but are
    not identical -- the signature of copy-paste from several source decks.
    """

    id = "color.inconsistent_variants"
    category = Category.COLOR
    description = "Several near-identical colours are used for the same purpose."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        # TODO: cluster every colour in the deck by perceptual distance
        # (single-link, threshold = tolerances.color_delta_e * 2), then report
        # any cluster with more than one member. Report deck-level, listing
        # the slides each variant appears on, and pick the cluster member that
        # matches the palette as the expected value.
        return ()
