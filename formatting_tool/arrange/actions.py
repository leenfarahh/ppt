"""What the caller can ask the engine for, and what comes back.

The action set is the add-in's, name for name, because the point of the port
is that a designer who has used the Productivity Tools pane in PowerPoint gets
the same fifteen alignments and the same property transfers out of this tool.
Nothing here is invented and nothing is left out; where the file-based
implementation cannot reproduce the COM one exactly, the difference is written
down in the module that implements it rather than smuggled into a new name.

THE ONE STRUCTURAL DIFFERENCE IS THE REFERENCE. The add-in reads
`ActiveWindow.Selection` and takes shape #1 as the anchor, because a designer
clicked it. There is no selection in a file, so every entry point here takes
its shapes as an ordered sequence and treats the first of them as the anchor
in exactly the same way. Which shape ends up first is the caller's judgement --
the grid line a rule derived, the majority a series agreed on -- and that
judgement stays where it already lives, in the rules, rather than being
reinvented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ReferenceMode(Enum):
    """What "the reference" means for one call.

    `FIRST_SHAPE` anchors on shape #1 of the sequence and never moves it.
    `SLIDE` anchors on the page box and moves every shape given, which is what
    PowerPoint's own `Align(..., relativeToSlide:=msoTrue)` does.
    """

    FIRST_SHAPE = "firstShape"
    SLIDE = "slide"


class AlignAction(Enum):
    """The fifteen alignments the pane offers.

    The first eleven are the ordinary edge and centre alignments. The
    `POSITION_*` four are the pane's "dock against the anchor" family: an edge
    on one axis and a centre on the other, so a label snapped to the right of
    a card sits against the card's right edge and centred on its height.
    """

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"
    TOP_LEFT = "topLeft"
    TOP_RIGHT = "topRight"
    BOTTOM_LEFT = "bottomLeft"
    BOTTOM_RIGHT = "bottomRight"
    CENTER = "center"
    MIDDLE = "middle"
    CENTER_MIDDLE = "centerMiddle"
    POSITION_LEFT = "positionLeft"
    POSITION_RIGHT = "positionRight"
    POSITION_TOP = "positionTop"
    POSITION_BOTTOM = "positionBottom"


class DistributeAction(Enum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


class MatchAction(Enum):
    """One property, for both Make Same and Select Same.

    Deliberately one enum for the two, as in the add-in: "make these the same
    width" and "select everything the same width" are the same question asked
    of a capture and of a candidate, and splitting them would let the two
    drift into disagreeing about what "the same" means.

    The three text-frame entries at the end are not in the C#. They are the
    settings this tool already reports on -- the anchor, the insets, the
    paragraph alignment -- and they are here because they are the half of
    "align the text within an element" that a property transfer can carry.
    """

    WIDTH = "width"
    HEIGHT = "height"
    SIZE = "size"
    CORNER_RADIUS = "cornerRadius"
    ROTATION = "rotation"
    FILL_COLOR = "fillColor"
    LINE_WEIGHT = "lineWeight"
    LINE_STYLE = "lineStyle"
    LINE_COLOR = "lineColor"
    FONT_NAME = "fontName"
    FONT_COLOR = "fontColor"
    FONT_SIZE = "fontSize"
    FONT_STYLE = "fontStyle"
    ARROW_STYLE = "arrowStyle"
    FORMAT = "format"
    TEXT_ANCHOR = "textAnchor"
    TEXT_INSETS = "textInsets"
    PARAGRAPH_ALIGNMENT = "paragraphAlignment"


# The only actions a slide reference can answer. A slide has a width and a
# height and nothing else a shape could be made the same as, so asking for its
# fill or its font is a question with no answer rather than a failed attempt.
SLIDE_REFERENCE_ACTIONS = frozenset(
    {MatchAction.WIDTH, MatchAction.HEIGHT, MatchAction.SIZE}
)

# The actions whose subject is the text inside the box rather than the box.
# They share a traversal -- every cell of a table, not just the first -- and
# the property modules branch on this set rather than listing them twice.
TEXT_ACTIONS = frozenset(
    {
        MatchAction.FONT_NAME,
        MatchAction.FONT_COLOR,
        MatchAction.FONT_SIZE,
        MatchAction.FONT_STYLE,
        MatchAction.TEXT_ANCHOR,
        MatchAction.TEXT_INSETS,
        MatchAction.PARAGRAPH_ALIGNMENT,
    }
)


def supports_slide_reference(action: MatchAction) -> bool:
    """C#: SupportsSlideReference."""
    return action in SLIDE_REFERENCE_ACTIONS


def parse_action(enum_cls, value):
    """Case-insensitive parse, as the add-in's `Enum.TryParse(..., true, ...)`.

    Returns None when nothing matches, which is the caller's cue to say so
    rather than to guess. Both spellings are accepted -- the pane's camelCase
    payload and the C# member's PascalCase -- because a value read off a
    finding, a config file or a web request has been written by a person and
    people write both.
    """
    if value is None:
        return None
    needle = str(value).strip().lower()
    if not needle:
        return None
    for member in enum_cls:
        if member.value.lower() == needle or member.name.lower() == needle:
            return member
    compact = needle.replace("_", "").replace("-", "").replace(" ", "")
    for member in enum_cls:
        if member.name.replace("_", "").lower() == compact:
            return member
        if member.value.replace("_", "").lower() == compact:
            return member
    return None


def parse_reference_mode(value) -> ReferenceMode:
    """Only an exact case-insensitive "slide" selects the slide.

    Anything else falls back to the first shape, including nonsense, which is
    the add-in's behaviour and the safe one: an unreadable mode that silently
    became "align everything to the page" would move every shape in the set.
    """
    if value is not None and str(value).strip().lower() == "slide":
        return ReferenceMode.SLIDE
    return ReferenceMode.FIRST_SHAPE


@dataclass
class ArrangeResult:
    """What one call did, in the shape the callers of this tool expect.

    C#: ProductivityAlignResult, with one addition. The add-in returns a
    single `Message` for a toast; a fixer in this tool returns a sentence
    saying what it changed, and the applier prints those onto the report. So
    the per-change sentences are collected in `changes` and `summary()` joins
    them, while `message` keeps its original job: the one thing the caller
    should be told when the call did not do what was asked.

    `skipped` and `aspect_locked` carry the same meanings as the C#: a shape
    the action could not be applied to, and a shape whose aspect ratio is
    locked and was resized anyway. See `elements.locks_aspect` for why the
    second is counted rather than obeyed.
    """

    success: bool = False
    changed: int = 0
    skipped: int = 0
    aspect_locked: int = 0
    message: Optional[str] = None
    changes: list[str] = field(default_factory=list)

    def record(self, line: str) -> None:
        """Note one change, and count it."""
        self.changed += 1
        if line:
            self.changes.append(line)

    def summary(self) -> Optional[str]:
        """Every change as one sentence, or None when nothing changed."""
        if not self.changes:
            return None
        if len(self.changes) == 1:
            return self.changes[0]
        return "; ".join(self.changes)

    def __bool__(self) -> bool:
        return bool(self.success)
