"""What the restyle may claim into a layout region, once it has been told.

`claim_drawn_over` fills a region from the box drawn on top of it, which is the
right reading for a subtitle and the wrong one for everything else that is the
same shape. The subtitle region runs the width of the page, so EVERY full-width
box on the slide is drawn over it: the standfirst, the chart's caption, and the
source line at the foot. Which of them is the subtitle is not in the file.

So the model is asked (`ai.roles`), and these are the rules that follow from
its answer. The deterministic reading of small print sits behind them for the
runs where there is no model; see `test_shape_roles`.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pptx")

from formatting_tool.rebuild.builder import (        # noqa: E402
    _leave_alone,
    _may_take,
    _subtitle_text,
)
from formatting_tool.ai.roles import ShapeRole, SlideRoles      # noqa: E402

EMU = 914400
CANVAS = (13.333, 7.5)


class _TextFrame:
    def __init__(self, text):
        self.text = text
        self.paragraphs = []


class _Box:
    """A shape, in the two shapes the code under test reads it in."""

    def __init__(self, text, left=0.7, top=1.3, width=12.3, height=0.5,
                 name="Rectangle 1", family=None):
        self.has_text_frame = True
        self.text_frame = _TextFrame(text)
        self.left, self.top = int(left * EMU), int(top * EMU)
        self.width, self.height = int(width * EMU), int(height * EMU)
        self.name = name
        self._family = family

    # what `_family_of` reads
    @property
    def placeholder_format(self):
        if self._family is None:
            raise AttributeError("not a placeholder")
        return type("PF", (), {"type": self._family})()


def _region(family="SUBTITLE (4)"):
    return _Box("", family=family, name="Subtitle Placeholder 2")


def _read(*entries):
    return SlideRoles(
        slide=1, reviewed=True,
        shapes=tuple(
            ShapeRole(ref=f"s{i}", role=role, shape_id=i, text=text)
            for i, (role, text) in enumerate(entries, start=1)
        ),
    )


# --------------------------------------------------------------------------- #
# What may not be claimed at all
# --------------------------------------------------------------------------- #

def test_a_source_line_is_never_a_candidate() -> None:
    """The defect: it is a full-width box, so it measures exactly like a
    standfirst and was filled into the region at the top of the page."""
    roles = _read(("source", "source: oxford economics, team analysis"))
    box = _Box("Source: Oxford Economics, Team analysis")

    assert _leave_alone(box, roles, CANVAS)


def test_a_chevron_with_words_on_it_is_never_a_candidate() -> None:
    """The other half of the same defect and the worse one: the words were
    claimed into a body region and the chevron they were drawn on was deleted
    with the box they came out of. The copy survived and the design did not."""
    roles = _read(("decoration", "1. what is the new ambition?"))
    box = _Box("1. What is the new ambition?")

    assert _leave_alone(box, roles, CANVAS)


def test_a_charts_own_label_is_never_a_candidate() -> None:
    roles = _read(("chart", "aed 195 bn"))
    assert _leave_alone(_Box("AED 195 Bn"), roles, CANVAS)


def test_body_copy_is_still_claimed() -> None:
    """The distinction the whole thing draws. A body box SHOULD move into the
    region the master gives it -- that is what applying a master is."""
    roles = _read(("body", "resilience and sovereignty are now a priority"))
    box = _Box("Resilience and sovereignty are now a priority")

    assert not _leave_alone(box, roles, CANVAS)


def test_small_print_is_held_back_with_no_model_at_all() -> None:
    """The model is optional -- no key, no renderer, a spent quota -- and the
    defect it prevents is the one a designer sees first."""
    assert _leave_alone(_Box("Source: team analysis", top=6.9), None, CANVAS)


def test_an_unread_slide_falls_back_to_the_file() -> None:
    """A slide the model declined is not a slide it cleared."""
    unread = SlideRoles(slide=1, reviewed=False)
    assert _leave_alone(_Box("Source: team analysis", top=6.9), unread, CANVAS)
    assert not _leave_alone(_Box("Ordinary copy", top=2.0), unread, CANVAS)


# --------------------------------------------------------------------------- #
# Which box may take the subtitle region
# --------------------------------------------------------------------------- #

def test_only_the_named_subtitle_may_take_the_region() -> None:
    roles = _read(("subtitle", "a sharper portfolio"))
    subtitle = _subtitle_text(roles)

    assert _may_take(_Box("A sharper portfolio"), _region(), subtitle)
    assert not _may_take(_Box("Manufacturing value add growth"), _region(),
                         subtitle)


def test_a_slide_with_no_subtitle_leaves_the_region_empty() -> None:
    """An empty string is a different answer from None and has to be:
    somebody looked and there is no standfirst on this slide, so the region is
    left for the designer rather than filled with a chart's caption."""
    roles = _read(("body", "some copy"))
    assert _subtitle_text(roles) == ""
    assert not _may_take(_Box("Manufacturing value add growth"), _region(), "")


