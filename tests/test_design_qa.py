"""The design check: what the model is shown, what is believed, what is applied.

Three things are worth holding still here, and none of them needs a model or a
copy of PowerPoint.

The shape map is the only frame of reference the model and this tool share. A
ref that is not on it, a name instead of a ref, a percentage measured against
the wrong edge: each one ends with a verdict pointing at a shape nobody meant.

What comes back is not trusted. The vocabulary is small on purpose, and a word
outside it has to read as "unlabelled" rather than as a new behaviour -- which
is a thing to test rather than to hope for, because the model picking a fifth
action is exactly the case nobody would notice.

And the step is bounded. 40pt and 9pt are the ends of it, and a step that would
pass either is not taken at all rather than clipped to the bound, because a
clipped grow is a shrink and nobody asked for one.
"""

from __future__ import annotations

from formatting_tool.ai.designqa import (
    build_shape_map,
    deck_issues_from_response,
    review_from_response,
    DeckIssue,
    ShapeVerdict,
    SlideIssue,
    SlideReview,
)
from formatting_tool.apply.qafix import (
    CEILING_PT,
    FLOOR_PT,
    breaks_mid_word,
    centred_on,
    next_size,
    steps_from,
)
from formatting_tool.designqa import (
    DesignQaReport,
    comments_for,
    outstanding,
    steps_for,
)
from formatting_tool.models import (
    DeckProfile,
    Geometry,
    ParagraphProfile,
    RunProfile,
    ShapeProfile,
    SlideProfile,
    TextRole,
)

WIDE, TALL = 13.333, 7.5


def _shape(name, left, top, width, height, **kwargs):
    text = kwargs.pop("text", "")
    size_pt = kwargs.pop("size_pt", None)
    paragraphs = []
    if text:
        paragraphs = [
            ParagraphProfile(
                text=text, runs=[RunProfile(text=text, size_pt=size_pt)]
            )
        ]
    return ShapeProfile(
        shape_id=kwargs.pop("shape_id", abs(hash(name)) % 9999),
        name=name,
        shape_type=kwargs.pop("shape_type", "TEXT_BOX (17)"),
        geometry=Geometry(left_in=left, top_in=top, width_in=width, height_in=height),
        text=text,
        paragraphs=paragraphs,
        **kwargs,
    )


def _slide(*shapes):
    return SlideProfile(number=1, shapes=list(shapes))


def _circle_holding_an_icon():
    """A component from a real deck: a circle, an icon inside it, off-centre.

    The circle spans 8% to 16% across the slide. An icon centred in it would
    run 10% to 14%; this one runs 9% to 13%, which is the defect a designer
    sees and no rule measures.
    """
    icon = _shape("Icon 7", 1.15, 2.3, 0.5, 0.5, shape_type="PICTURE (13)")
    icon.graphic_colors = ["#FFFFFF"]
    return ShapeProfile(
        shape_id=5, name="Circle 5", shape_type="GROUP (6)",
        geometry=Geometry(left_in=1.0, top_in=2.0, width_in=1.0, height_in=1.0),
        is_group=True,
        children=[_shape("Oval 6", 1.0, 2.0, 1.0, 1.0), icon],
    )


# --------------------------------------------------------------------------- #
# The shape map
# --------------------------------------------------------------------------- #

def test_the_map_places_a_shape_in_percentages_of_the_slide():
    """The model is looking at a picture. Inches mean nothing to it; the edge
    of the slide is the only thing it and this tool both have."""
    slide = _slide(
        _shape("Title 1", 1.3333, 0.75, 6.6665, 1.5, role=TextRole.TITLE,
               text="Our approach", size_pt=28)
    )
    listing, refs = build_shape_map(slide, WIDE, TALL)

    assert list(refs) == ["s1"]
    assert "left 10.0%" in listing and "top 10.0%" in listing
    assert "width 50.0%" in listing and "height 20.0%" in listing
    assert "'Title 1' (title)" in listing
    assert "28pt" in listing
    assert "'Our approach'" in listing


