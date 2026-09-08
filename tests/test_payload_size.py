"""What the AI payload carries, and what it stopped carrying.

A 105-slide deck was 105 calls at concurrency three, with 5,123 rule findings
between them. Measured on the real payloads, one dense slide cost:

    system prefix    8,418 ch   identical every call, cache_read=0 every call
    rule findings   30,560 ch   88 findings for ONE slide, 28 of them the same
                                rule on the same shape
    shape digests   33,176 ch   158 shapes, of which 2,920 ch was actual text

None of the three was doing the work it cost. These pin down the two the
payload builder can fix; the third is the explicit cache in `ai.client`.
"""

from __future__ import annotations

import json

from formatting_tool.ai.payload import build_rule_findings, build_slide_digests
from formatting_tool.models import (
    Category,
    Geometry,
    Issue,
    ParagraphProfile,
    RunProfile,
    Severity,
    ShapeProfile,
    SlideProfile,
    Source,
)


def _issue(rule_id: str, slide, shape, found="#A32020") -> Issue:
    return Issue(
        category=Category.COLOR,
        severity=Severity.ERROR,
        message=f"{rule_id} on {shape}",
        source=Source.RULE,
        rule_id=rule_id,
        slide=slide,
        shape=shape,
        found=found,
    )


def _shape(name, **kwargs) -> ShapeProfile:
    return ShapeProfile(
        shape_id=kwargs.pop("shape_id", 1),
        name=name,
        shape_type=kwargs.pop("shape_type", "AUTO_SHAPE"),
        geometry=Geometry(0.0, 0.0, 1.0, 1.0),
        **kwargs,
    )


def _with_text(name, words="NEOM's Vision") -> ShapeProfile:
    shape = _shape(name)
    shape.text = words
    shape.paragraphs = [
        ParagraphProfile(text=words, runs=[RunProfile(text=words)])
    ]
    return shape


# --------------------------------------------------------------------------- #
# Findings: one entry per defect, not per occurrence
# --------------------------------------------------------------------------- #

def test_repeats_of_one_rule_on_one_shape_collapse() -> None:
    """The model was never going to write 28 findings about one shape's
    colour, and it was being handed 28 to read."""
    issues = [_issue("color.text.off_palette", 5, "Body") for _ in range(28)]

    sent = build_rule_findings(issues)

    assert len(sent) == 1
    assert sent[0]["n"] == 28


def test_the_surviving_entry_keeps_the_first_ref() -> None:
    """A restatement names that ref and the merge absorbs into it. The others
    stay in the report untouched, fixable as they always were."""
    issues = [_issue("color.text.off_palette", 5, "Body") for _ in range(3)]

    sent = build_rule_findings(issues)

    assert sent[0]["ref"] == "R1"


def test_different_shapes_stay_separate() -> None:
    issues = [
        _issue("color.text.off_palette", 5, "Body"),
        _issue("color.text.off_palette", 5, "Title"),
        _issue("space.overlap", 5, "Body"),
    ]

    assert len(build_rule_findings(issues)) == 3


def test_a_deck_level_finding_keeps_its_null_slide() -> None:
    """That is how it says it is deck-level, and the batcher reads it to
    decide which findings travel with which slides."""
    sent = build_rule_findings([_issue("color.theme_mismatch", None, None)])

    assert "slide" in sent[0]
    assert sent[0]["slide"] is None


def test_a_single_finding_carries_no_count() -> None:
    sent = build_rule_findings([_issue("space.overlap", 2, "Title")])

    assert "n" not in sent[0]


# --------------------------------------------------------------------------- #
# Shapes: only what the model can judge
# --------------------------------------------------------------------------- #

def _digest(shapes: list[ShapeProfile]) -> list[dict]:
    slide = SlideProfile(number=1, layout_name="Blank", shapes=shapes)
    return build_slide_digests([slide])[0]["shapes"]


def test_a_shape_with_nothing_to_judge_is_not_sent() -> None:
    """On one real slide, 100 of 158 shapes -- including a 0.0017in
    embedded-object stub. The model cannot say anything about them that the
    geometry rules do not already prove."""
    assert _digest([_shape("Freeform 300")]) == []


def test_text_fill_outline_and_images_are_all_kept() -> None:
    kept = [
        _with_text("Body"),
        _shape("Panel", fill_hex="F2F2F2"),
        _shape("Rule", line_hex="A32020"),
        _shape("Photo", image_sha1="abc123"),
        _shape("Title 1", placeholder_type="TITLE"),
    ]

    assert len(_digest(kept)) == len(kept)


def test_a_group_survives_if_any_of_its_parts_does() -> None:
    """The group is how the parts are addressed."""
    group = _shape("Group 39", is_group=True)
    group.children = [_shape("Freeform 1"), _with_text("Label")]

    sent = _digest([group])

    assert len(sent) == 1
    assert len(sent[0]["children"]) == 1        # only the part worth sending


def test_a_group_of_nothing_is_not_sent() -> None:
    group = _shape("Group 40", is_group=True)
    group.children = [_shape("Freeform 1"), _shape("Freeform 2")]

    assert _digest([group]) == []


def test_every_shape_sent_carries_its_id() -> None:
    """It is what makes a proposed fix land on the right shape; names repeat
    within a slide and ids do not."""
    sent = _digest([_with_text("Body")])

    assert "id" in sent[0]


def test_the_digest_is_smaller_than_it_was() -> None:
    """The whole point, stated as a number so it cannot quietly regress."""
    shapes = [_with_text("Body")] + [
        _shape(f"Freeform {i}", shape_id=i) for i in range(100)
    ]

    sent = _digest(shapes)

    assert len(sent) == 1
    assert len(json.dumps(sent)) < 400
