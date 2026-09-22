"""Align elements, and align the text inside them.

A port of the Wizardly add-in's Productivity Tools service --
`Prezlab-Addin/Services/ProductivityAlignService.cs` -- onto files instead of
onto a running PowerPoint. Same four capabilities, same names, same
behaviours: align and distribute a set of elements, set how text sits inside
its box, copy one shape's properties onto others (Make Same), and find the
shapes that already match one (Select Same).

WHY IT IS A PORT AND NOT A WRAPPER. The original drives desktop PowerPoint
over COM: it reads `ActiveWindow.Selection`, calls `ShapeRange.Align`, hands
awkward cases to the format painter with `PickUp`/`Apply`, and collapses the
lot into one `StartNewUndoEntry`. This tool audits and corrects .pptx files in
batch, with no application running, no selection and nobody watching. Every
one of those COM calls has no counterpart here, so the logic is reimplemented
against python-pptx and the differences are written down where they bite:

  * There is no selection, so every entry point takes an ORDERED SEQUENCE of
    shapes and treats the first as the anchor, exactly as the pane treats
    shape #1. Which shape goes first is the caller's judgement and stays in
    the rules, where that judgement already lives. See `actions`.
  * Slide-relative alignment is the same arithmetic against the page box
    rather than a second code path delegating to PowerPoint. See `elements`.
  * Distribution is equal gaps holding the outermost two, which is what
    PowerPoint does and what `rules.space` already measures against.
  * Make Same copies XML elements rather than reading and rewriting values,
    which removes both of the original's format-painter fallbacks outright:
    copying `a:solidFill` carries the theme reference, the tint and the
    gradient stops that COM flattens to "na". See `properties`.
  * Select Same signs a shape from that same XML. The C#'s last-resort
    fallback -- save a copy of the presentation, open the package, find the
    slide part, read `spPr/ln` -- is where this starts. See `signature`.

UNITS. Live shapes are in EMU, as python-pptx has them, and `Box` carries EMU.
Anything a caller supplies or reads back is in INCHES, because that is what
findings, rules and the report speak. The two are never mixed in one name: a
number in inches is either called `*_in` or is a documented argument.

NOTHING HERE RAISES INTO A CALLER. Every entry point returns an
`ArrangeResult` carrying what it did, what it skipped and what the caller
should be told. That is the C#'s contract and it is the right one for this
tool for the same reason: these are called from fixers, and a fixer that
raises costs the report the other twenty-four findings.
"""

from .actions import (
    AlignAction,
    ArrangeResult,
    DistributeAction,
    MatchAction,
    ReferenceMode,
    SLIDE_REFERENCE_ACTIONS,
    TEXT_ACTIONS,
    parse_action,
    parse_reference_mode,
    supports_slide_reference,
)
from .elements import (
    Box,
    EMU_PER_INCH,
    align,
    align_centre,
    align_edge,
    bounds_of,
    distribute,
    emu,
    inches,
    locks_aspect,
    move_to,
)
from .properties import (
    Style,
    apply_style,
    capture,
    capture_slide,
    corner_radius_of,
    make_same,
    set_corner_radius,
)
from .signature import matches, select_same, signature
from .text import (
    ALIGNMENTS,
    ANCHORS,
    align_text,
    alignment_of,
    anchor_centred,
    anchor_of,
    first_run,
    frame_targets,
    insets_of,
    paragraphs,
    primary_frame,
    primary_paragraph,
    reading_order_of,
    set_alignment,
    set_anchor,
    set_anchor_centred,
    set_insets,
    set_reading_order,
    table_of,
    text_frames,
)

__all__ = [
    # Actions and results
    "AlignAction",
    "ArrangeResult",
    "DistributeAction",
    "MatchAction",
    "ReferenceMode",
    "SLIDE_REFERENCE_ACTIONS",
    "TEXT_ACTIONS",
    "parse_action",
    "parse_reference_mode",
    "supports_slide_reference",
    # Elements
    "Box",
    "EMU_PER_INCH",
    "align",
    "align_centre",
    "align_edge",
    "bounds_of",
    "distribute",
    "emu",
    "inches",
    "locks_aspect",
    "move_to",
    # Text inside elements
    "ALIGNMENTS",
    "ANCHORS",
    "align_text",
    "alignment_of",
    "anchor_centred",
    "anchor_of",
    "first_run",
    "frame_targets",
    "insets_of",
    "paragraphs",
    "primary_frame",
    "primary_paragraph",
    "reading_order_of",
    "set_alignment",
    "set_anchor",
    "set_anchor_centred",
    "set_insets",
    "set_reading_order",
    "table_of",
    "text_frames",
    # Make Same
    "Style",
    "apply_style",
    "capture",
    "capture_slide",
    "corner_radius_of",
    "make_same",
    "set_corner_radius",
    # Select Same
    "matches",
    "select_same",
    "signature",
]
