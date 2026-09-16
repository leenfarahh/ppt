"""Slides the master has no layout for, handed back instead of wrecked.

THE SLIDE THIS IS FOR. A deck's framework page was four labelled layers with
connector lines between them. The master offers nothing of that shape -- its
nearest layout is a title, a subtitle, a circle drawn as artwork and eight
identical body regions stacked down one side -- so PowerPoint's placeholder
matching poured four layers of copy into eight slots that know nothing about
them, and every bullet landed on top of the diagram it belonged to.

The layout choice was the best available and the result was unreadable. So the
slide is left exactly as it arrived, on its own design, and a comment asks a
designer to place it.

Two things have to hold and neither is obvious from the walk:

1. Doubt is the GATE, not the verdict. A slide the matcher was hesitant about
   and the restyle handled is still restyled -- otherwise a thin master hands
   back half the deck.
2. The ORIGINAL survives, not the copy. The walk normally deletes the original
   and keeps the restyled duplicate; a quarantine is that decision inverted,
   and inverting the wrong one silently ships the wreck.
"""

from __future__ import annotations

from formatting_tool.rebuild import quarantine
from formatting_tool.rebuild.master_apply import _restyle

PT = 72.0
CANVAS = (13.333 * PT, 7.5 * PT)


# --------------------------------------------------------------------------- #
# A PowerPoint stand-in
# --------------------------------------------------------------------------- #

class Shape:
    def __init__(self, left, top, width, height, text="", drawn=None):
        self.Left, self.Top = left * PT, top * PT
        self.Width, self.Height = width * PT, height * PT
        self._text = text
        # Where the renderer actually drew the text, if not inside the box.
        self._drawn = drawn or (left, top, width, height)
        self.Id = id(self)
        self.Type = 1

    @property
    def HasTextFrame(self):
        return 1 if self._text else 0

    @property
    def TextFrame(self):
        return self

    @property
    def TextRange(self):
        return self

    @property
    def Text(self):
        return self._text

    @property
    def BoundLeft(self):
        return self._drawn[0] * PT

    @property
    def BoundTop(self):
        return self._drawn[1] * PT

    @property
    def BoundWidth(self):
        return self._drawn[2] * PT

    @property
    def BoundHeight(self):
        return self._drawn[3] * PT


class _Collection:
    """A COM collection: callable and 1-based, with a Count."""

    def __init__(self, items):
        self._items = items

    def __call__(self, index):
        return self._items[index - 1]

    @property
    def Count(self):
        return len(self._items)


class Slide:
    def __init__(self, tag, shapes, deck=None):
        self.tag = tag
        self._shapes = list(shapes)
        self.deck = deck
        self.CustomLayout = None
        self.comments: list[str] = []

    @property
    def Shapes(self):
        return _Collection(self._shapes)

    @property
    def Comments(self):
        slide = self

        class _Comments:
            @staticmethod
            def Add(left, top, author, initials, text):
                slide.comments.append(text)

        return _Comments()

    def Duplicate(self):
        copy = Slide(f"{self.tag}-copy", self._shapes, self.deck)
        # A duplicate lands directly after its original, which is the whole
        # basis of the walk's index arithmetic.
        self.deck.insert(self.deck.index(self) + 1, copy)
        return _Collection([copy])

    def Delete(self):
        self.deck.remove(self)


class Presentation:
    def __init__(self, slides):
        self._slides = []
        for slide in slides:
            slide.deck = self._slides
            self._slides.append(slide)

    @property
    def Slides(self):
        return _Collection(self._slides)

    @property
    def PageSetup(self):
        return type("P", (), {"SlideWidth": CANVAS[0], "SlideHeight": CANVAS[1]})()


class Layout:
    """A target layout with no picture placeholder, so `_restore` does nothing."""

    @property
    def Shapes(self):
        return _Collection([])


def _tidy(tag="4"):
    """A slide as the designer drew it: four layers, nothing overlapping."""
    return Slide(tag, [
        Shape(0.6, 0.4, 11.0, 0.8, "An integrated framework architecture"),
        Shape(1.5, 1.6, 3.0, 0.8, "Capability & Organisation Layer"),
        Shape(1.5, 2.8, 3.0, 0.8, "Process & Lifecycle Layer"),
        Shape(1.5, 4.0, 3.0, 0.8, "Governance, Risk, and Policy Layer"),
        Shape(1.5, 5.2, 3.0, 0.8, "Technology & System Layer"),
    ])


def _wrecked(tag="4"):
    """The same slide after a layout that does not fit it: every block of copy
    poured into the same region, written over the one before."""
    return Slide(tag, [
        Shape(0.6, 0.4, 11.0, 0.8, "An integrated framework architecture"),
        Shape(6.0, 1.5, 6.0, 3.0, "End-to-end lifecycle definition"),
        Shape(6.0, 1.6, 6.0, 3.0, "Readiness governance model"),
        Shape(6.0, 1.7, 6.0, 3.0, "Integrated system across DCIM, BMS"),
        Shape(6.0, 1.8, 6.0, 3.0, "Operating capability model"),
    ])