def test_the_masters_furniture_is_not_reviewed():
    """A footer and a slide number are on every slide and are the template's
    decision, not this slide's. A verdict on one is a verdict on the master."""
    slide = _slide(
        _shape("Footer", 0.5, 7.0, 4.0, 0.3, placeholder_type="FOOTER (15)"),
        _shape("Slide Number", 12.0, 7.0, 0.8, 0.3,
               placeholder_type="SLIDE_NUMBER (13)"),
        _shape("Body 2", 1.0, 2.0, 11.0, 4.0, text="Copy"),
    )
    _listing, refs = build_shape_map(slide, WIDE, TALL)

    assert [s.name for s in refs.values()] == ["Body 2"]


def test_a_shape_with_no_area_is_not_listed():
    slide = _slide(
        _shape("Ghost", 1.0, 1.0, 0.0, 0.0),
        _shape("Real", 1.0, 1.0, 2.0, 2.0),
    )
    _listing, refs = build_shape_map(slide, WIDE, TALL)
    assert [s.name for s in refs.values()] == ["Real"]


def test_the_map_goes_inside_a_group():
    """The icon inside the circle is where the defect usually is, and a list
    that names the group and stops has nowhere to put a verdict about it."""
    listing, refs = build_shape_map(_slide(_circle_holding_an_icon()), WIDE, TALL)

    assert list(refs) == ["s1", "s1.1", "s1.2"]
    assert refs["s1.2"].name == "Icon 7"
    assert "s1.2. 'Icon 7' (icon) inside 'Circle 5'" in listing
    # Slide coordinates at every level, so a child and its parent can be
    # compared directly. `deck_reader` composes them; a child's own numbers
    # are in a private coordinate system the group declares.
    assert "s1. 'Circle 5' (group): left 7.5%, top 26.7%" in listing
    # The icon's own gaps: 1.1% of the slide on its left, 1.6% on its right,
    # which is the whole of what "off-centre" means and what whole percentages
    # rounded away.
    assert "s1.2. 'Icon 7' (icon) inside 'Circle 5': left 8.6%, top 30.7%" in listing


def test_a_composition_of_many_parts_is_left_whole():
    """Forty boxes in an org chart is an arrangement, not forty decisions, and
    listing it would spend the whole map on one drawing."""
    many = ShapeProfile(
        shape_id=9, name="Chart 9", shape_type="GROUP (6)",
        geometry=Geometry(left_in=1.0, top_in=1.0, width_in=8.0, height_in=4.0),
        is_group=True,
        children=[_shape(f"Box {i}", 1.0 + i * 0.4, 1.0, 0.3, 0.3) for i in range(20)],
    )
    _listing, refs = build_shape_map(_slide(many), WIDE, TALL)
    assert list(refs) == ["s1"]


def test_a_speck_inside_a_group_is_not_listed():
    group = ShapeProfile(
        shape_id=3, name="Badge 3", shape_type="GROUP (6)",
        geometry=Geometry(left_in=1.0, top_in=1.0, width_in=1.0, height_in=1.0),
        is_group=True,
        children=[
            _shape("Dot 4", 1.0, 1.0, 0.01, 0.01),
            _shape("Number 5", 1.2, 1.2, 0.6, 0.6, text="3"),
        ],
    )
    _listing, refs = build_shape_map(_slide(group), WIDE, TALL)
    assert [s.name for s in refs.values()] == ["Badge 3", "Number 5"]


def test_a_slide_with_no_size_has_no_map():
    """Dividing by a zero-width slide would put every shape at the origin,
    which reads as a map rather than as the absence of one."""
    listing, refs = build_shape_map(_slide(_shape("A", 1, 1, 2, 2)), 0.0, 0.0)
    assert (listing, refs) == ("", {})


# --------------------------------------------------------------------------- #
# What comes back
# --------------------------------------------------------------------------- #

def _refs():
    slide = _slide(
        _shape("Title 1", 1.3333, 0.75, 6.6665, 1.5, role=TextRole.TITLE,
               shape_id=11, text="Our approach"),
        _shape("Caption 4", 1.0, 6.0, 3.0, 0.75, shape_id=12, text="Source"),
    )
    _listing, refs = build_shape_map(slide, WIDE, TALL)
    return refs


