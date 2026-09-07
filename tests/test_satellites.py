"""Tests for the defect that survived `RepeatedElementRule`.

The same designer markup that produced `test_repeats.py` had a second badge
wrong on another slide, and that one was reported by nothing: eleven badges
arranged around a hexagon, so there was no row, no column and no even spacing
for a rule measuring the slide to hold them to. What the set does have is a
partner each badge travels with, and an offset that should be identical on
every copy. These tests pin that reading down, and pin down the four ways the
pairing goes wrong on real decks.
"""

from __future__ import annotations

import math

from formatting_tool.extract.master_spec import derive_master_spec
from formatting_tool.models import (
    BrandGuidelines,
    DeckProfile,
    Geometry,
    ShapeProfile,
    SlideProfile,
)
from formatting_tool.rules import RuleContext
from formatting_tool.rules.repeats import SatelliteOffsetRule

CANVAS_W, CANVAS_H = 13.333, 7.5

# The measured geometry of the deck this rule was written against.
HEX_W, HEX_H = 0.64, 0.55
BADGE_W, BADGE_H = 0.23, 0.20
OFFSET_X = 0.33          # badge centre sits this far right of the hexagon's
BADGE_01_RISE = 0.20     # and one badge sat this far above where it should


def _shape(name: str, cx: float, cy: float, w: float, h: float,
           kind: str = "AUTO_SHAPE (1)", text: str = "") -> ShapeProfile:
    """A shape placed by its centre, because an offset is between centres."""
    return ShapeProfile(
        shape_id=abs(hash((name, round(cx, 3), round(cy, 3)))) % 99999,
        name=name,
        shape_type=kind,
        geometry=Geometry(left_in=cx - w / 2, top_in=cy - h / 2, width_in=w, height_in=h),
        text=text,
    )


def _ctx(slide: SlideProfile) -> RuleContext:
    master = DeckProfile(path="master.pptx", width_in=CANVAS_W, height_in=CANVAS_H)
    deck = DeckProfile(
        path="messy.pptx", width_in=CANVAS_W, height_in=CANVAS_H, slides=[slide]
    )
    return RuleContext(deck=deck, spec=derive_master_spec(master, BrandGuidelines()))


def _ring(count: int = 6, radius: float = 2.2, offsets=None) -> list[ShapeProfile]:
    """`count` hexagons on a circle, each with its badge offset to the right.

    A ring on purpose: it is the arrangement that defeats every rule measuring
    against the slide, and the one this rule has to work on.
    """
    shapes: list[ShapeProfile] = []
    for n in range(count):
        angle = 2 * math.pi * n / count
        hx = 6.6 + radius * math.cos(angle)
        hy = 3.7 + radius * math.sin(angle)
        dx, dy = (offsets or {}).get(n, (OFFSET_X, 0.0))
        shapes.append(_shape(f"Hexagon {n}", hx, hy, HEX_W, HEX_H))
        shapes.append(
            _shape(f"Badge {n}", hx + dx, hy + dy, BADGE_W, BADGE_H, text=f"{n + 1:02d}")
        )
    return shapes


# --------------------------------------------------------------------------- #
# The finding
# --------------------------------------------------------------------------- #

def test_a_badge_riding_high_on_its_hexagon_is_reported() -> None:
    """The defect nothing caught: on a ring, measured against its partner."""
    slide = SlideProfile(
        number=1,
        shapes=_ring(offsets={0: (OFFSET_X + 0.04, -BADGE_01_RISE)}),
    )

    issues = list(SatelliteOffsetRule().check(_ctx(slide)))

    assert len(issues) == 1
    assert issues[0].shape == "Badge 0"
    assert "0.20in up" in issues[0].message
    assert "0.04in right" in issues[0].message


def test_a_ring_whose_badges_all_agree_is_not_reported() -> None:
    assert list(SatelliteOffsetRule().check(_ctx(SlideProfile(number=1, shapes=_ring())))) == []


def test_the_partner_is_named_by_size_not_by_name() -> None:
    """Real decks reuse one shape name across a whole slide, so a name
    identifies nothing and can even repeat the subject back at the reader."""
    slide = SlideProfile(number=1, shapes=_ring(offsets={0: (OFFSET_X, -0.2)}))

    issue = list(SatelliteOffsetRule().check(_ctx(slide)))[0]

    assert f"{HEX_W:.2f} x {HEX_H:.2f}in" in issue.message


