"""Binding a stranded word to the one before it.

`typography.orphan_widow` never fired: it needs to know where a line breaks,
that is not in the file -- a .pptx stores a paragraph and a box, and the
renderer decides -- and the only provider was a documented sketch whose
`_load` raised. So the rule was silent and the finding was never made.

With PowerPoint reading the real breaks it fires, and the fix is a
non-breaking space. Measured end to end on a deck built for it:

    before   ['Regenerating the ', 'environment and ', 'transforming our economy ',
              'and promoting ', 'sustainability']
    after    [..., 'and ', 'promoting\\xa0sustainability']

Widening the box was the other candidate and is worse: it changes the
composition and re-wraps the whole paragraph, so it can strand a different
word instead of no word. "Pull the last word back" is not a third option, it
is what this does -- a word cannot move without the break moving.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from formatting_tool.apply import apply_fixes, fixer_for
from formatting_tool.apply.fixers import LeaveAlone, fix_orphan_widow
from formatting_tool.models import Category, Issue, Severity, Source

NBSP = " "


def _issue(message: str, found: str, shape="Body", shape_id=None) -> Issue:
    issue = Issue(
        category=Category.TYPOGRAPHY,
        severity=Severity.WARNING,
        message=message,
        source=Source.RULE,
        rule_id="typography.orphan_widow",
        slide=1,
        shape=shape,
        shape_id=shape_id,
        deck="messy.pptx",
        found=found,
    )
    issue.id = issue.fingerprint()
    return issue


def _deck(tmp_path: Path, text: str, bold_last: bool = False) -> tuple[Path, int]:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3.4), Inches(3))
    box.name = "Body"
    frame = box.text_frame
    frame.word_wrap = True
    if bold_last:
        head, _, tail = text.rpartition(" ")
        paragraph = frame.paragraphs[0]
        first = paragraph.add_run()
        first.text = head + " "
        last = paragraph.add_run()
        last.text = tail
        last.font.bold = True
    else:
        frame.text = text
    for paragraph in frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(20)
    path = tmp_path / "messy.pptx"
    prs.save(str(path))
    return path, box.shape_id


def _text_of(path: Path) -> str:
    from pptx import Presentation

    return Presentation(str(path)).slides[0].shapes[0].text_frame.text


def test_the_stranded_word_is_bound_to_the_one_before_it(tmp_path: Path) -> None:
    deck, shape_id = _deck(
        tmp_path, "Regenerating the environment and promoting sustainability"
    )
    issue = _issue(
        "Last line is a single stranded word ('sustainability').",
        "sustainability", shape_id=shape_id,
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    assert f"promoting{NBSP}sustainability" in _text_of(out)


def test_no_word_is_changed(tmp_path: Path) -> None:
    """One character, and it is a space. Undone by deleting it."""
    text = "Regenerating the environment and promoting sustainability"
    deck, shape_id = _deck(tmp_path, text)
    issue = _issue(
        "Last line is a single stranded word ('sustainability').",
        "sustainability", shape_id=shape_id,
    )
    out = tmp_path / "fixed.pptx"

    apply_fixes(deck, [issue], out, selected=[issue.id])

    assert _text_of(out).replace(NBSP, " ") == text


def test_the_space_is_found_when_it_is_in_another_run(tmp_path: Path) -> None:
    """A bolded last word is enough to split the space from the word it
    precedes, and then the space is the tail of the run before."""
    deck, shape_id = _deck(
        tmp_path, "Regenerating the environment and promoting sustainability",
        bold_last=True,
    )
    issue = _issue(
        "Last line is a single stranded word ('sustainability').",
        "sustainability", shape_id=shape_id,
    )
    out = tmp_path / "fixed.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    assert NBSP in _text_of(out)


def test_a_short_last_line_is_not_answered_by_binding_two_words() -> None:
    """The same rule reports a stub and a title wrapping past its limit.
    Neither is fixed by binding two words: a stub is a stub whichever line it
    sits on."""
    issue = _issue("Last line is a 4-character stub ('etc.').", "etc.")

    assert fixer_for(issue) is fix_orphan_widow
    with pytest.raises(LeaveAlone) as raised:
        fix_orphan_widow(object(), issue, None)
    assert "stranded word" in str(raised.value)


def test_a_title_wrapping_too_far_is_not_either() -> None:
    issue = _issue("Title wraps to 3 lines.", "3 lines")

    with pytest.raises(LeaveAlone):
        fix_orphan_widow(object(), issue, None)


def test_copy_that_has_changed_since_the_report_is_left_alone(
    tmp_path: Path,
) -> None:
    deck, shape_id = _deck(tmp_path, "Something else entirely now")
    issue = _issue(
        "Last line is a single stranded word ('sustainability').",
        "sustainability", shape_id=shape_id,
    )

    result = apply_fixes(deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id])

    assert not result.applied
    assert "no longer there" in result.skipped[0].detail


def test_a_single_word_paragraph_has_nothing_to_bind_to(tmp_path: Path) -> None:
    deck, shape_id = _deck(tmp_path, "Sustainability")
    issue = _issue(
        "Last line is a single stranded word ('Sustainability').",
        "Sustainability", shape_id=shape_id,
    )

    result = apply_fixes(deck, [issue], tmp_path / "fixed.pptx", selected=[issue.id])

    assert not result.applied
    assert "only one word" in result.skipped[0].detail