def test_a_verdict_is_anchored_to_the_shape_it_names():
    review = review_from_response(
        {
            "shapes": [
                {"shape": "s2", "status": "issue", "issue": "too_small",
                 "action": "grow", "note": "the caption is unreadable"},
            ],
            "slide_issues": ["the right half of the slide is empty"],
        },
        1, _refs(), (WIDE, TALL),
    )

    assert review.reviewed
    [verdict] = review.verdicts
    assert (verdict.shape, verdict.shape_id) == ("Caption 4", 12)
    assert verdict.executable
    assert verdict.box == (0.07500, 0.8, 0.22501, 0.1)
    assert [(i.note, i.task) for i in review.slide_issues] == [
        ("the right half of the slide is empty", "")
    ]


def test_a_shape_that_was_never_sent_is_dropped():
    """An invented ref is the model having invented a shape, and the nearest
    real shape to an invented one is a different shape on somebody's deck."""
    review = review_from_response(
        {"shapes": [
            {"shape": "s9", "status": "issue", "issue": "overlap",
             "action": "shrink", "note": "collides"},
        ], "slide_issues": []},
        1, _refs(), (WIDE, TALL),
    )
    assert review.verdicts == []


def test_the_verdict_for_an_off_centre_icon_survives_the_whitelist():
    """The two defects the first version had no word for: an icon low inside
    its circle, and a placeholder still showing its prompt text."""
    slide = _slide(_circle_holding_an_icon())
    _listing, refs = build_shape_map(slide, WIDE, TALL)

    review = review_from_response(
        {"shapes": [
            {"shape": "s1.2", "status": "issue", "issue": "off_center",
             "action": "none", "note": "the icon sits low and left in its circle"},
        ], "slide_issues": []},
        1, refs, (WIDE, TALL),
    )
    [verdict] = review.verdicts
    assert (verdict.shape, verdict.issue) == ("Icon 7", "off_center")
    assert verdict.role == "icon"
    assert not verdict.executable      # a font step does not centre an icon


def test_a_broken_word_is_a_cut_off():
    """'Proportionalit / y' is not clipping and is the same defect: the box
    cannot show the words it was given."""
    review = review_from_response(
        {"shapes": [
            {"shape": "s1", "status": "issue", "issue": "cut_off",
             "action": "shrink", "note": "'Proportionality' breaks mid-word"},
        ], "slide_issues": []},
        1, _refs(), (WIDE, TALL),
    )
    [verdict] = review.verdicts
    assert (verdict.issue, verdict.action) == ("cut_off", "shrink")
    assert verdict.executable


def test_a_word_outside_the_vocabulary_is_not_an_action():
    review = review_from_response(
        {"shapes": [
            {"shape": "s1", "status": "issue", "issue": "ugly",
             "action": "move_left", "note": "it sits too far in"},
        ], "slide_issues": []},
        1, _refs(), (WIDE, TALL),
    )
    [verdict] = review.verdicts
    assert (verdict.action, verdict.issue) == ("none", "")
    assert not verdict.executable
    assert verdict.note == "it sits too far in"


def test_a_shape_the_model_passed_carries_no_action():
    """A font step on a shape it has just called fine is not a correction, it
    is a change nobody asked for."""
    review = review_from_response(
        {"shapes": [
            {"shape": "s1", "status": "ok", "issue": "too_big",
             "action": "shrink", "note": ""},
        ], "slide_issues": []},
        1, _refs(), (WIDE, TALL),
    )
    [verdict] = review.verdicts
    assert (verdict.status, verdict.action, verdict.issue) == ("ok", "none", "")


def test_a_shape_answered_twice_keeps_the_first_answer():
    review = review_from_response(
        {"shapes": [
            {"shape": "s1", "status": "issue", "issue": "too_big",
             "action": "shrink", "note": "first"},
            {"shape": "s1", "status": "ok", "issue": "none",
             "action": "none", "note": "second"},
        ], "slide_issues": []},
        1, _refs(), (WIDE, TALL),
    )
    assert [v.note for v in review.verdicts] == ["first"]


# --------------------------------------------------------------------------- #
# Across the deck
# --------------------------------------------------------------------------- #

def test_a_mismatch_names_the_slides_it_is_about():
    issues = deck_issues_from_response(
        {"deck_issues": [
            {"kind": "position", "slides": [4, 7],
             "note": "the title sits lower on slide 7 than on slide 4"},
        ]},
        [4, 5, 6, 7],
    )
    assert [(i.kind, i.slides) for i in issues] == [("position", [4, 7])]