# --------------------------------------------------------------------------- #
# Mirroring, which is a design decision and not a defect
# --------------------------------------------------------------------------- #

def _mirrored(left: int = 4, right: int = 6, rise=None) -> list[ShapeProfile]:
    """Two columns, badges facing outward, deliberately uneven in size.

    Uneven on purpose: with the columns equal, the settled-majority check
    already stops a single-cohort reading, and a test cannot then tell whether
    mirroring is supported or whether that other guard caught it. At six and
    four the larger column alone clears the majority, so only real support for
    two cohorts keeps the rule quiet.
    """
    shapes: list[ShapeProfile] = []
    for n in range(left + right):
        on_left = n < left
        cx = 4.0 if on_left else 9.0
        cy = 1.2 + (n if on_left else n - left) * 0.9
        side = -OFFSET_X if on_left else OFFSET_X
        shapes.append(_shape(f"Hexagon {n}", cx, cy, HEX_W, HEX_H))
        shapes.append(
            _shape(f"Badge {n}", cx + side, cy + (rise or {}).get(n, 0.0),
                   BADGE_W, BADGE_H)
        )
    return shapes


def test_a_mirrored_layout_is_two_intents_not_one_defect() -> None:
    """Half the set with the badge on the left is how these diagrams are drawn.

    Demanding a single offset across the whole set reports every mirrored
    layout, which is most of them.
    """
    slide = SlideProfile(number=1, shapes=_mirrored())

    assert list(SatelliteOffsetRule().check(_ctx(slide))) == []


def test_one_member_of_a_mirrored_cohort_still_reports() -> None:
    slide = SlideProfile(number=1, shapes=_mirrored(rise={1: -0.22}))

    issues = list(SatelliteOffsetRule().check(_ctx(slide)))

    assert [i.shape for i in issues] == ["Badge 1"]


# --------------------------------------------------------------------------- #
# The four ways pairing goes wrong on a real deck
# --------------------------------------------------------------------------- #

def test_a_satellite_placed_elsewhere_is_not_called_drift() -> None:
    """A hero image beside a row of thumbnails is a layout, not a nudge.

    Without the ceiling a real deck reported a 3.59in "drift", which is a
    different arrangement reusing the same shape. The offset here is close
    enough to its partner to pair (inside `satellite_reach`) but far enough
    from the cohort to exceed `repeat_max_drift_in`, which is the only band in
    which the ceiling does any work.
    """
    slide = SlideProfile(number=1, shapes=_ring(offsets={0: (OFFSET_X, -1.5)}))

    assert list(SatelliteOffsetRule().check(_ctx(slide))) == []


def test_a_drift_just_under_the_ceiling_is_still_reported() -> None:
    """The other side of that band, so the ceiling cannot silence everything."""
    slide = SlideProfile(number=1, shapes=_ring(offsets={0: (OFFSET_X, -0.7)}))

    assert [i.shape for i in SatelliteOffsetRule().check(_ctx(slide))] == ["Badge 0"]


def test_stacked_duplicates_do_not_scramble_the_assignment() -> None:
    """Two guards cover this jointly, and measurement says both are needed.

    Decks stack a filled shape and its glyph in the same box, so a set of six
    badges arrives as twelve. Two things stop that from scrambling the pairing:
    collapsing the co-located copies, and claiming the shortest gaps first so a
    distant satellite cannot steal a close partner. On ten real decks either
    one alone was enough and removing both lost four true findings, which is
    why they overlap here rather than getting a test each.
    """
    shapes = _ring(offsets={0: (OFFSET_X + 0.04, -BADGE_01_RISE)})
    stacked: list[ShapeProfile] = []
    for shape in shapes:
        stacked.append(shape)
        if shape.name.startswith("Badge"):
            stacked.append(
                _shape(
                    shape.name + " glyph",
                    shape.geometry.left_in + shape.geometry.width_in / 2,
                    shape.geometry.top_in + shape.geometry.height_in / 2,
                    BADGE_W,
                    BADGE_H,
                )
            )

    issues = list(SatelliteOffsetRule().check(_ctx(SlideProfile(number=1, shapes=stacked))))

    assert [i.shape for i in issues] == ["Badge 0"]