# --------------------------------------------------------------------------- #
# The measurement
# --------------------------------------------------------------------------- #

def test_a_tidy_slide_measures_as_undamaged() -> None:
    assert quarantine.measure(_tidy(), CANVAS).total == 0.0


def test_copy_written_over_copy_is_damage() -> None:
    damage = quarantine.measure(_wrecked(), CANVAS)
    assert damage.overlap > quarantine.WRECKED_SQIN
    assert damage.total == damage.overlap


def test_two_shapes_stacked_are_only_damage_when_both_carry_copy() -> None:
    """A card on a panel and a label on a band are how slides are drawn. Two
    blocks of text in one place are not."""
    panel = Slide("p", [Shape(1.0, 1.0, 4.0, 2.0), Shape(1.0, 1.0, 4.0, 2.0)])
    assert quarantine.measure(panel, CANVAS).overlap == 0.0

    both = Slide("b", [Shape(1.0, 1.0, 4.0, 2.0, "one"),
                       Shape(1.0, 1.0, 4.0, 2.0, "two")])
    assert quarantine.measure(both, CANVAS).overlap == 8.0


def test_text_drawn_outside_its_box_is_damage() -> None:
    """Where text falls is the renderer's decision, so it is asked rather than
    worked out."""
    spilling = Slide("s", [Shape(1.0, 1.0, 4.0, 1.0, "a long heading",
                                 drawn=(1.0, 1.0, 4.0, 3.0))])
    assert quarantine.measure(spilling, CANVAS).overflow == 8.0


def test_a_shape_pushed_off_the_page_is_damage() -> None:
    off = Slide("o", [Shape(12.333, 1.0, 2.0, 1.0, "half gone")])
    assert round(quarantine.measure(off, CANVAS).offcanvas, 2) == 1.0


def test_a_slide_that_will_not_answer_reads_as_undamaged() -> None:
    """The safe way round: it leaves the restyle exactly as it would be."""

    class Hostile:
        @property
        def Shapes(self):
            raise RuntimeError("COM said no")

    assert quarantine.measure(Hostile(), CANVAS).total == 0.0


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #

def test_a_restyle_that_wrecks_a_tidy_slide_is_refused() -> None:
    assert quarantine.wrecked(
        quarantine.Damage(), quarantine.Damage(overlap=9.0)
    )


def test_a_slide_that_arrived_broken_is_not_this_modules_business() -> None:
    """Made marginally worse is not made wrecked."""
    assert not quarantine.wrecked(
        quarantine.Damage(overlap=8.0), quarantine.Damage(overlap=8.5)
    )


def test_a_tidy_slide_picking_up_a_hairline_collision_is_left_alone() -> None:
    assert not quarantine.wrecked(
        quarantine.Damage(), quarantine.Damage(overlap=0.4)
    )


def test_a_restyle_that_improves_the_slide_is_never_refused() -> None:
    assert not quarantine.wrecked(
        quarantine.Damage(overlap=9.0), quarantine.Damage()
    )


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #

def _run(slides, plans, doubtful):
    presentation = Presentation(slides)
    outcomes = _restyle(presentation, {"Fits": Layout()}, plans, doubtful)
    return presentation, outcomes


def test_a_wrecked_slide_keeps_the_original_and_bins_the_restyle(
    monkeypatch,
) -> None:
    """The walk normally deletes the original and keeps the restyled copy.
    This is that decision inverted, and which one survives is the whole point.
    """
    # The copy measures as wrecked; the original does not.
    monkeypatch.setattr(
        quarantine, "measure",
        lambda slide, canvas: quarantine.Damage(
            overlap=9.0 if slide.tag.endswith("-copy") else 0.0
        ),
    )
    original = _tidy()
    presentation, outcomes = _run([original], {1: "Fits"}, {1})

    assert [s.tag for s in presentation._slides] == ["4"]
    assert presentation._slides[0] is original       # the very same object
    assert original.CustomLayout is None             # never restyled
    assert outcomes[0].quarantined
    assert not outcomes[0].applied                   # NOT on the master


def test_a_doubtful_slide_the_restyle_handled_is_still_restyled(
    monkeypatch,
) -> None:
    """Doubt is the gate, not the verdict. A thin master would otherwise hand
    back half the deck."""
    monkeypatch.setattr(
        quarantine, "measure", lambda slide, canvas: quarantine.Damage()
    )
    presentation, outcomes = _run([_tidy()], {1: "Fits"}, {1})

    assert [s.tag for s in presentation._slides] == ["4-copy"]
    assert not outcomes[0].quarantined
    assert outcomes[0].applied


def test_a_confident_slide_is_never_measured(monkeypatch) -> None:
    """Measuring costs two COM walks per slide. Only the doubtful ones pay."""
    seen: list[str] = []
    monkeypatch.setattr(
        quarantine, "measure",
        lambda slide, canvas: seen.append(slide.tag) or quarantine.Damage(),
    )
    _run([_tidy()], {1: "Fits"}, set())          # not doubtful

    assert seen == []