def test_a_mismatch_about_a_slide_nobody_looked_at_is_dropped():
    """A slide number is the only handle these have -- nothing validates them
    later -- so one naming slide 40 of a deck compared up to 12 would send a
    designer to a slide the model never saw."""
    issues = deck_issues_from_response(
        {"deck_issues": [
            {"kind": "spacing", "slides": [40], "note": "the margin is tighter"},
            {"kind": "spacing", "slides": [], "note": "something"},
        ]},
        [1, 2, 3],
    )
    assert issues == []


def test_a_mismatch_of_an_unknown_kind_is_kept_as_other():
    """The note is the finding. A kind the page cannot render is a reason to
    file it under `other`, not a reason to throw the sentence away."""
    [issue] = deck_issues_from_response(
        {"deck_issues": [
            {"kind": "vibes", "slides": [2], "note": "slide 2 feels heavier"},
        ]},
        [1, 2],
    )
    assert issue.kind == "other" and issue.note == "slide 2 feels heavier"


def test_a_mismatch_with_no_sentence_is_nothing_at_all():
    assert deck_issues_from_response(
        {"deck_issues": [{"kind": "color", "slides": [1, 2], "note": "  "}]}, [1, 2]
    ) == []


# --------------------------------------------------------------------------- #
# The bounded step
# --------------------------------------------------------------------------- #

def test_a_step_is_one_notch_rounded_to_a_half_point():
    assert next_size(24.0, grow=True) == 27.5     # 27.6 rounded
    assert next_size(12.0, grow=False) == 10.0    # 10.2 rounded


def test_a_step_that_would_pass_a_bound_is_not_taken():
    """Not clipped to the bound: clipping a grow at the ceiling makes it a
    shrink, which is the opposite of what was asked for."""
    assert next_size(CEILING_PT, grow=True) is None
    assert next_size(CEILING_PT + 8, grow=True) is None
    assert next_size(FLOOR_PT, grow=False) is None
    assert next_size(FLOOR_PT - 1, grow=False) is None


def test_a_step_stops_at_the_bound_rather_than_passing_it():
    assert next_size(36.0, grow=True) == CEILING_PT      # 41.4 held at 40
    assert next_size(10.0, grow=False) == FLOOR_PT       # 8.5 held at 9


def test_a_size_that_is_not_a_size_is_not_stepped():
    assert next_size(0.0, grow=True) is None
    assert next_size(-2.0, grow=False) is None


# --------------------------------------------------------------------------- #
# Centring, and the word that breaks in half
# --------------------------------------------------------------------------- #

def test_centring_is_the_gap_on_one_side_equalling_the_other():
    """The icon-in-a-circle case, as arithmetic: a 32pt icon in a 72pt circle
    sits 20pt in from each edge."""
    assert centred_on((10, 10, 32, 32), (0, 0, 72, 72)) == (20.0, 20.0)


def test_a_shape_already_centred_is_left_alone():
    """Half a point is below what any render shows, and writing it into a
    client's deck is a change with nothing to show for it."""
    assert centred_on((20, 20, 32, 32), (0, 0, 72, 72)) is None
    assert centred_on((20.4, 19.7, 32, 32), (0, 0, 72, 72)) is None


def test_a_parent_with_no_size_cannot_centre_anything():
    assert centred_on((10, 10, 32, 32), (0, 0, 0, 0)) is None


def test_a_word_broken_across_two_lines_is_recognised():
    """'Proportionalit / y': the defect no rule can see, because a .pptx
    stores a paragraph and a box and the renderer decides where the lines
    fall."""
    text = "Requirements, Evidence & Proportionality"
    assert breaks_mid_word(text, ["Requirements,", "Evidence &", "Proportionalit", "y"])


def test_lines_that_break_at_spaces_are_not_a_defect():
    text = "Requirements, Evidence & Proportionality"
    assert not breaks_mid_word(text, ["Requirements,", "Evidence &", "Proportionality"])
    assert not breaks_mid_word(text, [text])
    assert not breaks_mid_word("", [])


def test_lines_that_cannot_be_matched_to_the_text_claim_nothing():
    """Silence rather than a guess: a line this cannot line up with the copy
    is a renderer doing something unexpected, and reporting a break on the
    strength of not understanding it would be inventing one."""
    assert not breaks_mid_word("one two three", ["something", "else entirely"])


# --------------------------------------------------------------------------- #
# What reaches the applier
# --------------------------------------------------------------------------- #

