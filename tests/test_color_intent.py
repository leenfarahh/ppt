"""Off-palette colours that mean something, and what that buys them.

A palette rule can prove a colour is not on the palette. It can never say why
the colour is there, and on a real slide that difference is the whole answer:
four cards each showing the same concentric diagram, one ring picked out in
orange and the rest greyed, so that reading across the row shows the scope
widening. Every grey is off-palette. Every grey is deliberate. Flattening them
leaves four identical diagrams and destroys the only thing the row was saying.
A traffic-light status column is the same shape of problem.

WHAT THE ANSWER USED TO BE AND WHY IT CHANGED. The verdict used to hold the
fix back: the model said these greys mean something, so the tool left them
alone. That answered the wrong question. What makes a legend a legend is not
its particular greys, it is that its steps are DIFFERENT from each other -- and
leaving them alone left off-palette colours in a deck whose whole purpose is
to be on the palette, which is the complaint the tool exists to answer.

So a colour read as meaningful is corrected like any other, and what the
reading buys it is DISTANCE: the colours of one encoding are held clearly
apart from each other on the way onto the palette, rather than merely
not-identical. The finding is still never deleted -- the response schema has
no way to dismiss one, and a model that could delete findings would quietly
decide what a deck may be checked for.
"""

from __future__ import annotations

import pathlib

from formatting_tool.ai.schema import (
    AI_RESPONSE_SCHEMA,
    KEEP_CONFIDENCE,
    color_intents_from_response,
)
from formatting_tool.apply.applier import _meaningful_colors
from formatting_tool.models import Category, ColorIntent, Issue, Severity, Source


def _issue(
    slide: int,
    shape_id: int,
    category: Category = Category.COLOR,
    found: str = "#8A8A8A",
) -> Issue:
    issue = Issue(
        category=category,
        severity=Severity.ERROR,
        message="off-palette",
        source=Source.RULE,
        rule_id="color.text.off_palette",
        slide=slide,
        shape=f"Shape {shape_id}",
        shape_id=shape_id,
        found=found,
        deck="messy.pptx",
    )
    issue.id = issue.fingerprint()
    return issue


def _keep(slide: int, shape_ids, confidence: float = 0.9) -> ColorIntent:
    return ColorIntent(
        slide=slide, keep=True, scheme="greyed out to de-emphasise",
        shape_ids=list(shape_ids), confidence=confidence,
        why="the greys carry the progression across the row",
    )


# --------------------------------------------------------------------------- #
# Reading the verdict
# --------------------------------------------------------------------------- #

def test_only_keep_survives_parsing() -> None:
    """'change' is the default the tool already follows; recording it would
    put a row saying nothing on every report."""
    payload = {"color_intent": [
        {"slide": 1, "verdict": "change", "scheme": "", "shape_ids": [],
         "confidence": 0.95, "why": "drift"},
    ]}
    assert color_intents_from_response(payload, {1}) == []


def test_an_unconfident_keep_is_discarded() -> None:
    payload = {"color_intent": [
        {"slide": 1, "verdict": "keep", "scheme": "rag", "shape_ids": [7],
         "confidence": KEEP_CONFIDENCE - 0.01, "why": "maybe"},
    ]}
    assert color_intents_from_response(payload, {1}) == []


def test_a_confident_keep_is_carried_with_its_shapes() -> None:
    payload = {"color_intent": [
        {"slide": 19, "verdict": "keep", "scheme": "traffic-light RAG status",
         "shape_ids": [4, 5], "confidence": 0.9, "why": "the red is the point"},
    ]}
    got = color_intents_from_response(payload, {19})
    assert len(got) == 1
    assert got[0].slide == 19 and got[0].shape_ids == [4, 5]
    assert got[0].scheme == "traffic-light RAG status"


def test_a_verdict_on_a_slide_the_model_never_saw_is_dropped() -> None:
    """Holding a fix on a slide nobody reviewed protects a real defect."""
    payload = {"color_intent": [
        {"slide": 99, "verdict": "keep", "scheme": "rag", "shape_ids": [],
         "confidence": 0.99, "why": "unseen"},
    ]}
    assert color_intents_from_response(payload, {1, 2, 3}) == []


def test_there_is_still_no_way_to_dismiss_a_finding() -> None:
    """The schema offers a verdict on colour and nothing that deletes."""
    properties = AI_RESPONSE_SCHEMA["properties"]
    assert set(properties) == {"issues", "summary", "layout_choices", "color_intent"}
    verdicts = properties["color_intent"]["items"]["properties"]["verdict"]["enum"]
    assert verdicts == ["change", "keep"]


# --------------------------------------------------------------------------- #
# Acting on it
# --------------------------------------------------------------------------- #

def test_the_named_shapes_are_read_as_one_encoding() -> None:
    """The case the shape list exists for: a real colour system on a slide
    that also has a heading somebody typed the wrong blue into. Both are
    corrected; only the system's colours are held apart from each other."""
    system = _issue(19, 4, found="#8A8A8A")
    second = _issue(19, 5, found="#C4C4C4")
    mistake = _issue(19, 88, found="#1F4E9C")
    kept, schemes = _meaningful_colors(
        [system, second, mistake], [_keep(19, [4, 5])]
    )
    assert sorted(i.shape_id for i, _ in kept) == [4, 5]
    assert schemes == [{"8A8A8A", "C4C4C4"}]