def test_with_no_roles_at_all_the_old_reading_stands() -> None:
    """None means nothing was read, and the geometry decides as it always
    did -- so a host with no model keeps the behaviour it had."""
    assert _subtitle_text(None) is None
    assert _may_take(_Box("Anything"), _region(), None)


def test_the_gate_is_only_on_subtitle_regions() -> None:
    """A content region is filled from the box drawn over it on the ordinary
    terms. The subtitle is the one region every full-width box looks like."""
    roles = _read(("subtitle", "a sharper portfolio"))
    body = _region(family="BODY (2)")

    assert _may_take(_Box("Something else"), body, _subtitle_text(roles))


# --------------------------------------------------------------------------- #
# The subtitle reaching its region when it is not drawn over it
# --------------------------------------------------------------------------- #

def test_the_named_subtitle_is_claimed_wherever_it_sits() -> None:
    """Geometry is not enough on its own. On a messy deck the standfirst is
    nowhere near where the new master puts one -- the old master had it two
    inches lower, or there was no standfirst region at all and somebody typed
    the line into a loose box. It is unmistakably a standfirst to anybody
    looking at the slide and nothing in the file says so."""
    from formatting_tool.rebuild.builder import claim_subtitle

    low = _Box("A sharper portfolio", top=4.2, width=5.0)
    region = _region()
    pool = [region]

    claims = claim_subtitle([low], pool, "a sharper portfolio")

    assert claims == {id(low): region}
    assert pool == [], "the region has to leave the pool, or it fills twice"


def test_nothing_is_claimed_when_no_subtitle_was_named() -> None:
    from formatting_tool.rebuild.builder import claim_subtitle

    pool = [_region()]
    assert claim_subtitle([_Box("Anything")], pool, "") == {}
    assert claim_subtitle([_Box("Anything")], pool, None) == {}
    assert len(pool) == 1


def test_a_region_that_is_already_filled_is_not_claimed_again() -> None:
    """Runs and drawn-over boxes get first refusal everywhere else here, and
    this is no exception: it only ever fills a region nothing else took."""
    from formatting_tool.rebuild.builder import claim_subtitle

    taken = _region()
    taken.text_frame.text = "already home"
    assert claim_subtitle([_Box("A sharper portfolio")], [taken],
                          "a sharper portfolio") == {}


# --------------------------------------------------------------------------- #
# A protected shape is not deleted by any route
# --------------------------------------------------------------------------- #

def test_the_other_deletion_paths_are_closed_too() -> None:
    """Keeping a shape out of `loose` stops it being claimed into a region. It
    does not stop `furniture_of` sweeping it up as the run's own drawing, or
    `echoes_layout` reading it as a title the layout already writes -- and both
    of those delete. A chevron banner deleted as a run's drawing is exactly as
    gone as one deleted with its copy, so both branches ask first.

    Asserted against the source because the alternative is a full rebuild over
    a real deck, and what went wrong was a branch order rather than a
    behaviour a fixture would show."""
    import inspect

    from formatting_tool.rebuild import builder

    for name in ("fill_runs", "_rebuild_slide"):
        body = inspect.getsource(getattr(builder, name))
        loop = body.split("for shape in shapes:", 1)[1]
        guard = loop.index("_leave_alone(")
        for later in ("RUN_DRAWING", "LAYOUT_ECHO"):
            assert guard < loop.index(later), (
                f"{name}: {later} is decided before the protection is asked"
            )
