"""One fixer per rule, for the findings a machine can correct on its own.

A fixer earns its place by being mechanical: the finding names a shape and a
target, and there is exactly one way to reach the target. `space.alignment_grid`
says the left edge should be 11.19in, so the fix is to set it to 11.19in.

Most findings are not like that. `space.overlap` names two boxes and cannot
say which of them should move; `logo.missing` needs a logo file nobody has
handed over; `title.missing` needs copy that has to be written. Those have no
fixer and are reported as needing a designer, which is the honest answer and
a much better one than a plausible guess applied to a client deck.

Every fixer works on the live python-pptx shape and returns a short line
saying what it changed, or None when it found nothing to do. A fixer never
raises: a fix that cannot be made is reported, not fatal.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from ..models import Issue
from ..rules.space import DECLARED_GRID

log = logging.getLogger(__name__)

EMU_PER_INCH = 914400


class LeaveAlone(Exception):
    """Raised by a fixer that has decided this shape must not be touched.

    Distinct from returning None, which means "already correct". This means
    "correctable in principle, and doing it would be wrong here", and the
    reason travels with it onto the report so the designer knows why the fix
    they ticked did not happen.
    """


# Fixes that move a shape. They share a hazard the text fixers do not have: a
# slide is a composition, and a shape nudged to satisfy one rule lands on its
# neighbour. Every one of these is checked against the shapes around it before
# the move is allowed to stand.
GEOMETRIC = frozenset(
    {
        "space.off_canvas",
        "space.safe_margin",
        "space.alignment_grid",
        "space.repeat_out_of_line",
    }
)

# Whitespace that is safe to strip from the end of a paragraph. A vertical tab
# is a soft line break, which is `typography.manual_line_break`'s business and
# a deliberate act often enough that removing it here would be a surprise.
_TRAILING = " \t   "

_INCHES = re.compile(r"(-?\d+(?:\.\d+)?)\s*in")
_EDGE = re.compile(r"\b(left|top)\s+(-?\d+(?:\.\d+)?)\s*in")
_MARGIN = re.compile(r"\b(top|right|bottom|left)\s+(-?\d+(?:\.\d+)?)\s*in")
_QUOTED_FONT = re.compile(r"explicit\s+(.+?)\s*$")


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def fix_off_canvas(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Bring a shape that straddles an edge back inside the canvas.

    Nudged rather than resized: the shape's proportions are a design decision
    and its position usually is not, and a shape too large for the canvas is
    left alone because shrinking it would be the bigger change.

    A shape lying *wholly* off the slide is a different thing entirely and is
    never moved. It is invisible where it is, so nobody is looking at a
    defect; it is either parked there on purpose or left over from an edit,
    and dragging it into view does not repair a slide, it adds junk to one. On
    a real deck this pulled two stray diagram fragments on top of an icon.
    """
    width, height = ctx.width_emu, ctx.height_emu
    left, top = shape.left, shape.top
    if left is None or top is None:
        return None

    box_w = shape.width or 0
    box_h = shape.height or 0
    if box_w > width or box_h > height:
        return None

    if left + box_w <= 0 or left >= width or top + box_h <= 0 or top >= height:
        raise LeaveAlone(
            "it sits entirely off the slide, so it is parked or left over; "
            "whether to reposition or delete it is a designer's call"
        )

    new_left = min(max(left, 0), width - box_w)
    new_top = min(max(top, 0), height - box_h)
    if (new_left, new_top) == (left, top):
        return None

    shape.left, shape.top = new_left, new_top
    return (
        f"moved {(new_left - left) / EMU_PER_INCH:+.2f}, "
        f"{(new_top - top) / EMU_PER_INCH:+.2f}in back onto the canvas"
    )


