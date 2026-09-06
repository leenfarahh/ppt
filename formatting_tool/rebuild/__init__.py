"""Rebuild a messy deck onto the approved master.

Applying a layout to a slide in place does not work: a layout supplies only
what the slide has not already set, and a messy deck has set everything. This
package takes the other route -- open the master as the base file and recreate
each slide on it -- so that the master's geometry and type actually govern the
result.
"""

from .builder import (
    DroppedShape,
    RebuildError,
    RebuildResult,
    SlideRecord,
    rebuild,
)
from .matcher import LayoutMatch, choose_layout

__all__ = [
    "DroppedShape",
    "LayoutMatch",
    "RebuildError",
    "RebuildResult",
    "SlideRecord",
    "choose_layout",
    "rebuild",
]