def test_pairing_stays_local() -> None:
    """A satellite with no partner near it is left unpaired, not matched
    across the slide. Tested on the helper because the drift ceiling would
    otherwise mask the difference in the rule's output."""
    from formatting_tool.rules.repeats import _pair_up

    # Five partners for five satellites, so without a reach limit the stray
    # has a spare partner waiting for it and the count alone would not tell.
    partners = [_shape(f"P{n}", 2.0 + n, 2.0, HEX_W, HEX_H) for n in range(5)]
    satellites = [_shape(f"S{n}", 2.0 + n + OFFSET_X, 2.0, BADGE_W, BADGE_H)
                  for n in range(4)]
    satellites.append(_shape("Stray", 11.0, 6.8, BADGE_W, BADGE_H))

    pairs = _pair_up(satellites, partners, reach_factor=2.5)

    assert [s.name for s, _p in pairs] == ["S0", "S1", "S2", "S3"]


def test_two_copies_sharing_a_wrong_offset_are_both_reported() -> None:
    """Two agreeing is not an intent, it is two mistakes made the same way.

    This is what `satellite_cohort_min` decides. Set it low enough and any
    pair of matching errors becomes its own legitimate cohort, and the rule
    goes quiet on exactly the case a designer most wants to hear about.
    """
    slide = SlideProfile(
        number=1,
        shapes=_ring(offsets={4: (OFFSET_X, -0.22), 5: (OFFSET_X, -0.22)}),
    )

    issues = list(SatelliteOffsetRule().check(_ctx(slide)))

    assert sorted(i.shape for i in issues) == ["Badge 4", "Badge 5"]


def test_one_finding_per_shape_when_several_partners_qualify() -> None:
    """A badge on a hexagon inside an icon belongs to two valid pairings.

    Reported once per partner series it reads as two defects in one place; the
    real deck showed the same badge twice.
    """
    shapes = _ring(offsets={0: (OFFSET_X + 0.04, -BADGE_01_RISE)})
    for n in range(6):
        hexagon = next(s for s in shapes if s.name == f"Hexagon {n}")
        shapes.append(
            _shape(
                f"Icon {n}",
                hexagon.geometry.left_in + HEX_W / 2,
                hexagon.geometry.top_in + HEX_H / 2,
                0.45,
                0.50,
                kind="FREEFORM (5)",
            )
        )

    issues = list(SatelliteOffsetRule().check(_ctx(SlideProfile(number=1, shapes=shapes))))

    assert len(issues) == 1
    assert issues[0].shape == "Badge 0"


def test_a_set_too_small_to_show_a_majority_stays_silent() -> None:
    """Three pairs cannot have one departing from a majority of the rest."""
    slide = SlideProfile(number=1, shapes=_ring(count=3, offsets={0: (OFFSET_X, -0.2)}))

    assert list(SatelliteOffsetRule().check(_ctx(slide))) == []


def test_a_scatter_has_no_odd_one_out() -> None:
    """Three agreeing out of ten is not an intent, and the seven that do not
    agree are not seven defects.

    This is what the settled-majority check decides. Without it a slide whose
    spacing is loose everywhere reports most of itself, which is the fastest
    way to make a designer stop reading the report. No deck to hand triggers
    it, so the fixture stands in for one: a small cohort and a long tail, each
    member of the tail its own singleton but all of them inside the drift
    ceiling, which is the only band where the check does any work.
    """
    offsets = {n: (OFFSET_X + n * 0.07, n * 0.06) for n in range(1, 8)}
    slide = SlideProfile(number=1, shapes=_ring(count=10, radius=2.6, offsets=offsets))

    assert list(SatelliteOffsetRule().check(_ctx(slide))) == []


def test_a_hidden_slide_is_skipped() -> None:
    slide = SlideProfile(
        number=1, hidden=True, shapes=_ring(offsets={0: (OFFSET_X, -0.2)})
    )

    assert list(SatelliteOffsetRule().check(_ctx(slide))) == []
