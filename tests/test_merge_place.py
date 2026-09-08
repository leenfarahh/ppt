"""Folding an AI restatement into the rule finding it restates.

The AI is asked to name what it restates, by ref, and when it does the merge
has always worked: on one real run 13 of 19 rule findings absorbed a
restatement. On another run against the same tool the model labelled nothing,
and 6 of its 11 findings landed in the report beside a rule finding saying the
same thing about the same shape -- "The title placeholder is empty" next to
`title.missing` on 'Title 1'.

That is worse than a duplicate. The rule finding carries a fixer and the
restatement cannot, so one defect appeared twice, once correctable and once
needing a designer, and the count of things needing a designer was inflated by
findings that did not need one.

So an unlabelled AI finding is matched on where it sits. What these pin down
is the restraint in it: only when exactly one rule finding sits there.
"""

from __future__ import annotations

from formatting_tool.models import Category, Issue, Severity, Source
from formatting_tool.report.merge import merge_issues


def _rule(category: Category, rule_id: str, **kwargs) -> Issue:
    return Issue(
        category=category,
        severity=kwargs.pop("severity", Severity.ERROR),
        message=kwargs.pop("message", f"{rule_id} finding"),
        source=Source.RULE,
        rule_id=rule_id,
        deck="messy.pptx",
        **kwargs,
    )


def _ai(category: Category, **kwargs) -> Issue:
    return Issue(
        category=category,
        severity=kwargs.pop("severity", Severity.ERROR),
        message=kwargs.pop("message", "the model's wording"),
        source=Source.AI,
        deck="messy.pptx",
        confidence=kwargs.pop("confidence", 0.9),
        **kwargs,
    )


def test_a_ref_still_decides_it() -> None:
    """The labelled path is the good one and must not have changed."""
    rule = _rule(Category.TITLE, "title.missing", slide=4, shape="Title 1")
    ai = _ai(Category.TITLE, slide=4, shape="Title 1", confirms="R1")

    merged = merge_issues([rule], [ai], ref_lookup={"R1": rule})

    assert merged == [rule]


def test_an_unlabelled_restatement_folds_into_the_finding_it_sits_on() -> None:
    rule = _rule(Category.TITLE, "title.missing", slide=4, shape="Title 1")
    ai = _ai(
        Category.TITLE, slide=4, shape="Title 1",
        message="The title placeholder is empty.",
        suggestion="Add a title to the slide.",
    )

    merged = merge_issues([rule], [ai])

    assert merged == [rule]
    # The rule finding keeps its identity and gains what the AI added.
    assert merged[0].rule_id == "title.missing"
    assert merged[0].suggestion == "Add a title to the slide."
    assert merged[0].confidence == 0.9


def test_the_shape_name_is_matched_the_way_a_model_returns_it() -> None:
    """The AI is given names, not ids, and returns them with its own spacing
    and case."""
    rule = _rule(Category.LOGO, "logo.missing", slide=1, shape="Logo 3")
    ai = _ai(Category.LOGO, slide=1, shape="  logo 3 ")

    assert merge_issues([rule], [ai]) == [rule]


def test_two_findings_in_one_place_are_left_alone() -> None:
    """A real case: "48pt and shrinks to fit, exceeding the 22pt maximum" is
    one sentence restating two rule findings. Folding it into whichever came
    first attaches the model's explanation to the wrong one."""
    a = _rule(Category.FONT_SIZE, "size.role.out_of_range", slide=1, shape="Subtitle 2")
    b = _rule(Category.FONT_SIZE, "size.autofit_shrink", slide=1, shape="Subtitle 2")
    ai = _ai(Category.FONT_SIZE, slide=1, shape="Subtitle 2")

    merged = merge_issues([a, b], [ai])

    assert len(merged) == 3
    assert ai in merged


def test_one_rule_finding_absorbs_at_most_one_restatement() -> None:
    """Absorbing overwrites what the previous one contributed, so the second
    stays a finding of its own rather than silently replacing the first."""
    rule = _rule(Category.SPACE, "space.safe_margin", slide=2, shape="Text 2")
    first = _ai(Category.SPACE, slide=2, shape="Text 2", suggestion="first")
    second = _ai(Category.SPACE, slide=2, shape="Text 2", suggestion="second")

    merged = merge_issues([rule], [first, second])

    assert len(merged) == 2
    assert merged[0].suggestion == "first" or merged[1].suggestion == "first"
    assert second in merged


def test_a_finding_the_rules_never_made_survives_untouched() -> None:
    """The AI layer exists to see what the rules cannot. Nothing here may cost
    the report one of those."""
    rule = _rule(Category.SPACE, "space.safe_margin", slide=2, shape="Text 2")
    new = _ai(
        Category.TYPOGRAPHY, slide=3, shape="Oval 9",
        message="The shape contains extra trailing empty lines.",
    )

    merged = merge_issues([rule], [new])

    assert len(merged) == 2
    assert new in merged


def test_a_deck_level_ai_finding_does_not_swallow_a_deck_level_rule() -> None:
    """Both carry slide=None and shape=None, so they share a place. They are
    matched only when exactly one rule finding is there -- which is the point
    of the uniqueness test, not an accident of it."""
    one = _rule(Category.SPACE, "space.alignment_grid")
    two = _rule(Category.SPACE, "space.repeat_out_of_line")
    ai = _ai(Category.SPACE, message="Content bleeds past the side margins.")

    merged = merge_issues([one, two], [ai])

    assert len(merged) == 3
    assert ai in merged


def test_a_low_confidence_finding_is_still_dropped_before_any_of_this() -> None:
    rule = _rule(Category.TITLE, "title.missing", slide=4, shape="Title 1")
    ai = _ai(Category.TITLE, slide=4, shape="Title 1", confidence=0.2)

    merged = merge_issues([rule], [ai], min_confidence=0.5)

    assert merged == [rule]
    assert merged[0].confidence is None
