"""Apply corrections to a deck: the ticked findings, and the master's layouts.

`validate` says what is wrong and changes nothing. This is the other half:
it takes the findings a designer ticked and makes exactly those changes, then
optionally rebuilds the result onto the master's layouts.

Only mechanical findings have a fixer. A finding that names a shape and a
target -- "the left edge should be 11.19in" -- can be corrected without
judgement. One that names two overlapping boxes, or wants a logo file, or
needs copy written, cannot, and is reported as needing a designer rather than
guessed at.
"""

from .applier import (
    ApplyError,
    ApplyResult,
    FixContext,
    FixOutcome,
    apply_fixes,
    FixBrand,
    fixable,
)
from .fixers import (
    FIXERS,
    FIX_ORDER,
    GEOMETRIC,
    LeaveAlone,
    fix_order,
    fixer_for,
    why_not_fixable,
)

__all__ = [
    "ApplyError",
    "ApplyResult",
    "FIXERS",
    "FIX_ORDER",
    "GEOMETRIC",
    "LeaveAlone",
    "fix_order",
    "FixContext",
    "FixOutcome",
    "apply_fixes",
    "FixBrand",
    "fixable",
    "fixer_for",
    "why_not_fixable",
]