def test_the_slide_count_is_unchanged_either_way(monkeypatch) -> None:
    """The walk is a plain 1..n over a count that never moves. A quarantine
    that left the duplicate behind would shift every later slide."""
    monkeypatch.setattr(
        quarantine, "measure",
        lambda slide, canvas: quarantine.Damage(
            overlap=9.0 if slide.tag == "2-copy" else 0.0
        ),
    )
    presentation, outcomes = _run(
        [_tidy("1"), _wrecked("2"), _tidy("3")],
        {1: "Fits", 2: "Fits", 3: "Fits"},
        {1, 2, 3},
    )

    assert len(presentation._slides) == 3
    assert [o.quarantined for o in outcomes] == [False, True, False]
    # And the one handed back is the one that was wrecked, in its own place.
    assert presentation._slides[1].tag == "2"


def test_every_quarantined_slide_is_commented(monkeypatch) -> None:
    from formatting_tool.rebuild.master_apply import _annotate

    monkeypatch.setattr(
        quarantine, "measure",
        lambda slide, canvas: quarantine.Damage(
            overlap=9.0 if slide.tag.endswith("-copy") else 0.0
        ),
    )
    presentation, outcomes = _run([_tidy()], {1: "Fits"}, {1})
    _annotate(presentation, outcomes)

    assert len(presentation._slides[0].comments) == 1
    body = presentation._slides[0].comments[0]
    assert "left exactly as you sent it" in body
    assert "Fits" in body                      # the layout that was tried


def test_a_comment_that_cannot_be_added_does_not_cost_the_slide() -> None:
    """The slide is already kept by the time the comment is written."""

    class Mute:
        @property
        def Comments(self):
            raise RuntimeError("no comment pane")

    assert quarantine.annotate(Mute(), "anything") is False


# --------------------------------------------------------------------------- #
# What else must leave the slide alone
# --------------------------------------------------------------------------- #

def test_a_quarantined_slide_is_not_mirrored() -> None:
    """The mirror exists because the MASTER is drawn for the other reading
    direction, and this slide is not on the master. It is on the design its
    author built it in, already reading the way they meant. Turning it round
    is the exact mistake rebuild.rtl opens by warning about, done to the one
    slide that was promised it would be left as it arrived."""
    from formatting_tool.rebuild import rtl

    class Placeholder:
        Type = 14
        Left, Top, Width, Height = 72.0, 72.0, 144.0, 72.0

        def __init__(self):
            self.moved = False

        def __setattr__(self, name, value):
            if name == "Left":
                object.__setattr__(self, "moved", True)
            object.__setattr__(self, name, value)

    kept, restyled = Placeholder(), Placeholder()
    presentation = Presentation([Slide("1", [kept]), Slide("2", [restyled])])

    rtl.mirror_com(presentation, skip={1})

    assert not kept.moved
    assert restyled.moved


def test_the_report_names_every_slide_left_for_a_designer() -> None:
    """The whole point is that a person is told. A flag that reaches the JSON
    and not the report a designer reads is not a flag."""
    import io

    from formatting_tool.models import ValidationReport
    from formatting_tool.report import writers

    report = ValidationReport(master="m.pptx", decks=["d.pptx"], generated_at="now")
    report.master_applied = [{
        "deck": "d.pptx",
        "quarantined": [{
            "slide": 4, "nearest_layout": "Section Subheadline",
            "damage_before_sqin": 0.0, "damage_after_sqin": 31.4,
        }],
    }]

    for write in (writers.write_text, writers.write_markdown):
        out = io.StringIO()
        write(report, out)
        body = out.getvalue()
        assert "slide 4" in body.lower()
        assert "Section Subheadline" in body
        assert "31.4" in body
        assert "NOT" in body            # not on the master, said plainly


def test_a_route_that_cannot_hand_a_slide_back_says_so() -> None:
    """A silent difference between two hosts is the thing to avoid: the XML
    route rebuilds from the master and has no untouched slide to keep."""
    import io

    from formatting_tool.models import ValidationReport
    from formatting_tool.report import writers

    report = ValidationReport(master="m.pptx", decks=["d.pptx"], generated_at="now")
    report.master_applied = [{
        "deck": "d.pptx",
        "quarantined": [],
        "quarantine_note": "3 slide(s) were placed on a least-bad layout, and "
                           "this route cannot hand one back.",
    }]

    out = io.StringIO()
    writers.write_text(report, out)
    assert "cannot hand one back" in out.getvalue()


def test_a_clean_run_prints_no_quarantine_section() -> None:
    import io

    from formatting_tool.models import ValidationReport
    from formatting_tool.report import writers

    report = ValidationReport(master="m.pptx", decks=["d.pptx"], generated_at="now")
    out = io.StringIO()
    writers.write_text(report, out)
    assert "Left for a designer" not in out.getvalue()
