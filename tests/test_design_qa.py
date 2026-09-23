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

from pathlib import Path

from formatting_tool.ai.designqa import (
    build_shape_map,
    deck_issues_from_response,
    review_from_response,
    DeckIssue,
    Member,
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
from formatting_tool.extract import derive_master_spec
from formatting_tool.guidelines import BrandGuidelines
from formatting_tool.designqa import (
    DesignQaReport,
    _deck_fixes,
    _most_common,
    _own_spec,
    comments_for,
    issues_for,
    outstanding,
    steps_for,
)
from formatting_tool.ai.designqa import PROPOSABLE_OPS, _proposal
from formatting_tool.models import (
    Category,
    DeckProfile,
    FixAction,
    Geometry,
    Issue,
    ParagraphProfile,
    RunProfile,
    Severity,
    ShapeProfile,
    SlideProfile,
    Source,
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


def test_centring_finds_the_holder_in_the_file_when_no_group_states_it():
    """A deck usually draws a component as two shapes side by side rather than
    as a group: a circle, and an icon on top of it. No ref relates them, so
    `center` used to arrive with no parent and be refused -- centred on what? --
    and the commonest mechanical defect in a deck built from repeated
    components went to a designer with a sentence."""
    slide = _slide(
        _shape("Card 2", 0.5, 1.5, 3.0, 3.0, shape_id=2),
        _shape("Oval 3", 1.0, 2.0, 1.0, 1.0, shape_id=3),
        _shape("Icon 4", 1.15, 2.3, 0.5, 0.5, shape_id=4,
               shape_type="PICTURE (13)"),
    )
    _listing, refs = build_shape_map(slide, WIDE, TALL)

    review = review_from_response(
        {"shapes": [
            {"shape": "s3", "status": "issue", "issue": "off_center",
             "action": "center", "note": "the icon sits low in its circle"},
        ], "slide_issues": []},
        1, refs, (WIDE, TALL),
    )
    [verdict] = review.verdicts
    # The circle, not the card behind it: the holder is the SMALLEST shape
    # that contains this one, which is the tightest thing it could be sitting
    # in. Centring the icon on the card would walk it across the component.
    assert (verdict.parent, verdict.parent_id) == ("Oval 3", 3)
    assert verdict.executable


def test_a_shape_with_nothing_tight_enough_around_it_is_not_centred():
    """No containing shape, or only ones far too big to be holding anything,
    and the verdict stays a task rather than being centred on a background."""
    slide = _slide(
        _shape("Backdrop 2", 0.0, 0.0, WIDE, TALL, shape_id=2),
        _shape("Caption 3", 1.0, 6.0, 0.6, 0.3, shape_id=3, text="Source"),
    )
    _listing, refs = build_shape_map(slide, WIDE, TALL)

    review = review_from_response(
        {"shapes": [
            {"shape": "s2", "status": "issue", "issue": "off_center",
             "action": "center", "note": "the caption is not centred"},
        ], "slide_issues": []},
        1, refs, (WIDE, TALL),
    )
    [verdict] = review.verdicts
    assert verdict.parent_id is None and not verdict.executable


def test_a_picture_over_the_slide_is_sent_behind_it():
    """The commonest overlap on a real deck is a full-bleed photograph drawn
    in front of the cards it was meant to sit under. Nothing is in the wrong
    place, so no move answers it; which shape was added last is the defect."""
    review = review_from_response(
        {"shapes": [
            {"shape": "s2", "status": "issue", "issue": "overlap",
             "action": "send_to_back",
             "note": "the photograph covers the cards"},
        ], "slide_issues": []},
        1, _refs(), (WIDE, TALL),
    )
    [verdict] = review.verdicts
    assert verdict.action == "send_to_back" and verdict.executable
    [step] = steps_from([review])
    assert (step.op, step.shape_id) == ("send_to_back", 12)


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


def test_the_same_mismatch_under_the_other_word_is_answered_the_same_way():
    """`position` and `alignment` are two words the consistency pass has for
    one defect, and the model picks whichever suits the sentence it is writing.
    Only one of them used to reach any arithmetic, so half of the same finding
    was fixable and half was prose."""
    report = _report()
    report.profile = _deck_with_titles({1: 0.4, 2: 0.42, 3: 0.41, 7: 1.2})
    report.deck_issues = [DeckIssue(
        kind="alignment", slides=[7],
        note="slide 7 starts on a different grid line from the rest",
        task="put it back on the line the rest of the deck uses",
    )]

    [task] = [t for t in report.tasks if t.kind == "deck"]
    assert task.fixable and task.op == "align"
    [step] = [s for s in steps_for(report, ["deck:0"]) if s.op == "align"]
    assert (step.slide, step.top_in) == (7, 0.415)


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
# Proposals: the open half of the vocabulary
# --------------------------------------------------------------------------- #

def _shape_with(name, size_pt=None, color=None, role=TextRole.BODY, shape_id=1):
    run = RunProfile(text="Some copy", size_pt=size_pt, color_hex=color)
    return ShapeProfile(
        shape_id=shape_id, name=name, shape_type="PLACEHOLDER (14)",
        geometry=Geometry(left_in=1.0, top_in=2.0, width_in=8.0, height_in=2.0),
        role=role, text="Some copy",
        paragraphs=[ParagraphProfile(text="Some copy", runs=[run])],
    )


def _deck(sizes_and_colors) -> DeckProfile:
    """A deck of body shapes, one per slide, at the given size and colour."""
    slides = [
        SlideProfile(number=number, shapes=[
            _shape_with(f"Body {number}", size_pt=size, color=color,
                        shape_id=100 + number)
        ])
        for number, (size, color) in enumerate(sizes_and_colors, 1)
    ]
    return DeckProfile(path="deck.pptx", width_in=WIDE, height_in=TALL, slides=slides)


def test_a_proposal_outside_what_this_check_offers_is_not_a_proposal():
    """`ai.schema._fix` accepts the whole of FIX_OPS, which includes deleting a
    shape. A model answering this prompt has no business proposing that."""
    shape = _shape_with("Body 1")
    assert _proposal({"op": "remove_note", "shape_id": 1}, shape) is None
    assert _proposal({"op": "set_font", "font": "Comic Sans"}, shape) is None
    assert all(
        _proposal({"op": op, "size_pt": 12, "hex": "112233", "left_in": 1.0,
                   "top_in": 1.0, "width_in": 2.0, "height_in": 2.0}, shape)
        is not None
        for op in PROPOSABLE_OPS
    )


def test_a_proposal_is_anchored_to_the_shape_the_verdict_names():
    """The ref the schema forced is the anchor. An id that disagrees with it is
    the model contradicting itself about which shape this is."""
    shape = _shape_with("Body 1", shape_id=7)
    action = _proposal({"op": "set_font_size", "size_pt": 12, "shape_id": 999}, shape)

    assert (action.shape_id, action.shape) == (7, "Body 1")


def test_a_proposal_makes_a_verdict_executable_without_a_verb():
    verdict = ShapeVerdict(
        slide=1, ref="s1", shape="Body 1", shape_id=7, role="body",
        status="issue", issue="too_small", action="none",
        note="smaller than the rest", task="set it to 12pt",
        fix=FixAction(op="set_font_size", shape_id=7, size_pt=12.0),
    )
    assert verdict.executable

    report = DesignQaReport(deck="d.pptx", generated_at="now", model="t",
                            reviews=[SlideReview(slide=1, reviewed=True,
                                                 verdicts=[verdict])])
    [issue] = issues_for(report, ["1:s1"])
    assert issue.id == "1:s1" and issue.fix.op == "set_font_size"
    assert issue.source.value == "ai"          # routes to fix_ai_action
    # And it is not also sent to the COM half, which has no word for it.
    assert steps_for(report, ["1:s1"]) == []


def test_a_measured_verb_beats_a_proposal_on_the_same_shape():
    """Both would do something; one of them has a target this tool measured
    and the other has a number the model chose."""
    verdict = ShapeVerdict(
        slide=1, ref="s1", shape="Body 1", shape_id=7, role="body",
        status="issue", issue="too_small", action="grow", note="small",
        task="make it bigger",
        fix=FixAction(op="set_font_size", shape_id=7, size_pt=40.0),
    )
    report = DesignQaReport(deck="d.pptx", generated_at="now", model="t",
                            reviews=[SlideReview(slide=1, reviewed=True,
                                                 verdicts=[verdict])])

    assert issues_for(report, ["1:s1"]) == []
    assert [s.op for s in steps_for(report, ["1:s1"])] == ["grow"]


# --------------------------------------------------------------------------- #
# A mismatch the deck can answer itself
# --------------------------------------------------------------------------- #

def _mismatch_report(profile, kind="type_scale", slides=(1, 2, 3)):
    report = DesignQaReport(
        deck="deck.pptx", generated_at="now", model="t", profile=profile,
        deck_issues=[DeckIssue(kind=kind, slides=list(slides),
                               note="they differ", task="make them match")],
    )
    report.spec = _own_spec(profile)
    return report


def test_the_odd_slide_is_set_to_what_the_majority_uses():
    report = _mismatch_report(_deck([(12, "17191C"), (12, "17191C"), (7, "17191C")]))
    _steps, proposals = _deck_fixes(report, 0, report.deck_issues[0])

    assert [(p.slide, p.fix.op, p.fix.size_pt) for p in proposals] == [
        (3, "set_font_size", 12.0)
    ]
    # Ticking the mismatch selects every correction it needs.
    assert len(issues_for(report, ["deck:0"])) == 1
    assert [t.fixable for t in report.tasks if t.kind == "deck"] == [True]


def test_the_majority_counts_the_slides_the_mismatch_names():
    """A mismatch usually names every slide it is about, so counting only the
    ones left out counts nothing -- which is what made every mismatch come
    back unfixable the first time."""
    report = _mismatch_report(
        _deck([(12, "17191C"), (12, "17191C"), (7, "17191C")]), slides=(1, 2, 3),
    )
    _steps, proposals = _deck_fixes(report, 0, report.deck_issues[0])
    assert proposals and proposals[0].fix.size_pt == 12.0


def test_a_colour_the_deck_uses_on_several_slides_is_one_it_vouches_for():
    """The theme is what the file says; this is what the file does. A body
    colour on every slide is a decision even when no theme entry names it."""
    report = _mismatch_report(
        _deck([(12, "17191C"), (12, "17191C"), (12, "D0211C")]), kind="color",
    )
    _steps, proposals = _deck_fixes(report, 0, report.deck_issues[0])

    assert [(p.slide, p.fix.op, p.fix.hex) for p in proposals] == [
        (3, "recolor_text", "17191C")
    ]
    assert "deck:17191C" in report.spec.palette


def test_a_colour_on_one_slide_alone_is_not_vouched_for():
    """It is as likely to be the mistake being reported as the answer to it."""
    profile = _deck([(12, "17191C"), (12, "17191C"), (12, "D0211C")])
    spec = _own_spec(profile)
    assert "deck:D0211C" not in spec.palette


def test_a_deck_that_has_not_decided_is_not_decided_for():
    """Two slides at 11pt and two at 12pt say the deck has not chosen, and
    picking one would be this tool choosing for it."""
    report = _mismatch_report(
        _deck([(11, "17191C"), (11, "17191C"), (12, "17191C"), (12, "17191C")]),
        slides=(1, 2, 3, 4),
    )
    _steps, proposals = _deck_fixes(report, 0, report.deck_issues[0])
    assert proposals == []
    assert _most_common([1, 1, 2, 2]) is None
    assert _most_common([]) is None


def test_a_mismatch_with_no_countable_answer_stays_a_task():
    """`spacing` has no single number to count and `content` is a missing
    element rather than a wrong value."""
    profile = _deck([(12, "17191C"), (12, "17191C"), (7, "17191C")])
    for kind in ("spacing", "content", "other", "size", "alignment"):
        report = _mismatch_report(profile, kind=kind)
        steps, proposals = _deck_fixes(report, 0, report.deck_issues[0])
        assert (steps, proposals) == ([], []), kind


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


# --------------------------------------------------------------------------- #
# A set of shapes that does not line up
#
# The defect the per-shape vocabulary cannot reach, and the reason it could
# not: every verb in it is about one shape and the thing that holds it, and a
# row of circles that does not line up is not any one circle being wrong. It
# used to come back as a sentence and reach a designer as work. What makes it
# arithmetic instead is the split these tests are really about -- the model
# names the set and the relation, the FILE says where the shapes belong, and
# nothing anywhere asks a picture for a coordinate.
# --------------------------------------------------------------------------- #

def _row(*boxes) -> DeckProfile:
    """One slide of shapes at the given (left, top, width, height), in inches."""
    shapes = [
        ShapeProfile(
            shape_id=100 + index, name=f"Oval {index}", shape_type="OVAL (9)",
            geometry=Geometry(left_in=left, top_in=top,
                              width_in=width, height_in=height),
        )
        for index, (left, top, width, height) in enumerate(boxes)
    ]
    return DeckProfile(path="deck.pptx", width_in=WIDE, height_in=TALL,
                       slides=[SlideProfile(number=1, shapes=shapes)])


def _arranged(profile: DeckProfile, kind: str, *indexes) -> DesignQaReport:
    """A report whose one slide finding is an arrangement over those shapes."""
    shapes = profile.slides[0].shapes
    members = tuple(
        Member(ref=f"s{i + 1}", shape=shapes[i].name,
               shape_id=shapes[i].shape_id, path=(i + 1,))
        for i in indexes
    )
    return DesignQaReport(
        deck="deck.pptx", generated_at="now", model="test", profile=profile,
        reviews=[SlideReview(slide=1, reviewed=True, slide_issues=[SlideIssue(
            note="the circles do not line up",
            task="put the row back in line",
            arrangement=kind, members=members,
        )])],
    )


def test_a_shape_out_of_line_is_moved_to_the_median_edge():
    """Median rather than mean, for the reason the cross-slide version gives:
    the shape being reported is the one that is out, and a mean lets it drag
    the line it is supposed to be joining towards itself."""
    report = _arranged(
        _row((1.0, 2.0, 1.5, 1.5), (3.0, 2.0, 1.5, 1.5), (5.0, 2.4, 1.5, 1.5),
             (7.0, 2.0, 1.5, 1.5), (9.0, 2.0, 1.5, 1.5)),
        "align_top", 0, 1, 2, 3, 4,
    )

    [task] = report.tasks
    assert task.fixable and task.op == "align" and task.issue == "align_top"

    # Only the circle that is out moves: the four already on the line are
    # inside the floor and produce no step at all.
    [step] = steps_for(report, ["slide:1:0"])
    assert (step.shape_id, step.top_in) == (102, 2.0)
    # And ONLY on the axis the arrangement is about. Levelling a row does not
    # name a horizontal position at all -- not even the one the shape already
    # has, which is the whole point: by the time this is applied, a step
    # before it may have changed that.
    assert step.left_in is None
    assert step.task_id == "slide:1:0"


def test_aligning_on_an_edge_accounts_for_the_shapes_own_size():
    """A right edge is a left edge plus a width, and the shapes in a set are
    not all the same width."""
    # Right edges of 4.0, 4.0 and 4.0 already agree, so nothing moves.
    report = _arranged(
        _row((1.0, 2.0, 3.0, 1.0), (2.0, 3.0, 2.0, 1.0), (0.5, 4.0, 3.5, 1.0)),
        "align_right", 0, 1, 2,
    )
    assert not steps_for(report, ["slide:1:0"])

    # Now they are 4.0, 4.0 and 2.5. The third moves its right edge to 4.0,
    # which for a 2.0in shape means a left of 2.0.
    report = _arranged(
        _row((1.0, 2.0, 3.0, 1.0), (2.0, 3.0, 2.0, 1.0), (0.5, 4.0, 2.0, 1.0)),
        "align_right", 0, 1, 2,
    )
    [step] = steps_for(report, ["slide:1:0"])
    assert (step.shape_id, step.left_in) == (102, 2.0)


def test_distributing_leaves_the_ends_alone_and_evens_the_gaps():
    """The ends anchor it because they are what the set's extent means. How
    wide a row should be is a composition somebody chose; how the space inside
    it is divided is not."""
    report = _arranged(
        _row((1.0, 2.0, 1.0, 1.0), (1.5, 2.0, 1.0, 1.0), (6.0, 2.0, 1.0, 1.0)),
        "distribute_h", 0, 1, 2,
    )
    steps = {s.shape_id: s.left_in for s in steps_for(report, ["slide:1:0"])}

    # The span runs 1.0 to 7.0 and holds 3.0in of shape, so each of the two
    # gaps is 1.5in. The ends are already right and produce no step; the
    # middle goes to 1.0 + 1.0 + 1.5.
    assert steps == {101: 3.5}


def test_gaps_are_measured_between_boxes_not_between_centres():
    """The two are the same only when every shape is the same size, and when
    they are not, evenly spaced centres is the arrangement that looks wrong."""
    report = _arranged(
        _row((0.0, 2.0, 1.0, 1.0), (4.0, 2.0, 4.0, 1.0), (9.0, 2.0, 1.0, 1.0)),
        "distribute_h", 0, 1, 2,
    )
    # Span 0.0 to 10.0, holding 6.0in of shape, so each gap is 2.0in and the
    # wide middle shape starts at 3.0. Evenly spaced CENTRES would have put it
    # at 3.5, with more white on one side of it than on the other.
    [step] = steps_for(report, ["slide:1:0"])
    assert (step.shape_id, step.left_in) == (101, 3.0)


def test_shapes_that_do_not_fit_the_span_are_not_distributed():
    """Equal negative gaps is arithmetic that still produces an answer, and
    the answer is a tidy pile. The honest reading is that the model named the
    wrong set, so nothing moves and the finding stays a task."""
    report = _arranged(
        _row((0.0, 2.0, 3.0, 1.0), (1.0, 2.0, 3.0, 1.0), (2.0, 2.0, 3.0, 1.0)),
        "distribute_h", 0, 1, 2,
    )
    assert not steps_for(report, ["slide:1:0"])
    [task] = report.tasks
    assert not task.fixable


def test_a_set_already_in_line_is_not_offered_as_work():
    """A tick box that would do nothing is worse than no tick box."""
    report = _arranged(
        _row((1.0, 2.0, 1.5, 1.5), (3.0, 2.0, 1.5, 1.5), (5.0, 2.0, 1.5, 1.5)),
        "align_top", 0, 1, 2,
    )
    [task] = report.tasks
    assert not task.fixable and task.op == ""
    assert not steps_for(report, ["slide:1:0"])


def test_a_row_that_already_shares_the_other_edge_is_not_moved():
    """A set cannot share both edges of an axis unless its shapes are the same
    size along it, so granting the second one takes the first one away.

    The geometry is a real slide: four column headings, one box per heading,
    all four drawn at the same top and the same width, and the first of them
    0.28in taller because its heading wraps to a second line where the other
    three sit on one. The model saw three headings sharing a baseline and the
    fourth not, and asked for `align_bottom`. Applied, that lifted the
    two-line box clear of the top edge its row is actually built on.

    The rule layer's answer for this slide is `typography.heading_balance`,
    which breaks the short headings across two lines and moves no box at all.
    """
    row = _row(
        (0.708, 2.779, 2.75, 0.662),      # "Decision momentum stalls", 2 lines
        (3.764, 2.779, 2.75, 0.381),
        (6.819, 2.779, 2.75, 0.381),
        (9.875, 2.779, 2.75, 0.381),
    )
    report = _arranged(row, "align_bottom", 0, 1, 2, 3)
    assert not steps_for(report, ["slide:1:0"])
    [task] = report.tasks
    assert not task.fixable

    # And the same row asked the other way round, which is the same slide
    # reported by a model that named the edge the boxes already share.
    assert not steps_for(_arranged(row, "align_top", 0, 1, 2, 3), ["slide:1:0"])


def test_a_row_out_of_line_on_both_edges_is_still_levelled():
    """The refusal above is about a set that already agrees somewhere, not
    about a set whose shapes are different sizes. Four cards of three heights
    with nothing shared between them is the ragged row this exists to fix."""
    report = _arranged(
        _row((1.0, 2.0, 1.5, 1.0), (3.0, 2.0, 1.5, 1.2),
             (5.0, 2.3, 1.5, 0.9), (7.0, 2.0, 1.5, 1.0)),
        "align_top", 0, 1, 2, 3,
    )
    [step] = steps_for(report, ["slide:1:0"])
    assert (step.shape_id, step.top_in) == (102, 2.0)


def test_a_short_row_is_centred_across_the_slide_as_one_piece():
    """The one arrangement measured off the slide rather than off the set, and
    the one where every member moves by the same amount: the gaps a designer
    drew between the cards are not this correction's business."""
    # Three 3.0in cards with 0.4in gaps, sitting left on a grid drawn for four.
    # The set runs 0.7 to 10.5, so 9.8in of row in a 13.333in slide starts at
    # 1.766 and every card shifts by the same 1.066.
    report = _arranged(
        _row((0.7, 2.0, 3.0, 2.0), (4.1, 2.0, 3.0, 2.0), (7.5, 2.0, 3.0, 2.0)),
        "center_h", 0, 1, 2,
    )
    [task] = report.tasks
    assert task.fixable and task.op == "align" and task.issue == "center_h"

    steps = sorted(steps_for(report, ["slide:1:0"]), key=lambda s: s.left_in)
    assert [s.left_in for s in steps] == [1.766, 5.166, 8.566]
    # The gaps are exactly the gaps that were drawn, and nothing is said about
    # how far down the slide the row sits.
    assert all(step.top_in is None for step in steps)


def test_a_row_already_centred_is_not_offered_as_work():
    report = _arranged(
        _row((1.667, 2.0, 4.0, 2.0), (6.667, 2.0, 5.0, 2.0)),
        "center_h", 0, 1,
    )
    assert not steps_for(report, ["slide:1:0"])


def test_centring_stops_where_the_applier_would_refuse_the_move():
    """A set that has to travel further than an alignment allows is not sitting
    slightly off centre. Refused here rather than there, so the designer reads
    the model's instruction instead of a sentence about wrong neighbours."""
    report = _arranged(
        _row((0.2, 2.0, 1.5, 2.0), (2.0, 2.0, 1.5, 2.0)),
        "center_h", 0, 1,
    )
    assert not steps_for(report, ["slide:1:0"])
    [task] = report.tasks
    assert not task.fixable


def test_an_arrangement_is_not_offered_without_the_deck_to_measure():
    """No profile, no geometry, no target. The finding is still reported."""
    report = _arranged(_row((1.0, 2.0, 1.5, 1.5), (3.0, 2.4, 1.5, 1.5)),
                       "align_top", 0, 1)
    report.profile = None
    [task] = report.tasks
    assert not task.fixable and task.what == "put the row back in line"


def test_an_arrangement_names_shapes_from_the_list_or_stops_being_one():
    """The same rule a verdict lives under: an invented ref is the model
    having invented a shape. Dropping it can leave too few to measure, and
    the finding then degrades to the note it used to be -- not to nothing."""
    review = review_from_response(
        {
            "shapes": [],
            "slide_issues": [{
                "note": "the two headings sit at different heights",
                "task": "level them",
                "arrangement": "align_top",
                "shapes": ["s1", "s2"],
            }, {
                "note": "the badges are unevenly spaced",
                "task": "even them out",
                "arrangement": "distribute_h",
                "shapes": ["s1", "s2", "s404"],
            }],
        },
        1, _refs(), (WIDE, TALL),
    )

    levelled, spaced = review.slide_issues
    assert levelled.arrangement == "align_top"
    assert [m.shape_id for m in levelled.members] == [11, 12]
    assert levelled.addressable

    # s404 was never sent, and two shapes are already evenly spaced whatever
    # the gap, so there is nothing left to distribute.
    assert spaced.arrangement == "" and spaced.members == ()
    assert spaced.note == "the badges are unevenly spaced"


def test_the_same_shape_named_twice_is_counted_once():
    """Counting it twice would drag a median towards it."""
    review = review_from_response(
        {"shapes": [], "slide_issues": [{
            "note": "n", "task": "t", "arrangement": "align_left",
            "shapes": ["s1", "s1", "s2"],
        }]},
        1, _refs(), (WIDE, TALL),
    )
    [issue] = review.slide_issues
    assert [m.ref for m in issue.members] == ["s1", "s2"]


def test_a_relation_nobody_defined_reads_as_no_relation():
    review = review_from_response(
        {"shapes": [], "slide_issues": [{
            "note": "n", "task": "t", "arrangement": "align_diagonally",
            "shapes": ["s1", "s2"],
        }]},
        1, _refs(), (WIDE, TALL),
    )
    [issue] = review.slide_issues
    assert issue.arrangement == "" and not issue.addressable


def test_the_model_is_never_asked_where_a_shape_belongs():
    """The whole split rests on this: it names the set and the relation, and
    the file supplies every number. A coordinate field on a slide finding
    would be the guess the rest of the prompt exists to prevent."""
    from formatting_tool.ai.designqa import _schema

    fields = _schema(["s1"])["properties"]["slide_issues"]["items"]["properties"]
    assert set(fields) == {"note", "task", "arrangement", "shapes"}


def test_a_slide_finding_that_is_not_a_relation_is_still_for_a_designer():
    """The bucket did not go away. An empty column has no arithmetic and is
    no less real for it."""
    [task] = [t for t in _report().tasks if t.kind == "slide"]
    assert not task.fixable and task.op == ""


def test_an_arrangement_whose_moves_were_all_refused_stays_outstanding():
    """A step that was asked for and did not happen is exactly the finding a
    page can lose: off the tick list, not in the changes, nowhere."""
    report = _arranged(
        _row((1.0, 2.0, 1.5, 1.5), (3.0, 2.0, 1.5, 1.5), (5.0, 2.4, 1.5, 1.5)),
        "align_top", 0, 1, 2,
    )
    left = outstanding(report, ["slide:1:0"], [])
    assert [t.id for t in left] == ["slide:1:0"]


def test_a_row_of_circles_goes_from_answer_to_move_without_a_hand_in_between():
    """The two halves, joined, on the shape map they actually share.

    Everything above tests one side of the seam: the parse builds members from
    refs, the arithmetic builds steps from members. The seam itself is the ref
    -> path -> shape lookup, and it is the part that fails silently -- a path
    off by one finds a shape, just not the one the model meant, and every
    assertion about medians still passes while a circle nobody mentioned
    walks across the slide. So this one starts where the model does, with a
    map it was handed, and ends at the move.
    """
    slide = SlideProfile(number=1, shapes=[
        _shape("Title 1", 0.6, 0.4, 8.0, 0.9, role=TextRole.TITLE,
               shape_id=10, text="Goals for the month"),
        _shape("Oval 1", 1.0, 2.0, 1.5, 1.5, shape_id=21),
        _shape("Oval 2", 3.0, 2.0, 1.5, 1.5, shape_id=22),
        _shape("Oval 3", 5.0, 2.6, 1.5, 1.5, shape_id=23),
        _shape("Oval 4", 7.0, 2.0, 1.5, 1.5, shape_id=24),
    ])
    _listing, refs = build_shape_map(slide, WIDE, TALL)

    review = review_from_response(
        {"shapes": [], "slide_issues": [{
            "note": "the third circle sits lower than the rest of the row",
            "task": "level the row on its top edge",
            "arrangement": "align_top",
            "shapes": ["s2", "s3", "s4", "s5"],
        }]},
        1, refs, (WIDE, TALL),
    )
    report = DesignQaReport(
        deck="deck.pptx", generated_at="now", model="test", reviews=[review],
        profile=DeckProfile(path="deck.pptx", width_in=WIDE, height_in=TALL,
                            slides=[slide]),
    )

    [task] = report.tasks
    assert task.fixable and task.what == "level the row on its top edge"

    # One move, on the circle that is out, to the top the other three share.
    # The title was never named and is not touched by a row being levelled.
    [step] = steps_for(report, [task.id])
    assert (step.shape, step.shape_id) == ("Oval 3", 23)
    assert (step.left_in, step.top_in) == (None, 2.0)
    assert step.measured_off == "level with the others, on their top edge"


def _grid() -> DeckProfile:
    """Ten circles in two rows of five, with one in the top row sitting low.

    The slide that broke this, drawn the way the screenshots had it.
    """
    boxes = []
    for row in range(2):
        for col in range(5):
            top = 1.8 + row * 2.2 + (0.3 if (row, col) == (0, 2) else 0.0)
            boxes.append((0.8 + col * 2.5, top, 1.8, 1.8))
    return _row(*boxes)


def test_a_set_spanning_two_rows_is_split_and_each_row_kept_apart():
    """The failure that drove this, and the thing no per-move check can catch.
    Taken whole, the median top of ten circles in two rows falls BETWEEN the
    rows: every circle is a little over an inch from it, every move passes the
    two-inch sanity check on its own, and the slide comes back with both rows
    collapsed onto one line. Nothing is wrong with any single move. The set is
    wrong, so the set is what gets split."""
    report = _arranged(_grid(), "align_top", *range(10))

    # One move. The low circle rejoins the four it belongs with, and the row
    # below it is measured on its own and does not move at all.
    [step] = steps_for(report, ["slide:1:0"])
    assert (step.shape_id, step.top_in) == (102, 1.8)


def test_naming_the_row_and_naming_the_grid_do_the_same_thing():
    """Which is the point of splitting rather than refusing. A model shown
    this slide says "the circles do not line up", and whether it then names
    five shapes or ten is not something the answer should turn on."""
    grid = steps_for(_arranged(_grid(), "align_top", *range(10)), ["slide:1:0"])
    row = steps_for(_arranged(_grid(), "align_top", 0, 1, 2, 3, 4), ["slide:1:0"])

    assert [(s.shape_id, s.top_in) for s in grid] == [(102, 1.8)]
    assert [(s.shape_id, s.top_in) for s in row] == [(102, 1.8)]


def test_the_same_split_happens_on_the_other_axis():
    """Aligning left edges is about a column, so the set splits into columns.
    The left-hand one is already square and stays put; in the right-hand one
    the first box is the anchor, so it keeps its 7.0in and the 7.3in below it
    is the only shape that moves."""
    report = _arranged(
        _row((1.0, 1.0, 2.0, 1.0), (1.0, 3.0, 2.0, 1.0),
             (7.0, 1.0, 2.0, 1.0), (7.3, 3.0, 2.0, 1.0)),
        "align_left", 0, 1, 2, 3,
    )
    assert {s.shape_id: s.left_in
            for s in steps_for(report, ["slide:1:0"])} == {103: 7.0}


def test_two_shapes_out_of_line_join_the_first_of_them():
    """THE ANCHOR IS A BOX ON THE SLIDE, NOT A STATISTIC. The median this used
    to take put both shapes on a line neither of them was drawn at, which is
    the behaviour that walked a levelled row into the title above it: the line
    a set is brought onto has to be one a designer can point at. So the first
    box along the row keeps its place and the rest join it."""
    report = _arranged(
        _row((2.0, 1.6, 3.0, 0.5), (8.0, 1.9, 3.0, 0.5)), "align_top", 0, 1)

    assert {s.shape_id: s.top_in
            for s in steps_for(report, ["slide:1:0"])} == {101: 1.6}


def test_distributing_a_grid_spaces_each_row_on_its_own():
    """Taken whole, sorting a grid by left edge interleaves the rows and the
    gaps come out measured between shapes that are not beside each other.
    Split first, each row is spaced against its own neighbours."""
    # The grid is evenly spaced across already, so an honest answer is no
    # moves -- and it is the interleaving that would have invented some.
    assert not steps_for(_arranged(_grid(), "distribute_h", *range(10)),
                         ["slide:1:0"])

    # Bunch the top row up and only the top row is respaced.
    crowded = _grid()
    crowded.slides[0].shapes[1].geometry.left_in = 3.0
    steps = steps_for(_arranged(crowded, "distribute_h", *range(10)),
                      ["slide:1:0"])
    assert {s.shape_id for s in steps} <= {100, 101, 102, 103, 104}
    assert steps


def test_a_shape_far_enough_out_stops_being_part_of_the_row():
    """Where a row ends is set by the shapes' own sizes, which is the right
    scale: how far out of line a shape can drift before it is no longer in the
    row depends on how big the row is. A circle that has cleared its
    neighbours entirely is not a circle that needs nudging back."""
    # 1.8in circles, one dropped 2.0in: it shares no height with the other
    # two, so it is a line of its own -- and a line of one is left alone.
    report = _arranged(
        _row((1.0, 2.0, 1.8, 1.8), (3.0, 2.0, 1.8, 1.8), (5.0, 4.0, 1.8, 1.8)),
        "align_top", 0, 1, 2,
    )
    assert not steps_for(report, ["slide:1:0"])

    # Dropped 0.3in, it still overlaps them, and it is levelled.
    report = _arranged(
        _row((1.0, 2.0, 1.8, 1.8), (3.0, 2.0, 1.8, 1.8), (5.0, 2.3, 1.8, 1.8)),
        "align_top", 0, 1, 2,
    )
    [step] = steps_for(report, ["slide:1:0"])
    assert (step.shape_id, step.top_in) == (102, 2.0)


# --------------------------------------------------------------------------- #
# Two corrections on one slide
#
# A slide rarely has one thing wrong with it, and the design that survives one
# finding per slide does not survive three. These are about what happens when
# several arrangements land in the same round: none of them may quietly undo
# another, and where two of them genuinely disagree, one has to lose out loud.
# --------------------------------------------------------------------------- #

def _two_findings(profile: DeckProfile, *findings) -> DesignQaReport:
    """A report carrying several arrangements on the one slide."""
    shapes = profile.slides[0].shapes
    issues = []
    for kind, indexes in findings:
        issues.append(SlideIssue(
            note=f"the shapes are wrong: {kind}", task=f"fix {kind}",
            arrangement=kind,
            members=tuple(
                Member(ref=f"s{i + 1}", shape=shapes[i].name,
                       shape_id=shapes[i].shape_id, path=(i + 1,))
                for i in indexes
            ),
        ))
    return DesignQaReport(
        deck="deck.pptx", generated_at="now", model="test", profile=profile,
        reviews=[SlideReview(slide=1, reviewed=True, slide_issues=issues)],
    )


def test_a_correction_never_names_the_axis_it_is_not_about():
    """THE BUG THIS EXISTS FOR, and it is worth stating exactly because it
    reads as harmless. A correction used to carry the shape's CURRENT position
    for the axis it was not changing -- writing a value back where it already
    is should do nothing. It does nothing only while that value is still
    current. Two findings on one slide are two sets of steps, both measured
    off the deck as it was read, and the second carries the position the first
    has just corrected. Seen on a real deck, in one round:

        Oval 7  level with the others, on their top edge  (-0.00in, -0.16in)
        Oval 7  an equal gap from the shapes either side  (-0.12in, +0.16in)

    Both applied, both reported, net vertical movement zero. Neither step was
    wrong on its own, which is why nothing refused either of them."""
    report = _two_findings(
        _row((0.8, 1.8, 1.8, 1.8), (3.2, 1.8, 1.8, 1.8), (5.7, 1.96, 1.8, 1.8),
             (8.0, 1.8, 1.8, 1.8), (10.6, 1.8, 1.8, 1.8)),
        ("align_top", range(5)), ("distribute_h", range(5)),
    )
    steps = steps_for(report, [t.id for t in report.tasks])
    assert steps

    # Exactly one axis per step, always. A step that named both would be a
    # step asserting something about an axis nobody measured.
    for step in steps:
        assert (step.left_in is None) != (step.top_in is None)

    # And the circle that is in both findings is levelled by one and spaced by
    # the other, rather than levelled and then put back.
    low = [s for s in steps if s.shape_id == 102]
    assert [s.top_in for s in low if s.top_in is not None] == [1.8]


def test_each_row_of_a_grid_is_spaced_onto_the_same_positions():
    """Which is how the columns come out square without anything being told to
    square them: two rows with the same end shapes and the same widths divide
    the same span, so they land on the same numbers."""
    report = _two_findings(
        _row((0.8, 1.8, 1.8, 1.8), (3.2, 1.8, 1.8, 1.8), (5.7, 1.8, 1.8, 1.8),
             (8.0, 1.8, 1.8, 1.8), (10.6, 1.8, 1.8, 1.8),
             (0.8, 4.0, 1.8, 1.8), (3.4, 4.0, 1.8, 1.8), (5.8, 4.0, 1.8, 1.8),
             (8.2, 4.0, 1.8, 1.8), (10.6, 4.0, 1.8, 1.8)),
        ("distribute_h", range(10)),
    )
    placed = {s.shape_id: s.left_in for s in steps_for(report, ["slide:1:0"])}
    settled = dict(zip(range(100, 110),
                       [0.8, 3.25, 5.7, 8.15, 10.6] * 2))
    for shape_id, want in settled.items():
        assert placed.get(shape_id, want) == want


def test_a_row_is_spaced_exactly_rather_than_nearly():
    """The floor for a set of shapes is not the floor for a title that sits a
    little low on one slide. A circle left 0.05in short of the line is the
    defect that was reported, still on the slide, under a page that has just
    said the row was levelled."""
    report = _two_findings(
        _row((0.8, 1.8, 1.8, 1.8), (3.2, 1.8, 1.8, 1.8), (5.7, 1.8, 1.8, 1.8),
             (8.0, 1.8, 1.8, 1.8), (10.6, 1.8, 1.8, 1.8)),
        ("distribute_h", range(5)),
    )
    # 0.8 and 10.6 anchor an 11.6in span holding 9.0in of circle, so the gaps
    # are 0.65in and the middle three belong at 3.25, 5.7 and 8.15. The one
    # 0.05in out is corrected, not waved through.
    placed = {s.shape_id: s.left_in for s in steps_for(report, ["slide:1:0"])}
    assert placed[101] == 3.25


def test_two_corrections_cannot_both_decide_where_one_shape_sits():
    """A row spaced evenly and a column lined up both settle a horizontal
    position and they will not agree. Applied in whatever order they arrive,
    the second wins and the first is still reported as done -- a page claiming
    two corrections over a slide carrying one. Arithmetic can compute either
    answer and cannot choose between them, so the first keeps the axis and the
    second is refused where somebody can read it."""
    from formatting_tool.apply.qafix import QaFixResult, Step, _align

    class FakeShape:
        Left, Top = 72.0, 144.0

    shape, result, settled = FakeShape(), QaFixResult(), set()
    spaced = Step(op="align", slide=1, shape_id=5, shape="Oval 1", left_in=2.0)
    _align(None, shape, spaced, result, settled)
    assert shape.Left == 144.0 and result.applied

    squared = Step(op="align", slide=1, shape_id=5, shape="Oval 1", left_in=3.0)
    _align(None, shape, squared, result, settled)
    assert shape.Left == 144.0                      # the first answer stands
    assert "already settled" in result.skipped[0].reason
    assert "across" in result.skipped[0].reason


def test_two_slides_do_not_share_one_shapes_identity():
    """A SHAPE ID IS UNIQUE WITHIN A SLIDE, NOT ACROSS A DECK. python-pptx
    numbers from 2 on every slide, so slide 1 and slide 7 both hold a shape 2
    and they are different shapes.

    The guard that stops two corrections fighting over one axis remembered
    which axes were settled by shape id alone, for a round that spans the whole
    deck. So levelling a row on slide 1 refused to level the row on slide 7,
    and said so in words about a disagreement that did not exist."""
    from formatting_tool.apply.qafix import QaFixResult, Step, _align

    class FakeShape:
        def __init__(self):
            self.Left, self.Top = 72.0, 144.0

    result, settled = QaFixResult(), set()
    first, second = FakeShape(), FakeShape()

    _align(None, first, Step(op="align", slide=1, shape_id=2, shape="Oval 1",
                             path=(1,), top_in=1.5), result, settled)
    _align(None, second, Step(op="align", slide=7, shape_id=2, shape="Oval 1",
                              path=(1,), top_in=1.5), result, settled)

    assert not result.skipped, [s.reason for s in result.skipped]
    assert first.Top == second.Top == 108.0


def test_settling_one_axis_leaves_the_other_free():
    """Levelling a row and spacing it are not in conflict -- they are the two
    halves of squaring it up -- so having settled a shape's top must not lock
    anything out of settling where it sits across."""
    from formatting_tool.apply.qafix import QaFixResult, Step, _align

    class FakeShape:
        Left, Top = 72.0, 144.0

    shape, result, settled = FakeShape(), QaFixResult(), set()
    _align(None, shape, Step(op="align", slide=1, shape_id=5, shape="Oval 1",
                             top_in=1.5), result, settled)
    _align(None, shape, Step(op="align", slide=1, shape_id=5, shape="Oval 1",
                             left_in=2.0), result, settled)

    assert not result.skipped
    assert (shape.Left, shape.Top) == (144.0, 108.0)
    # Each correction describes only what it did. A levelling that reported
    # "+0.00in across" was reporting an axis it never touched.
    assert result.applied[0].detail.endswith("(-0.50in down)")
    assert result.applied[1].detail.endswith("(+1.00in across)")


# --------------------------------------------------------------------------- #
# The answer has to fit
#
# The failure these are about is the quietest one this check has. A slide whose
# answer runs past the token limit comes back as a truncated string, which is
# not valid JSON, which is no verdicts at all -- and the answer is long in
# proportion to how much is wrong with the slide, so the slide that is lost is
# reliably the slide most worth reading. On a real run: slides 7, 10 and 12 of
# a 29-slide deck, all "response was not valid JSON", all silently absent from
# the report.
# --------------------------------------------------------------------------- #

def test_the_budget_covers_what_the_schema_is_entitled_to_ask_for():
    """A verdict carrying an issue and a proposal is about 350 characters of
    JSON -- a ref, a status, an issue, an action, a note, a task, and a `fix`
    object with eight keys of its own. The old flat 4,096 could not hold a
    full slide's worth of those, which is not a tuning problem: it is a budget
    smaller than the answer the schema requires."""
    from formatting_tool.ai.designqa import _MAX_SHAPES, _answer_tokens

    # Roughly 350 chars a verdict at roughly 3.3 chars a token, and every
    # listed shape is required to have one.
    need = _MAX_SHAPES * 350 / 3.3
    assert _answer_tokens(_MAX_SHAPES) > need


def test_a_sparse_slide_still_gets_room_to_think():
    """The floor is not the answer's size, it is the share of the allowance
    that thinking would otherwise take from a short answer."""
    from formatting_tool.ai.designqa import _ANSWER_TOKENS_FLOOR, _answer_tokens

    assert _answer_tokens(0) == _ANSWER_TOKENS_FLOOR
    assert _answer_tokens(2) == _ANSWER_TOKENS_FLOOR


def test_the_budget_grows_with_the_shapes_and_then_stops():
    from formatting_tool.ai.designqa import _ANSWER_TOKENS_CAP, _answer_tokens

    assert _answer_tokens(60) > _answer_tokens(30) > _answer_tokens(10)
    assert _answer_tokens(10_000) == _ANSWER_TOKENS_CAP


def test_a_cut_off_answer_keeps_the_verdicts_that_arrived():
    """Decoded one entry at a time from the start, stopping at the first that
    will not decode. Nothing is repaired -- a half-written verdict is dropped
    rather than guessed at -- so what comes back is a prefix of what the model
    actually said and never an invention."""
    from formatting_tool.ai.gemini import salvage_list

    cut = (
        '{"shapes": ['
        '{"shape": "s1", "status": "ok", "issue": "none"}, '
        '{"shape": "s2", "status": "issue", "issue": "cut_off"}, '
        '{"shape": "s3", "status": "iss'
    )
    kept = salvage_list(cut, "shapes")
    assert [entry["shape"] for entry in kept] == ["s1", "s2"]
    # The array that never started is not an error, it is an empty answer.
    assert salvage_list(cut, "slide_issues") == []


def test_salvage_reads_a_whole_answer_the_same_way():
    """It has to be right on undamaged text too, since what makes an answer
    the last complete one is not visible from inside the scan."""
    from formatting_tool.ai.gemini import salvage_list

    whole = '{"shapes": [{"shape": "s1"}, {"shape": "s2"}], "slide_issues": []}'
    assert [e["shape"] for e in salvage_list(whole, "shapes")] == ["s1", "s2"]
    assert salvage_list(whole, "slide_issues") == []


def test_salvage_gives_nothing_back_rather_than_guessing():
    from formatting_tool.ai.gemini import salvage_list

    assert salvage_list("", "shapes") == []
    assert salvage_list("not json at all", "shapes") == []
    assert salvage_list('{"shapes": [', "shapes") == []
    # A key that is there but holds something other than an array. The next
    # "[" in the object belongs to a different key and must not be read as
    # this one's.
    assert salvage_list('{"shapes": null}', "shapes") == []
    mixed = '{"shapes": null, "slide_issues": [{"note": "x"}]}'
    assert salvage_list(mixed, "shapes") == []
    assert salvage_list(mixed, "slide_issues") == [{"note": "x"}]


def test_a_truncated_answer_carries_its_text_for_the_caller_to_use():
    """The distinction that makes salvaging possible at all: a refusal leaves
    nothing to read, and this leaves a complete answer with its tail cut off.
    Still an AIValidationError, so callers that cannot use half an answer are
    unaffected."""
    from formatting_tool.ai.gemini import AIValidationError, Truncated

    exc = Truncated("cut off", text='{"shapes": [{"shape": "s1"}')
    assert isinstance(exc, AIValidationError)
    assert exc.text.startswith('{"shapes"')


def test_a_row_being_levelled_does_not_tick_off_a_title_mismatch():
    """TWO DIFFERENT FINDINGS THAT APPLY THE SAME VERB. A cross-slide position
    mismatch moves a title to where the deck puts it; a within-slide
    arrangement moves a circle into line with its row. Both come out as
    `align`, and what was left to do was worked out by asking whether ANY
    align had landed on any of the mismatch's slides.

    So a row levelled on slide 7 answered "did the title on slide 7 move?"
    with yes. The mismatch dropped off the outstanding list, never went into
    the deck as a comment, and the title stayed exactly where it was. The case
    is not exotic: the title's own move is refused whenever it is further out
    than an alignment allows, which is precisely when the mismatch is worth
    reporting.
    """
    from formatting_tool.apply.qafix import Change

    report = _report()
    report.profile = _deck_with_titles({1: 0.4, 2: 0.42, 3: 0.41, 7: 4.2})
    report.deck_issues = [DeckIssue(
        kind="position", slides=[7], note="the title on 7 sits lower",
        task="move it up to match the rest",
    )]
    deck_task = next(t for t in report.tasks if t.kind == "deck")
    assert deck_task.fixable, "the deck mismatch has to be tickable to be lost"

    # The round: the title's own move was refused for being further than an
    # alignment, and a row on the same slide was levelled and reported.
    applied = [Change(op="align", slide=7, shape_id=999, shape="Oval 3",
                      detail="moved it to level with the others",
                      task_id="slide:7:0")]

    left = {t.id for t in outstanding(report, [deck_task.id], applied)}
    assert deck_task.id in left, (
        "the title mismatch was reported as done because a different finding "
        "applied the same verb on the same slide"
    )




# --------------------------------------------------------------------------- #
# The deterministic half
#
# This check was built as the model's half of the tool and never asked the rule
# layer anything, which was an absence rather than a decision. What it cost was
# every finding that already had arithmetic and a fixer behind it: on a deck
# built to demonstrate contrast failures, the page reported one finding on the
# contrast slide -- a clipped caption -- while the rules find five there,
# including all four pairings the slide was drawn to show.
# --------------------------------------------------------------------------- #

def _rule_issue(rule_id, slide=1, shape="Card 1", shape_id=11, **kwargs):
    issue = Issue(
        category=Category.COLOR,
        severity=Severity.WARNING,
        message=kwargs.pop("message", "something measurable is wrong"),
        source=Source.RULE,
        rule_id=rule_id,
        slide=slide,
        shape=shape,
        shape_id=shape_id,
        deck="deck.pptx",
        **kwargs,
    )
    issue.id = issue.fingerprint()
    return issue


def test_a_measured_finding_is_work_like_any_other():
    """One list, and which half of the tool noticed a defect is this tool's
    bookkeeping rather than a designer's problem."""
    report = _report()
    report.rule_issues = [_rule_issue(
        "color.text.contrast",
        message="Text in #FFFFFF on #DEDEDE reads at 1.3:1.",
        suggestion="Recolour the text to #1A1A1A.",
    )]

    task = next(t for t in report.tasks if t.id.startswith("rule:"))
    assert task.fixable and task.slide == 1 and task.shape == "Card 1"
    # The instruction leads and the measurement follows, the same way round as
    # every other row on the page.
    assert task.what == "Recolour the text to #1A1A1A."
    assert task.why.startswith("Text in #FFFFFF")


def test_a_measured_finding_is_drawn_on_the_render_like_a_verdict():
    """A verdict carries the rectangle the model was given. A rule finding
    carries a slide and a shape id, so without this the page has nothing to
    draw and nothing to anchor a comment to -- and half the value of the page
    is that a finding points at the thing it is about."""
    profile = _row((1.0, 2.0, 3.0, 1.5))
    shape = profile.slides[0].shapes[0]
    report = _report()
    report.profile = profile
    report.width_in, report.height_in = WIDE, TALL
    report.rule_issues = [_rule_issue(
        "color.text.contrast", shape=shape.name, shape_id=shape.shape_id
    )]

    task = next(t for t in report.tasks if t.id.startswith("rule:"))
    assert task.box == (round(1.0 / WIDE, 5), round(2.0 / TALL, 5),
                        round(3.0 / WIDE, 5), round(1.5 / TALL, 5))


def test_a_finding_both_halves_made_is_shown_once():
    """The rule wins, and not because it is cleverer: it carries a number
    nobody has to be trusted for and a fixer that can act on it, where the
    model's version of the same finding carries a sentence. Two rows about one
    defect, one of them tickable, is a page asking a designer to work out
    which is which."""
    report = DesignQaReport(
        deck="deck.pptx", generated_at="now", model="test",
        reviews=[SlideReview(slide=1, reviewed=True, verdicts=[
            _verdict("s1", "none", shape_id=11, status="issue"),
        ])],
    )
    report.reviews[0].verdicts[0] = ShapeVerdict(
        slide=1, ref="s1", shape="Card 1", shape_id=11, role="body",
        status="issue", issue="low_contrast", action="none",
        note="the grey text is hard to read on the grey panel",
    )
    assert len(report.tasks) == 1               # the model's, on its own

    report.rule_issues = [_rule_issue("color.text.contrast", shape_id=11)]
    ids = [t.id for t in report.tasks]
    assert ids == [f"rule:{report.rule_issues[0].id}"]


def test_the_two_halves_are_not_deduplicated_where_they_disagree():
    """They overlap in three places and are complementary everywhere else,
    which is the point of running both. A verdict about a shape the rules
    reported something ELSE on is still that verdict's own finding."""
    report = DesignQaReport(
        deck="deck.pptx", generated_at="now", model="test",
        reviews=[SlideReview(slide=1, reviewed=True, verdicts=[ShapeVerdict(
            slide=1, ref="s1", shape="Card 1", shape_id=11, role="body",
            status="issue", issue="crowded", action="none",
            note="it has no room to breathe",
        )])],
    )
    report.rule_issues = [_rule_issue("color.text.contrast", shape_id=11)]
    assert len(report.tasks) == 2


def test_a_measured_finding_reaches_the_applier_unprefixed():
    """The page addresses it as `rule:<id>` and `apply_fixes` selects on the id
    the issue itself carries."""
    report = _report()
    issue = _rule_issue("color.text.contrast")
    report.rule_issues = [issue]

    assert [i.id for i in issues_for(report, [f"rule:{issue.id}"])] == [issue.id]
    assert issues_for(report, ["1:s3"]) == []


def test_a_measured_finding_that_was_ticked_and_applied_is_not_outstanding():
    """Matched on the bare id rather than on shape and op: a rule issue has no
    `fix`, so the op the applier reports for it is empty, and the shape-and-op
    match every other row uses would call every one of them outstanding."""
    from formatting_tool.apply.qafix import Change

    report = _report()
    issue = _rule_issue("color.text.contrast")
    report.rule_issues = [issue]
    key = f"rule:{issue.id}"

    applied = [Change(op="", slide=1, shape_id=11, shape="Card 1",
                      detail="recoloured 1 run(s)", task_id=issue.id)]
    assert key not in {t.id for t in outstanding(report, [key], applied)}
    # And a round where it was ticked and refused leaves it on the list.
    assert key in {t.id for t in outstanding(report, [key], [])}


def test_everything_is_measured_and_only_the_master_free_half_is_offered():
    """The gap between the two lists is not waste.

    What is OFFERED has to be narrow: the spec here is derived from the deck
    itself, so the brand rules would be measuring the deck against its own
    theme. What is MEASURED has to be complete, because this list is also the
    baseline `apply_fixes` compares its own work against -- handed only the
    narrow set, it read every palette and margin finding on the deck as newly
    introduced and corrected them unasked.
    """
    report = _report()
    report.rule_issues = [
        _rule_issue("color.text.contrast", shape_id=11),
        _rule_issue("color.text.off_palette", shape_id=12),
        _rule_issue("space.safe_margin", shape_id=13),
        _rule_issue("space.text_collision", shape_id=14),
    ]

    offered = {i.rule_id for i in report.offered_rule_issues}
    assert offered == {"color.text.contrast", "space.text_collision"}
    assert {t.issue for t in report.tasks if t.id.startswith("rule:")} == offered
    # And nothing outside the offered half can be ticked into the applier.
    assert issues_for(report, None) and all(
        i.rule_id in offered for i in issues_for(report, None)
    )


def test_a_rectangle_overlap_is_offered_only_where_the_model_saw_a_collision():
    """The model is told on this same page that two overlapping rectangles are
    not a collision and type touching type is, so offering the rectangle
    arithmetic unconditionally would be the page contradicting itself. But
    refusing it outright threw the move away: a badge over the words of its
    button is a real collision, the model sees it, and `fix_overlap` knows
    exactly how far to nudge. The render supplies the judgement and the file
    supplies the number."""
    report = DesignQaReport(
        deck="deck.pptx", generated_at="now", model="test",
        reviews=[SlideReview(slide=1, reviewed=True, verdicts=[ShapeVerdict(
            slide=1, ref="s1", shape="Badge 3", shape_id=11, role="other",
            status="issue", issue="crowded", action="none",
            note="it has no room to breathe",
        )])],
    )
    report.rule_issues = [_rule_issue("space.overlap", shape_id=11)]
    # The model looked at this shape and did not call it a collision.
    assert report.offered_rule_issues == []

    report.reviews[0].verdicts[0] = ShapeVerdict(
        slide=1, ref="s1", shape="Badge 3", shape_id=11, role="other",
        status="issue", issue="overlap", action="none",
        note="the badge covers the words of the button it sits on",
    )
    assert [i.rule_id for i in report.offered_rule_issues] == ["space.overlap"]
    # And it is shown once, not twice: the rule carries the number and the fix.
    assert [t.issue for t in report.tasks] == ["space.overlap"]


def test_a_mismatch_that_would_rewrite_the_slide_is_left_as_a_task():
    """A cross-slide mismatch is about a repeated element. What `_by_role`
    produces is every text shape on every named slide corrected towards a
    majority counted over the whole deck, which is the same answer on a regular
    deck and a very different one otherwise.

    Measured on a deck built to demonstrate mismatched type: one `type_scale`
    finding produced 107 proposals, every one setting text to 12pt, and one
    `color` finding produced 96, every one to the same navy. Four headings at
    22, 26, 18 and 20pt all became 12pt and three button labels became the
    colour of the buttons they sat on.
    """
    # A regular deck with one slide out of step: the correction is small and
    # is exactly the finding, so it stands.
    report = _report()
    report.profile = _typed_deck([11.0, 11.0, 11.0], odd={5: 9.0})
    report.spec = derive_master_spec(report.profile, BrandGuidelines())

    _steps, proposals = _deck_fixes(
        report, 0, DeckIssue(kind="type_scale", slides=[5], note="n", task="t"))
    assert [(p.slide, p.fix.size_pt) for p in proposals] == [(5, 11.0), (5, 11.0)]

    # A deck whose type disagrees with itself everywhere has no majority worth
    # moving towards, and correcting it rewrites the deck rather than the
    # element the model was looking at.
    report.profile = _typed_deck([22.0, 26.0, 18.0, 12.0])
    report.spec = derive_master_spec(report.profile, BrandGuidelines())
    _steps, proposals = _deck_fixes(
        report, 0,
        DeckIssue(kind="type_scale", slides=[1, 2, 3, 4, 5], note="n", task="t"))
    assert proposals == []


def test_a_capped_mismatch_is_refused_whole_rather_than_in_part():
    """Taking the first few would correct an arbitrary subset of a set the
    model described as one thing, which is worse than correcting none: the deck
    comes back half rewritten and the finding reads as done."""
    report = _report()
    report.profile = _typed_deck([22.0, 26.0, 18.0, 12.0])
    report.spec = derive_master_spec(report.profile, BrandGuidelines())

    _steps, proposals = _deck_fixes(
        report, 0,
        DeckIssue(kind="color", slides=[1, 2, 3, 4, 5], note="n", task="t"))
    assert proposals == []


def _typed_deck(sizes: list, odd: dict | None = None) -> DeckProfile:
    """Five slides carrying the given run sizes, one shape each.

    `odd` overrides the sizes on the named slides, for the regular deck with
    one slide out of step.
    """
    inks = ["1A1A1A", "F96167", "2C5F2D", "1E2761"]

    def body(name, size, sid, ink):
        run = RunProfile(text="copy", size_pt=size, color_hex=ink)
        return ShapeProfile(
            shape_id=sid, name=name, shape_type="TEXT_BOX (17)",
            geometry=Geometry(left_in=0.5, top_in=2.0, width_in=6.0, height_in=1.0),
            role=TextRole.BODY, text="copy",
            paragraphs=[ParagraphProfile(text="copy", runs=[run])],
        )

    slides = []
    for number in range(1, 6):
        shapes = []
        for index, size in enumerate(sizes):
            if odd and number in odd and index < len(sizes) - 1:
                size = odd[number]
            shapes.append(body(f"Body {number}{index}", size,
                               number * 10 + index, inks[index % len(inks)]))
        slides.append(SlideProfile(number=number, layout_name="content",
                                   shapes=shapes))
    return DeckProfile(path="deck.pptx", width_in=WIDE, height_in=TALL,
                       slides=slides)


# --------------------------------------------------------------------------- #
# The arrangements that change a size rather than a position
# --------------------------------------------------------------------------- #

def test_a_row_of_copies_that_drifted_takes_the_width_most_of_them_are():
    """Six cards drawn at three widths are a grid nobody laid out, and no
    alignment describes it because the cards line up perfectly on the edges
    they share. The median says which width was meant where a mean would let
    the three that drifted drag all six."""
    report = _arranged(
        _row((0.6, 1.9, 3.6, 1.5), (4.75, 1.85, 3.6, 1.5), (8.6, 2.05, 3.4, 1.5),
             (0.6, 4.15, 3.6, 1.5), (4.55, 4.35, 3.75, 1.5), (8.75, 4.0, 3.4, 1.5)),
        "same_width", 0, 1, 2, 3, 4, 5,
    )
    steps = steps_for(report, ["slide:1:0"])

    assert {s.op for s in steps} == {"resize"}
    assert {s.width_in for s in steps} == {3.6}
    # Only the three that are out move, and only their width: a row of cards
    # holding different amounts of copy may be different heights.
    assert len(steps) == 3
    assert all(s.height_in is None and s.left_in is None for s in steps)


def test_a_set_taken_whole_rather_than_split_into_rows():
    """A size is not a position. Six cards in two rows of three should be one
    width; taking each row's own median would leave the rows disagreeing."""
    report = _arranged(
        _row((0.6, 1.0, 3.0, 1.0), (4.0, 1.0, 3.0, 1.0), (8.0, 1.0, 3.0, 1.0),
             (0.6, 4.0, 2.6, 1.0), (4.0, 4.0, 3.0, 1.0), (8.0, 4.0, 3.0, 1.0)),
        "same_width", 0, 1, 2, 3, 4, 5,
    )
    [step] = steps_for(report, ["slide:1:0"])
    assert step.width_in == 3.0


def test_a_shape_too_far_out_to_be_a_copy_stops_the_whole_set():
    """A copy of a component that drifted is within a few per cent of its
    siblings. Something a third out is a different element, and a row corrected
    with one member left out is a row that still does not match, reported as
    done."""
    report = _arranged(
        _row((0.6, 1.0, 3.6, 1.0), (4.0, 1.0, 3.6, 1.0), (8.0, 1.0, 1.2, 1.0)),
        "same_width", 0, 1, 2,
    )
    assert steps_for(report, ["slide:1:0"]) == []


def test_headings_at_four_sizes_are_left_for_a_designer():
    """A size is a chosen value rather than a measurement. Four headings at 22,
    26, 18 and 20pt have no majority, and their median is 21 -- a size nobody
    typed and no other heading on the deck uses."""
    report = _typed_row([22.0, 26.0, 18.0, 20.0])
    assert steps_for(report, ["slide:1:0"]) == []


def test_headings_with_a_majority_take_the_size_most_of_them_are():
    report = _typed_row([22.0, 22.0, 18.0, 22.0])
    [step] = steps_for(report, ["slide:1:0"])

    assert (step.op, step.size_pt) == ("set_size", 22.0)
    assert step.shape == "Heading 2"


def _typed_row(sizes: list) -> DesignQaReport:
    """One slide of headings at the given sizes, named as one set."""
    shapes = [
        ShapeProfile(
            shape_id=100 + index, name=f"Heading {index}",
            shape_type="TEXT_BOX (17)",
            geometry=Geometry(left_in=1.0 + index * 3.0, top_in=1.0,
                              width_in=2.6, height_in=0.8),
            text="Aa", paragraphs=[ParagraphProfile(
                text="Aa", runs=[RunProfile(text="Aa", size_pt=size)])],
        )
        for index, size in enumerate(sizes)
    ]
    profile = DeckProfile(path="deck.pptx", width_in=WIDE, height_in=TALL,
                          slides=[SlideProfile(number=1, shapes=shapes)])
    return _arranged(profile, "same_type_size", *range(len(sizes)))


# --------------------------------------------------------------------------- #
# The brand rules, when there is a brand to measure against
#
# The objection to them on this page was never the rules, it was the reference.
# Measured against a spec derived from the deck itself they are noise; handed
# the master the deck was restyled onto they are the question somebody looking
# at that deck is actually asking.
# --------------------------------------------------------------------------- #

def test_without_a_master_the_brand_rules_are_not_offered():
    """Every colour is on the palette by construction, or off it by an accident
    of what the theme happens to hold."""
    report = _report()
    report.rule_issues = [
        _rule_issue("color.text.off_palette", shape_id=11),
        _rule_issue("color.shape.off_palette", shape_id=12),
        _rule_issue("color.text.contrast", shape_id=13),
    ]
    assert report.has_master is False
    assert [i.rule_id for i in report.offered_rule_issues] == [
        "color.text.contrast"
    ]


def test_with_the_master_the_deck_was_restyled_onto_they_are():
    """The deck check hands this page the deck it has just restyled and knows
    the master it used. Passing that through is the difference between "these
    colours are consistent with themselves" and "these colours are the
    brand's"."""
    report = _report()
    report.has_master = True
    report.rule_issues = [
        _rule_issue("color.text.off_palette", shape_id=11),
        _rule_issue("color.shape.off_palette", shape_id=12),
        # Still not everything: a rule needing the master's grid is a different
        # question from whether the colours are the brand's.
        _rule_issue("space.alignment_grid", shape_id=13),
    ]
    offered = {i.rule_id for i in report.offered_rule_issues}
    assert offered == {"color.text.off_palette", "color.shape.off_palette"}
    assert all(t.fixable for t in report.tasks if t.id.startswith("rule:"))


def test_the_master_is_what_makes_the_report_say_it_has_one():
    """Carried rather than inferred from the spec: a spec derived from a deck
    and a spec read off a master are the same type and tell the same story
    about themselves."""
    from formatting_tool.designqa import review_deck

    # No images, so nothing is asked of the model and the spec is all that is
    # being checked here.
    profile = _typed_deck([11.0])
    spec = derive_master_spec(profile, BrandGuidelines())

    assert review_deck(Path("deck.pptx"), [], profile=profile).has_master is False
    assert review_deck(
        Path("deck.pptx"), [], profile=profile, spec=spec
    ).has_master is True


# --------------------------------------------------------------------------- #
# A chart is not a diagram
# --------------------------------------------------------------------------- #
#
# A bar chart is a run of like-sized boxes on a regular grid with one of them a
# different height, which is the literal definition every arrangement here
# matches on -- and every correction they would make to it changes what the
# picture says the numbers are. No reading of the file can tell that run from a
# row of cards. The model looking at the render can, and `charts` is where it
# says so.


def _listed(refs):
    """The ref map `review_from_response` reads, from (id, box) pairs."""
    from formatting_tool.ai.designqa import Listed
    from formatting_tool.models import Geometry, ShapeProfile

    out = {}
    for index, (shape_id, box) in enumerate(refs.items(), start=1):
        left, top, width, height = box
        out[shape_id] = Listed(
            shape=ShapeProfile(
                shape_id=index,
                name=shape_id,
                shape_type="AUTO_SHAPE",
                geometry=Geometry(
                    left_in=left, top_in=top, width_in=width, height_in=height
                ),
            ),
            path=(index,),
        )
    return out


def _chart_slide():
    """A chart across the left half, and a caption sitting beside it."""
    return _listed({
        "s1": (1.0, 2.0, 6.0, 4.0),     # the plot
        "s2": (1.2, 5.4, 0.8, 0.3),     # a value label inside it
        "s3": (2.4, 5.4, 0.8, 0.3),     # another
        "s4": (8.0, 2.0, 4.0, 0.4),     # a caption, outside it
    })


def test_a_shape_inside_a_chart_gets_no_action() -> None:
    """A bar is a certain height because a number is a certain size, so
    stepping its type or centring it makes the picture state something other
    than the data."""
    from formatting_tool.ai.designqa import review_from_response

    review = review_from_response(
        {
            "charts": ["s1"],
            "shapes": [
                {"shape": "s2", "status": "issue", "issue": "too_small",
                 "action": "grow", "note": "the value labels are tiny",
                 "task": "set the value labels larger", "fix": None},
            ],
            "slide_issues": [],
        },
        1, _chart_slide(), (13.333, 7.5),
    )

    [verdict] = review.verdicts
    assert verdict.action == "none"
    # The finding is kept: "this chart's labels are too small" is real, and
    # a designer changes it in the chart rather than by dragging a shape.
    assert verdict.status == "issue" and verdict.task


def test_a_shape_outside_the_chart_is_corrected_as_usual() -> None:
    from formatting_tool.ai.designqa import review_from_response

    review = review_from_response(
        {
            "charts": ["s1"],
            "shapes": [
                {"shape": "s4", "status": "issue", "issue": "too_small",
                 "action": "grow", "note": "the caption is tiny",
                 "task": "set the caption larger", "fix": None},
            ],
            "slide_issues": [],
        },
        1, _chart_slide(), (13.333, 7.5),
    )

    assert review.verdicts[0].action == "grow"


def test_an_arrangement_naming_a_charts_parts_is_dropped_whole() -> None:
    """Not trimmed. A set with a chart's parts taken out of it is not a
    smaller version of the finding, it is a different finding nobody made --
    so the arrangement goes and the sentence stays."""
    from formatting_tool.ai.designqa import review_from_response

    review = review_from_response(
        {
            "charts": ["s1"],
            "shapes": [],
            "slide_issues": [{
                "note": "the value labels do not line up",
                "task": "level the value labels",
                "arrangement": "align_top",
                "shapes": ["s2", "s3"],
            }],
        },
        1, _chart_slide(), (13.333, 7.5),
    )

    [issue] = review.slide_issues
    assert issue.arrangement == "" and issue.members == ()
    assert issue.task == "level the value labels"


def test_a_slide_with_no_chart_on_it_is_unchanged() -> None:
    """Most slides. An empty `charts` has to cost nothing."""
    from formatting_tool.ai.designqa import review_from_response

    review = review_from_response(
        {
            "charts": [],
            "shapes": [],
            "slide_issues": [{
                "note": "the cards do not line up",
                "task": "level the cards",
                "arrangement": "align_top",
                "shapes": ["s2", "s3"],
            }],
        },
        1, _chart_slide(), (13.333, 7.5),
    )

    [issue] = review.slide_issues
    assert issue.arrangement == "align_top" and len(issue.members) == 2


def test_the_chart_regions_survive_a_truncated_answer() -> None:
    """`charts` is what holds the corrections off a data graphic, so an answer
    cut off after it would otherwise keep every verdict and lose the one thing
    stopping them being applied to a bar chart."""
    from formatting_tool.ai.gemini import salvage_list

    text = '{"charts": ["s1", "s2"], "shapes": [{"shape": "s4"'
    assert salvage_list(text, "charts") == ["s1", "s2"]


# --------------------------------------------------------------------------- #
# An alignment that would bury a neighbour
# --------------------------------------------------------------------------- #

class _Neighbour:
    """A shape as COM reports one, in points."""

    def __init__(self, shape_id, left, top, width, height, name="Shape"):
        self.Id = shape_id
        self.Left, self.Top = float(left), float(top)
        self.Width, self.Height = float(width), float(height)
        self.Name = name


class _Slide:
    def __init__(self, *shapes):
        self.Shapes = _Collection(shapes)


class _Collection:
    def __init__(self, items):
        self._items = list(items)
        self.Count = len(self._items)

    def __call__(self, index):
        return self._items[index - 1]


def test_an_alignment_that_lands_on_the_title_is_refused() -> None:
    """The defect that drove the anchor change and the guard both. A set is
    levelled against its own members and nothing in that arithmetic knows what
    else is on the slide, so a row of column headings brought onto the line of
    the first of them is free to arrive on top of the title above it."""
    from formatting_tool.apply.qafix import QaFixResult, Step, _align

    title = _Neighbour(1, 60, 60, 600, 60, "Title 1")
    heading = _Neighbour(2, 60, 170, 200, 30, "Rectangle 7")
    slide = _Slide(title, heading)
    result, settled = QaFixResult(), set()

    # 1.0in down is 72pt, squarely inside the title's band.
    _align(slide, heading, Step(op="align", slide=1, shape_id=2,
                                shape="Rectangle 7", path=(2,), top_in=1.0),
           result, settled)

    assert heading.Top == 170.0, "the heading moved into the title"
    assert "Title 1" in result.skipped[0].reason


def test_an_overlap_it_arrived_with_is_not_this_moves_doing() -> None:
    """A label on a band is over the band by design, and refusing that would
    refuse every alignment inside a component."""
    from formatting_tool.apply.qafix import QaFixResult, Step, _align

    band = _Neighbour(1, 60, 160, 600, 60, "Band")
    label = _Neighbour(2, 70, 170, 200, 30, "Label")
    slide = _Slide(band, label)
    result, settled = QaFixResult(), set()

    _align(slide, label, Step(op="align", slide=1, shape_id=2, shape="Label",
                              path=(2,), top_in=2.5), result, settled)

    assert label.Top == 180.0
    assert result.applied and not result.skipped


def test_an_alignment_clear_of_everything_still_happens() -> None:
    from formatting_tool.apply.qafix import QaFixResult, Step, _align

    other = _Neighbour(1, 600, 60, 100, 60, "Elsewhere")
    moving = _Neighbour(2, 60, 170, 200, 30, "Rectangle 7")
    slide = _Slide(other, moving)
    result, settled = QaFixResult(), set()

    _align(slide, moving, Step(op="align", slide=1, shape_id=2,
                               shape="Rectangle 7", path=(2,), top_in=2.5),
           result, settled)

    assert moving.Top == 180.0
    assert result.applied and not result.skipped