def fix_alignment_grid(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Snap the left edge to the grid line the finding names.

    Registered, after a spell disabled. `space.alignment_grid` used to find
    the edge the most shapes on a slide share, and the busiest one is not
    necessarily the one a given shape belongs to: on a real deck this snapped
    a section label 0.20in away from the table it captioned and onto a grid
    line borrowed from the other half of the slide.

    Two things answer that now. The grid can come from the master rather than
    from the deck being audited, so the candidate lines are a handful the
    designer drew instead of every habit four shapes share. And the applier
    refuses a move that ends an alignment the shape already had, which is
    exactly what the label-and-table failure was: no new overlap, a broken
    relationship. See `applier._aligned`.

    Neither makes "which column" certain, so this refuses to act on an
    INFERRED grid at all, and the move is bounded by the near-miss window the
    rule reports in: at most four position tolerances, 0.20in by default.

    The refusal is not caution for its own sake. Run against an inferred grid
    on a real deck, this snapped a 5.48in section header 0.20in onto a line
    whose entire support was five 1.67in pentagon labels elsewhere on the
    slide -- the original failure, reproduced. A line four shapes happen to
    share is not a column; a line the master draws is.
    """
    if DECLARED_GRID not in (issue.message or ""):
        raise LeaveAlone(
            "the grid line here is inferred from this deck's own habits, not "
            "declared by the master, and which column a shape belongs to is "
            "a design call. Mark the master's presentation space to enable this"
        )
    target = _inches(issue.expected)
    if target is None or shape.left is None:
        return None
    new_left = int(round(target * EMU_PER_INCH))
    if new_left == shape.left:
        return None
    moved = (new_left - shape.left) / EMU_PER_INCH
    shape.left = new_left
    return f"snapped the left edge {moved:+.2f}in to {target:.2f}in"


def fix_repeat_out_of_line(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Put one member of a series back on the edge the rest of it shares.

    The finding already did the judging: it found the series, established
    which edge the majority agrees on, and satisfied itself that this member
    is near enough for the difference to be drift. All that is left is to move
    it, which is the same act as aligning the set by hand in PowerPoint.
    """
    side, target = _edge(issue.expected)
    if side is None or target is None:
        return None

    current = shape.left if side == "left" else shape.top
    if current is None:
        return None
    wanted = int(round(target * EMU_PER_INCH))
    if wanted == current:
        return None

    moved = (wanted - current) / EMU_PER_INCH
    if side == "left":
        shape.left = wanted
    else:
        shape.top = wanted
    return f"aligned the {side} edge {moved:+.2f}in onto the set at {target:.2f}in"


def fix_safe_margin(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Move a shape back inside the safe margin, the shortest distance.

    Moved, never resized: the box was drawn at that width on purpose, and a
    shape too big for the usable area cannot be brought inside it by nudging,
    so it is left for a person. Only the edges the finding actually names are
    corrected, so a box crossing the right margin is not also dragged down off
    a top margin it was never breaching.
    """
    margins = _margins(issue.expected)
    if not margins:
        return None

    left, top = shape.left, shape.top
    width, height = shape.width or 0, shape.height or 0
    if left is None or top is None:
        return None

    usable_w = ctx.width_emu - _emu(margins.get("left")) - _emu(margins.get("right"))
    usable_h = ctx.height_emu - _emu(margins.get("top")) - _emu(margins.get("bottom"))
    if width > usable_w or height > usable_h:
        return None

    new_left, new_top = left, top
    if "left" in margins:
        new_left = max(new_left, _emu(margins["left"]))
    if "right" in margins:
        new_left = min(new_left, ctx.width_emu - _emu(margins["right"]) - width)
    if "top" in margins:
        new_top = max(new_top, _emu(margins["top"]))
    if "bottom" in margins:
        new_top = min(new_top, ctx.height_emu - _emu(margins["bottom"]) - height)

    if (new_left, new_top) == (left, top):
        return None
    shape.left, shape.top = new_left, new_top
    return (
        f"moved {(new_left - left) / EMU_PER_INCH:+.2f}, "
        f"{(new_top - top) / EMU_PER_INCH:+.2f}in inside the safe margin"
    )


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #

def fix_whitespace(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Strip trailing spaces and collapse runs of spaces inside a line.

    Works run by run from the end of each paragraph, because the trailing
    space usually sits in the last run and only that run's text should change.
    """
    if not _has_text(shape):
        return None

    trimmed = 0
    collapsed = 0
    for paragraph in shape.text_frame.paragraphs:
        runs = list(paragraph.runs)
        for run in reversed(runs):
            stripped = run.text.rstrip(_TRAILING)
            if stripped != run.text:
                trimmed += len(run.text) - len(stripped)
                run.text = stripped
            if stripped:
                break        # the paragraph now ends in real text
        for run in runs:
            squeezed = re.sub(r"  +", " ", run.text)
            if squeezed != run.text:
                collapsed += 1
                run.text = squeezed

    parts = []
    if trimmed:
        parts.append(f"{trimmed} trailing character(s)")
    if collapsed:
        parts.append(f"repeated spaces in {collapsed} run(s)")
    return "removed " + " and ".join(parts) if parts else None


def fix_manual_line_break(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Replace soft returns with a space so the text wraps to its box.

    A space rather than nothing: the break is almost always sitting between
    two words that would otherwise run together.
    """
    if not _has_text(shape):
        return None

    from pptx.oxml.ns import qn  # noqa: PLC0415 - lazy, python-pptx is optional

    removed = 0
    for paragraph in shape.text_frame.paragraphs:
        for br in list(paragraph._p.findall(qn("a:br"))):
            previous = br.getprevious()
            if previous is not None and previous.tag == qn("a:r"):
                node = previous.find(qn("a:t"))
                if node is not None and node.text and not node.text.endswith(" "):
                    node.text += " "
            paragraph._p.remove(br)
            removed += 1
    return f"removed {removed} soft return(s)" if removed else None


def fix_theme_font_drift(shape: Any, issue: Issue, ctx: "FixContext") -> Optional[str]:
    """Clear a run-level typeface so the run inherits from the layout again.

    Only the typeface named in the finding is cleared. A run set in some other
    face is a different defect, and silently rewriting it here would be a
    change nobody asked for.
    """
    wanted = _font_name(issue.found)
    if not wanted or not _has_text(shape):
        return None

    cleared = 0
    for paragraph in shape.text_frame.paragraphs:
        for run in paragraph.runs:
            name = run.font.name
            if name and name.casefold() == wanted.casefold():
                run.font.name = None
                cleared += 1
    return (
        f"cleared the hardcoded {wanted!r} from {cleared} run(s)"
        if cleared
        else None
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

Fixer = Callable[[Any, Issue, "FixContext"], Optional[str]]

FIXERS: dict[str, Fixer] = {
    "space.alignment_grid": fix_alignment_grid,
    "space.off_canvas": fix_off_canvas,
    "space.safe_margin": fix_safe_margin,
    "space.repeat_out_of_line": fix_repeat_out_of_line,
    "typography.whitespace": fix_whitespace,
    "typography.manual_line_break": fix_manual_line_break,
    "font.family.theme_drift": fix_theme_font_drift,
}

# The order fixes run in, low first. Two fixes can touch one shape, and then
# the second decides: snapping a box to a grid line after clamping it back
# onto the canvas pushes it straight off again, which is exactly what applying
# them in report order did. So preferences run first and hard constraints run
# last -- a shape must be on the canvas, and would merely prefer to be on the
# grid.
FIX_ORDER: dict[str, int] = {
    "typography.whitespace": 10,
    "typography.manual_line_break": 10,
    "font.family.theme_drift": 10,
    "space.repeat_out_of_line": 20,
    "space.off_canvas": 90,
    # Last of all, and strictly stronger: the safe margin sits inside the
    # canvas, so a shape moved within it satisfies the canvas too.
    "space.safe_margin": 95,
}
DEFAULT_ORDER = 50


def fix_order(issue: Issue) -> int:
    return FIX_ORDER.get(issue.rule_id or "", DEFAULT_ORDER)

# Findings a machine should not attempt, and why. Kept explicit so that
# `apply --list` can say "needs a designer, because ..." rather than leaving a
# finding unexplained, which reads like an oversight.
NEEDS_A_PERSON: dict[str, str] = {
    "space.overlap": "names two boxes and cannot know which one should move",
    "logo.missing": "needs the approved logo file, which the tool does not have",
    "logo.unapproved_asset": "needs the approved logo file to swap in",
    "title.missing": "needs copy that has to be written",
    "title.detached_textbox": "moving copy between shapes changes the design",
    "title.position_inconsistent": "which position is the right one is a design call",
    "subtitle.structure": "needs copy that has to be written",
    "size.autofit_shrink": "the fix is to edit the copy, not to resize the box",
    "layout.not_in_master": "use `rebuild`, which recreates the slide on the layout",
    "layout.header_footer_missing": "the master has to be edited, not the deck",
    "layout.band_missing": "the master has to be edited, not the deck",
    "color.text.off_palette": "which palette entry was meant is a design call",
    "color.shape.off_palette": "which palette entry was meant is a design call",
    "color.inconsistent_use": "which of the colours in play is correct is a design call",
    "font.family.unapproved": "which approved face replaces it is a design call",
    "font.family.mixed_in_shape": "which of the faces in play is correct is a design call",
    "size.role.out_of_range": "resizing type changes how much copy fits",
    "size.role.inconsistent": "resizing type changes how much copy fits",
    "space.text_overflow": "the fix is to edit the copy or resize the box",
    "typography.orphan_widow": "the fix is to edit the copy",
    "typography.terminal_punctuation": "editing the copy is the writer's call",
}


def fixer_for(issue: Issue) -> Optional[Fixer]:
    """The fixer for a finding, or None when it needs a person.

    AI findings never have one. They are judgements about a slide, phrased for
    a reader, and there is no target to move to.
    """
    if issue.source.value != "rule" or not issue.rule_id:
        return None
    return FIXERS.get(issue.rule_id)


def why_not_fixable(issue: Issue) -> str:
    if issue.source.value != "rule":
        return "an AI observation, not a measured target"
    return NEEDS_A_PERSON.get(
        issue.rule_id or "", "no fixer is written for this rule"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _has_text(shape: Any) -> bool:
    try:
        return bool(shape.has_text_frame)
    except Exception:
        return False


def _inches(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = _INCHES.search(value)
    return float(match.group(1)) if match else None


def _emu(inches: Optional[float]) -> int:
    return int(round((inches or 0.0) * EMU_PER_INCH))


def _edge(expected: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    """"left 6.48in, as the rest of the set" -> ("left", 6.48)."""
    if not expected:
        return None, None
    match = _EDGE.search(expected)
    return (match.group(1), float(match.group(2))) if match else (None, None)


def _margins(expected: Optional[str]) -> dict[str, float]:
    """"top 0.4in, right 0.19in, ... safe margin" -> {"top": 0.4, ...}.

    Only the edges the finding names are returned, so an unspecified edge is
    never invented and never corrected against.
    """
    if not expected:
        return {}
    return {side: float(value) for side, value in _MARGIN.findall(expected)}


def _font_name(found: Optional[str]) -> Optional[str]:
    """"explicit Arial" -> "Arial"."""
    if not found:
        return None
    match = _QUOTED_FONT.search(found.strip())
    return match.group(1) if match else None
