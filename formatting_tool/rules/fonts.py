"""Typeface rules: which face the copy is set in.

Direction is not here. `typography.rtl_not_set` used to sit beside the
Arabic font rule because both read Arabic copy, and the two answer
different questions off it: whether the face can DRAW the script, and
which way the paragraph RUNS. The direction rules live together in
`rules.direction`, where the alignment and the shape-side checks they
share their evidence with are.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..models import Category, Issue, Severity
from ..script import has_arabic
from .base import Rule, RuleContext


class UnapprovedFontRule(Rule):
    id = "font.family.unapproved"
    category = Category.FONT_FAMILY
    description = "Text uses a typeface outside the approved set."
    default_severity = Severity.ERROR
    requires_guidelines = True

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        approved = {f.casefold() for f in ctx.spec.allowed_fonts}
        if not approved:
            return

        for slide, shape, _paragraph, run in ctx.runs():
            # A run with no explicit font inherits from the layout, which the
            # theme rule below covers. Silence here, not a false positive.
            if not run.font_name:
                continue
            if run.font_name.casefold() in approved:
                continue
            yield self.issue(
                f"Typeface {run.font_name!r} is not approved.",
                slide=slide,
                shape=shape,
                expected=", ".join(ctx.spec.allowed_fonts),
                found=run.font_name,
                suggestion=f"Set the run to {ctx.spec.allowed_fonts[0]!r}.",
            )


class ArabicFontRule(Rule):
    """Arabic copy set in a typeface that has no Arabic in it.

    The defect a merged font list cannot see. `MasterSpec.allowed_fonts` used
    to be the brand's Latin faces and its Arabic faces in one list, so an
    Arabic heading set in Aptos passed: Aptos is on the list. It renders as
    boxes, or as whatever the machine falls back to, which is a different
    typeface on every machine that opens the deck.

    Coverage, not direction, so ONE Arabic word in an English run is enough.
    How much Latin sits beside it changes nothing about whether the face can
    draw the Arabic.

    Silent without a declared Arabic list. Guessing which of the brand's faces
    covers Arabic from the name would be a guess, and reporting every Arabic
    run in a deck whose guidelines never mentioned Arabic would be noise.
    """

    id = "font.family.arabic"
    category = Category.FONT_FAMILY
    description = "Arabic text is set in a typeface with no Arabic glyphs."
    default_severity = Severity.ERROR
    requires_guidelines = True

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        approved = list(ctx.spec.arabic_fonts)
        if not approved:
            return
        allowed = {name.casefold() for name in approved}

        for slide, shape, _paragraph, run in ctx.runs():
            if not has_arabic(run.text):
                continue
            name = (run.font_name or "").strip()
            if not name or name.casefold() in allowed:
                continue
            yield self.issue(
                f"Arabic text is set in {name!r}, which is not one of the "
                "brand's Arabic typefaces.",
                slide=slide,
                shape=shape,
                expected=", ".join(approved),
                found=name,
                suggestion=(
                    f"Set the Arabic runs in {approved[0]!r}. A Latin face has "
                    "no Arabic glyphs, so this renders as fallback shapes."
                ),
            )


class MixedFontsInShapeRule(Rule):
    """More than one typeface inside a single text box.

    Almost always pasted content that kept its source formatting, and the
    single most common finding in a messy deck.
    """

    id = "font.family.mixed_in_shape"
    category = Category.FONT_FAMILY
    description = "One text box mixes several typefaces."
    default_severity = Severity.WARNING

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        for slide, shape in ctx.text_shapes():
            fonts = {
                run.font_name
                for paragraph in shape.paragraphs
                for run in paragraph.runs
                if run.font_name and run.text.strip()
            }
            if len(fonts) > 1:
                yield self.issue(
                    f"Text box mixes {len(fonts)} typefaces.",
                    slide=slide,
                    shape=shape,
                    found=", ".join(sorted(fonts)),
                    suggestion="Clear formatting and reapply the layout style.",
                )


class ThemeFontDriftRule(Rule):
    """The right typeface, hardcoded instead of inherited.

    RunProfile.font_name is only populated when the run sets a typeface
    explicitly (python-pptx returns None for an inherited font), so a run whose
    explicit font equals the theme font is redundant: on-brand today, broken
    the moment the theme changes. Reported once per shape, not once per run.

    The theme compared against is the master's, never the deck's own. A messy
    deck arrives carrying the theme of whatever file it was built from, and
    measuring it against that theme asks only whether the wrong brand was
    applied consistently.
    """

    id = "font.family.theme_drift"
    category = Category.FONT_FAMILY
    description = "Approved typeface is hardcoded instead of inherited."
    default_severity = Severity.INFO

    def check(self, ctx: RuleContext) -> Iterable[Issue]:
        theme_fonts = {f.casefold(): f for f in ctx.spec.theme_fonts.values() if f}
        if not theme_fonts:
            return

        # Keyed by shape id, not by shape name. Names repeat freely inside a
        # slide -- sixteen shapes called "Pentagon 7" is a real deck -- and
        # collapsing them into one finding would make it unfixable: a fix
        # applies to one shape, so a finding has to mean one shape.
        counts: dict[tuple[int, int], int] = defaultdict(int)
        seen: dict[tuple[int, int], tuple] = {}
        for slide, shape, _paragraph, run in ctx.runs():
            if run.font_name and run.font_name.casefold() in theme_fonts:
                key = (slide.number, shape.shape_id)
                counts[key] += 1
                seen.setdefault(key, (slide, shape, run.font_name))

        for key, (slide, shape, font_name) in seen.items():
            yield self.issue(
                f"{counts[key]} run(s) hardcode {font_name!r}, which is the "
                f"theme typeface.",
                slide=slide,
                shape=shape,
                expected="inherited from the layout",
                found=f"explicit {font_name}",
                suggestion="Clear the run-level font so it follows the theme.",
            )