def _verdict(ref, action, shape_id=11, status="issue", slide=1, parent_id=None):
    return ShapeVerdict(
        slide=slide, ref=ref, shape=f"Shape {ref}", shape_id=shape_id,
        role="body", status=status, issue="too_small" if action != "none" else "",
        action=action, note="a note", task=f"do something about {ref}",
        parent_id=parent_id, parent="Circle" if parent_id else "",
    )


def _deck_with_titles(tops: dict) -> DeckProfile:
    """A deck whose slides each carry a title at the given top, in inches."""
    slides = []
    for number, top in tops.items():
        title = ShapeProfile(
            shape_id=number * 100, name=f"Title {number}",
            shape_type="PLACEHOLDER (14)",
            geometry=Geometry(left_in=0.6, top_in=top, width_in=8.0, height_in=0.9),
            placeholder_type="TITLE (1)", role=TextRole.TITLE,
        )
        slides.append(SlideProfile(number=number, shapes=[title]))
    return DeckProfile(path="deck.pptx", width_in=WIDE, height_in=TALL, slides=slides)


def _report():
    return DesignQaReport(
        deck="deck.pptx", generated_at="now", model="test",
        reviews=[
            SlideReview(slide=1, reviewed=True, verdicts=[
                _verdict("s1", "grow", shape_id=11),
                _verdict("s2", "none", shape_id=12),
                _verdict("s3", "shrink", shape_id=13),
            ], slide_issues=[SlideIssue(
                note="the timeline has a stop nothing uses",
                task="drop the empty stop, or give it a label",
            )]),
        ],
    )


def test_only_a_font_action_becomes_a_step():
    steps = steps_from(_report().reviews)
    assert [(s.shape_id, s.direction) for s in steps] == [(11, "grow"), (13, "shrink")]


def test_a_verdict_with_no_shape_id_is_not_applied():
    """Applying by name would edit whichever "Pentagon 7" came first."""
    review = SlideReview(slide=1, reviewed=True,
                         verdicts=[_verdict("s1", "grow", shape_id=None)])
    assert steps_from([review]) == []


def test_ticking_narrows_what_is_applied():
    steps = steps_for(_report(), ["1:s3"])
    assert [s.shape_id for s in steps] == [13]


def test_a_title_out_of_line_with_the_deck_is_moved_to_the_median():
    """The model says which slides disagree; arithmetic says where the title
    belongs. Median rather than mean, so the slide being reported cannot drag
    the answer towards itself."""
    report = _report()
    report.profile = _deck_with_titles({1: 0.4, 2: 0.42, 3: 0.41, 7: 1.2})
    report.deck_issues = [DeckIssue(
        kind="position", slides=[7], note="the title on 7 sits lower",
        task="move it up to match",
    )]

    [task] = [t for t in report.tasks if t.kind == "deck"]
    assert task.fixable and task.op == "align"

    [step] = [s for s in steps_for(report, ["deck:0"]) if s.op == "align"]
    assert step.slide == 7 and step.shape_id == 700
    # The median of 0.40, 0.41, 0.42 and 1.20, which the slide that is out of
    # line cannot drag towards itself the way a mean would.
    assert step.top_in == 0.415


def test_a_deck_with_too_few_titles_has_no_usual_position():
    """Two slides agreeing is a coincidence, not a convention."""
    report = _report()
    report.profile = _deck_with_titles({1: 0.4, 7: 1.2})
    report.deck_issues = [DeckIssue(kind="position", slides=[7], note="lower",
                                    task="move it")]

    assert not [t for t in report.tasks if t.kind == "deck"][0].fixable
    assert steps_for(report, ["deck:0"]) == []


def test_a_mismatch_that_is_not_about_position_is_not_moved():
    """A colour that differs between slides has no coordinates to align."""
    report = _report()
    report.profile = _deck_with_titles({1: 0.4, 2: 0.4, 3: 0.4, 7: 1.2})
    report.deck_issues = [DeckIssue(kind="color", slides=[7], note="red not black",
                                    task="recolour it")]

    assert steps_for(report, ["deck:0"]) == []


def test_a_tick_naming_something_that_is_not_a_step_is_dropped():
    """The page can only send what it was shown, but what it was shown
    includes notes, and a note is not an instruction."""
    assert steps_for(_report(), ["1:s2"]) == []
    assert steps_for(_report(), ["2:s1"]) == []


