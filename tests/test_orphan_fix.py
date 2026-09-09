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


# --------------------------------------------------------------------------- #
# The pair has to fit
# --------------------------------------------------------------------------- #
#
# A non-breaking space makes one token out of two words, and PowerPoint does
# not wrap a token wider than its box -- it breaks it, mid-word. On a narrow
# pill "Demand is no longer speculative" came out "speculati / ve" and
# "commercial threshold" came out "thre / shold": the fix traded an untidy
# last line for a word snapped in half, which is the worse of the two and the
# one a client sees first.
#
# The widest line the renderer drew is the evidence, and it is evidence of
# exactly the right thing: that much text demonstrably fits on one line in
# this box, at this font, at this size.

from formatting_tool.apply.fixers import _pair_fits, _width_units   # noqa: E402

# The two boxes off the screenshot this was reported from, as rendered.
PILL_LINES = ["Demand is no", "longer", "speculative"]
THRESHOLD_LINES = ["Economics", "crossed the", "commercial", "threshold"]
ROOMY_LINES = ["The quick brown fox jumps over the lazy", "dog everywhere"]


def _widest(lines: list[str]) -> int:
    return max(len(line.strip()) for line in lines)


def test_a_pair_wider_than_the_box_is_refused() -> None:
    assert not _pair_fits("longer speculative", _widest(PILL_LINES))
    assert not _pair_fits("commercial threshold", _widest(THRESHOLD_LINES))


def test_a_pair_that_fits_is_still_bound() -> None:
    """The guard must not disable the fix. A stranded word in a box with room
    is what the non-breaking space is for."""
    assert _pair_fits("dog everywhere", _widest(ROOMY_LINES))
    assert _pair_fits("word up", 48)


def test_the_decision_is_not_marginal_on_the_real_cases() -> None:
    """Character counts are a proxy for width, so a guard that only just
    decided would be a coin toss. These miss by a wide margin either way."""
    tight = _width_units("longer speculative") / _width_units("n" * _widest(PILL_LINES))
    roomy = _width_units("dog everywhere") / _width_units("n" * _widest(ROOMY_LINES))

    assert tight > 1.2          # refused, and clearly
    assert roomy < 0.5          # bound, and clearly


def test_narrow_and_wide_characters_do_not_measure_the_same() -> None:
    """The one error a bare character count makes."""
    assert _width_units("llllllllll") < _width_units("WWWWWWWWWW")


def test_with_no_measurement_the_fix_goes_ahead() -> None:
    """An older report carries no line evidence. Refusing everything
    unmeasured would disable the fix wherever the report predates this."""
    assert _pair_fits("anything at all", None)
    assert _pair_fits("anything at all", 0)


def test_the_fixer_refuses_and_says_why(tmp_path: Path) -> None:
    """End to end: the narrow-box case is declined, the text is untouched, and
    the reason names the real hazard rather than shrugging."""
    from pptx import Presentation

    text = "Demand is no longer speculative"
    deck, shape_id = _deck(tmp_path, text)
    issue = _issue(
        "Last line is a single stranded word ('speculative').",
        "speculative", shape_id=shape_id,
    )
    issue.widest_line_chars = _widest(PILL_LINES)
    out = tmp_path / "clean.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert result.applied == []
    detail = result.skipped[0].detail
    assert "breaks a token it cannot fit" in detail
    # And nothing was written into the copy.
    after = Presentation(str(out)).slides[0].shapes[0].text_frame.text
    assert NBSP not in after
    assert after == text


def test_the_fixer_still_binds_when_there_is_room(tmp_path: Path) -> None:
    from pptx import Presentation

    text = "The quick brown fox jumps over the lazy dog everywhere"
    deck, shape_id = _deck(tmp_path, text)
    issue = _issue(
        "Last line is a single stranded word ('everywhere').",
        "everywhere", shape_id=shape_id,
    )
    issue.widest_line_chars = _widest(ROOMY_LINES)
    out = tmp_path / "clean.pptx"

    result = apply_fixes(deck, [issue], out, selected=[issue.id])

    assert len(result.applied) == 1
    after = Presentation(str(out)).slides[0].shapes[0].text_frame.text
    assert f"dog{NBSP}everywhere" in after