def test_nothing_is_taken_out_of_the_run() -> None:
    """The change that matters. A colour read as meaningful is still put on
    the palette: being off it is the defect the whole exercise is about."""
    issues = [_issue(19, 4), _issue(19, 88)]
    kept, _schemes = _meaningful_colors(issues, [_keep(19, [])])
    assert len(kept) == 2
    assert [i for i, _ in kept] == issues


def test_an_empty_shape_list_reads_the_whole_slide_as_the_system() -> None:
    issues = [_issue(19, 4, found="#8A8A8A"), _issue(19, 88, found="#C4C4C4")]
    kept, schemes = _meaningful_colors(issues, [_keep(19, [])])
    assert len(kept) == 2
    assert schemes == [{"8A8A8A", "C4C4C4"}]


def test_only_colour_work_is_read_as_an_encoding() -> None:
    """A slide whose greys are deliberate still gets its shapes aligned, and
    a spacing finding is not part of anybody's colour scheme."""
    colour = _issue(19, 4)
    space = _issue(19, 4, category=Category.SPACE)
    kept, _schemes = _meaningful_colors([colour, space], [_keep(19, [4])])
    assert [i.category for i, _ in kept] == [Category.COLOR]


def test_other_slides_are_untouched() -> None:
    here, elsewhere = _issue(19, 4), _issue(20, 4)
    kept, _schemes = _meaningful_colors([here, elsewhere], [_keep(19, [4])])
    assert [i.slide for i, _ in kept] == [19]


def test_one_colour_is_not_a_system() -> None:
    """A single colour has nothing to stay distinct from, so raising the bar
    on it would only make a wrong blue harder to correct."""
    _kept, schemes = _meaningful_colors([_issue(19, 4)], [_keep(19, [4])])
    assert schemes == []


def test_no_verdict_means_correct_it_on_the_ordinary_terms() -> None:
    """No opinion is not a reading. Nothing is grouped and nothing is held
    apart; the colour is corrected the way every other colour is."""
    issues = [_issue(19, 4)]
    assert _meaningful_colors(issues, None) == ([], [])
    assert _meaningful_colors(issues, []) == ([], [])


def test_a_change_verdict_reads_nothing() -> None:
    intent = ColorIntent(slide=19, keep=False, shape_ids=[4], confidence=0.99)
    kept, schemes = _meaningful_colors([_issue(19, 4)], [intent])
    assert kept == [] and schemes == []


def test_the_finding_is_kept_whole_for_the_report() -> None:
    """A designer told a legend was recoloured can check it still reads as
    one, which needs the finding and the reading side by side."""
    issue = _issue(19, 4)
    kept, _schemes = _meaningful_colors([issue], [_keep(19, [4])])
    held_issue, intent = kept[0]
    assert held_issue is issue
    assert intent.scheme and intent.why


def test_an_encoding_is_held_further_apart_than_the_tolerance() -> None:
    """What the reading actually buys. Two greys that both measure nearest to
    one palette entry come out as two entries a reader can tell apart, rather
    than as two that merely are not the same value."""
    from formatting_tool.apply.colorplan import SCHEME_FLOOR, build_color_plan
    from formatting_tool.colorutil import delta_e

    palette = {
        "dk1": "1A1A1A", "lt1": "FFFFFF", "accent1": "0B5FA5",
        "accent2": "6E7B8B", "accent3": "B9C0C8", "accent4": "D8402F",
    }
    greys = ["8A8A8A", "9A9A9A"]
    plan = build_color_plan(
        selected=greys, everything=greys, palette=palette,
        tolerance=3.0, limit=12.0, schemes=[set(greys)],
    )
    landed = [plan.choice_for(value).target for value in greys]
    assert all(landed), plan.declined
    assert delta_e(*landed) > SCHEME_FLOOR


# --------------------------------------------------------------------------- #
# The precondition, which is the part that actually failed
# --------------------------------------------------------------------------- #

def test_the_page_asks_for_renders() -> None:
    """Every render-based check is inert without this, and silently so.

    The page posted `guidelines`, `use_ai`, `model`, `effort`,
    `min_confidence` and `batch_size`, and never `render`. So
    `RunConfig.render` was False on every run from the UI, `_render` returned
    "rendering was not requested", and the AI layer saw geometry only. Nothing
    reported it: the prompt still described the pictures, the model still
    answered, and the answers were simply poorer. Colour intent could not fire
    at all, because a verdict about a slide the model was not shown is dropped.

    Asserted against the file rather than through a request because that is
    where the omission was, and a mock of the page would have agreed with the
    bug.
    """
    page = pathlib.Path("formatting_tool/web/static/index.html").read_text(
        encoding="utf-8"
    )
    options = page.split('form.append("options"', 1)[1].split("}));", 1)[0]
    assert "render: true" in options


def test_the_reading_reaches_the_plan_rather_than_the_selection() -> None:
    """Where the verdict is acted on now. It used to shorten the list of
    fixes to run; it now widens the gap the plan has to leave between the
    colours of one encoding, and the fixes all still run."""
    from formatting_tool.apply import applier

    system = _issue(19, 4, found="#8A8A8A")
    second = _issue(19, 5, found="#C4C4C4")
    _kept, schemes = applier._meaningful_colors(
        [system, second], [_keep(19, [4, 5])]
    )
    assert schemes == [{"8A8A8A", "C4C4C4"}]