def test_a_mismatch_with_nothing_to_measure_against_is_still_work():
    """Without a deck to take a median from there is no target, so the
    mismatch is a task rather than a correction -- and it is still on the
    list."""
    report = _report()
    report.deck_issues = [
        DeckIssue(kind="position", slides=[1, 2],
                  note="the title sits lower on slide 2",
                  task="move the title on slide 2 up to match the rest"),
    ]
    [task] = [t for t in report.tasks if t.kind == "deck"]

    assert task.what == "move the title on slide 2 up to match the rest"
    assert not task.fixable
    assert report.stats["mismatches"] == 1
    assert all(s.shape_id in (11, 13) for s in steps_for(report, None))


def test_every_finding_becomes_a_task():
    """The whole point of the list: a verdict the applier can act on and an
    observation about an empty column are the same thing to whoever has to
    hand the deck over. What differs is whether ticking it does the work."""
    tasks = _report().tasks

    assert [t.id for t in tasks] == ["1:s1", "1:s2", "1:s3", "slide:1:0"]
    assert [t.fixable for t in tasks] == [True, False, True, False]
    # The instruction, not the observation, is what a task says.
    assert tasks[3].what == "drop the empty stop, or give it a label"
    assert tasks[3].why == "the timeline has a stop nothing uses"


def test_what_cannot_be_ticked_is_what_gets_written_into_the_deck():
    notes = _report().notes
    assert "slide 1: drop the empty stop, or give it a label" in notes
    assert any("do something about s2" in note for note in notes)
    assert not any("s1" in note for note in notes)     # that one is a correction


# --------------------------------------------------------------------------- #
# What a round leaves behind
# --------------------------------------------------------------------------- #

class _Change:
    """A correction that landed, as `apply.qafix` reports one."""

    def __init__(self, slide, shape_id, op):
        self.slide, self.shape_id, self.op = slide, shape_id, op


def test_a_step_that_was_refused_is_still_outstanding():
    """The finding a page loses: off the tick list, absent from the changes,
    and nowhere at all unless something keeps it."""
    report = _report()
    left = outstanding(report, ["1:s1", "1:s3"], [_Change(1, 11, "grow")])

    assert [t.id for t in left] == ["1:s2", "1:s3", "slide:1:0"]


def test_everything_that_was_done_drops_off_the_list():
    report = _report()
    left = outstanding(
        report, ["1:s1", "1:s3"],
        [_Change(1, 11, "grow"), _Change(1, 13, "shrink")],
    )
    assert [t.id for t in left] == ["1:s2", "slide:1:0"]


def test_with_nothing_ticked_everything_is_outstanding():
    assert len(outstanding(_report(), [], [])) == len(_report().tasks)


def test_a_task_becomes_a_comment_anchored_on_its_shape():
    """A comment on the shape it concerns rather than in a corner: the
    designer clicks the note and PowerPoint takes them to the thing."""
    report = _report()
    report.width_in, report.height_in = 13.333, 7.5
    report.reviews[0].verdicts[1] = ShapeVerdict(
        slide=1, ref="s2", shape="Chart 4", shape_id=12, role="chart",
        status="issue", issue="crowded", action="none",
        note="the legend overlaps the plot", task="move the legend below the plot",
        box=(0.5, 0.25, 0.4, 0.3),
    )
    [comment] = [
        c for c in comments_for(report, report.tasks) if c.slide == 1
        and "Chart 4" in c.body
    ]

    assert comment.body.startswith("Chart 4: move the legend below the plot")
    assert "the legend overlaps the plot" in comment.body
    assert (comment.left_pt, comment.top_pt) == (480.0, 135.0)


def test_a_task_about_the_whole_slide_sits_in_the_corner():
    report = _report()
    [comment] = [c for c in comments_for(report, report.tasks)
                 if "empty stop" in c.body]
    assert (comment.left_pt, comment.top_pt) == (12.0, 12.0)


def test_the_stats_count_what_the_page_shows():
    stats = _report().stats
    assert stats["slides"] == 1 and stats["reviewed"] == 1
    assert stats["shapes"] == 3 and stats["issues"] == 3
    assert stats["tasks"] == 4 and stats["actions"] == 2 and stats["notes"] == 2
