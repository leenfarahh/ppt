"""Off-palette colours that mean something, and are therefore left alone.

A palette rule can prove a colour is not on the palette. It can never say why
the colour is there, and on a real slide that difference is the whole answer:
four cards each showing the same concentric diagram, one ring picked out in
orange and the rest greyed, so that reading across the row shows the scope
widening. Every grey is off-palette. Every grey is deliberate. Correcting them
leaves four identical diagrams and destroys the only thing the row was saying.
A traffic-light status column is the same shape of problem.

So the model is asked, off the render, and its answer HOLDS A FIX BACK rather
than removing a finding. That line matters and is tested here: the response
schema says there is deliberately no way to dismiss a rule finding, because a
model that could delete findings would quietly decide what a deck is allowed
to be checked for. Keeping the finding and declining to act on it unasked is
a different thing, and it is the thing this does.

The bar is set against keeping. An uncertain keep leaves a real defect in a
deck nobody looks at again; an uncertain change is one click to undo.
"""

from __future__ import annotations

from formatting_tool.ai.schema import (
    AI_RESPONSE_SCHEMA,
    KEEP_CONFIDENCE,
    color_intents_from_response,
)
from formatting_tool.apply.applier import _keep_intentional_colors
from formatting_tool.models import Category, ColorIntent, Issue, Severity, Source


def _issue(slide: int, shape_id: int, category: Category = Category.COLOR) -> Issue:
    issue = Issue(
        category=category,
        severity=Severity.ERROR,
        message="off-palette",
        source=Source.RULE,
        rule_id="color.text.off_palette",
        slide=slide,
        shape=f"Shape {shape_id}",
        shape_id=shape_id,
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

def test_a_named_shape_is_held_and_the_rest_of_the_slide_is_not() -> None:
    """The case the shape list exists for: a real colour system on a slide
    that also has a heading somebody typed the wrong blue into."""
    system = _issue(19, 4)
    mistake = _issue(19, 88)
    kept, remaining = _keep_intentional_colors([system, mistake], [_keep(19, [4])])
    assert [i.shape_id for i, _ in kept] == [4]
    assert [i.shape_id for i in remaining] == [88]


def test_an_empty_shape_list_holds_the_whole_slide() -> None:
    issues = [_issue(19, 4), _issue(19, 88)]
    kept, remaining = _keep_intentional_colors(issues, [_keep(19, [])])
    assert len(kept) == 2 and remaining == []


def test_only_colour_work_is_held() -> None:
    """A slide whose greys are deliberate still gets its shapes aligned."""
    colour = _issue(19, 4)
    space = _issue(19, 4, category=Category.SPACE)
    kept, remaining = _keep_intentional_colors([colour, space], [_keep(19, [4])])
    assert [i.category for i, _ in kept] == [Category.COLOR]
    assert [i.category for i in remaining] == [Category.SPACE]


def test_other_slides_are_untouched() -> None:
    here, elsewhere = _issue(19, 4), _issue(20, 4)
    kept, remaining = _keep_intentional_colors([here, elsewhere], [_keep(19, [4])])
    assert [i.slide for i, _ in kept] == [19]
    assert [i.slide for i in remaining] == [20]


def test_no_verdict_means_correct_it() -> None:
    """No opinion is not a keep. The default is and stays 'correct it'."""
    issues = [_issue(19, 4)]
    assert _keep_intentional_colors(issues, None) == ([], issues)
    assert _keep_intentional_colors(issues, []) == ([], issues)


def test_a_change_verdict_holds_nothing() -> None:
    intent = ColorIntent(slide=19, keep=False, shape_ids=[4], confidence=0.99)
    issues = [_issue(19, 4)]
    kept, remaining = _keep_intentional_colors(issues, [intent])
    assert kept == [] and remaining == issues


def test_the_finding_is_kept_not_deleted() -> None:
    """The held finding is handed back whole, so the report can still show it
    and the designer can still act on it."""
    issue = _issue(19, 4)
    kept, _remaining = _keep_intentional_colors([issue], [_keep(19, [4])])
    held_issue, intent = kept[0]
    assert held_issue is issue
    assert intent.scheme and intent.why
