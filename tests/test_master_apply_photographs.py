"""Keeping a photograph's circle and its place across the layout swap.

The PowerPoint route's half of the same bug the XML route has in
`tests/test_rebuild_inherited_geometry.py`: assigning `CustomLayout` re-runs
inheritance, and a picture placeholder whose circle and position live on the
OLD layout loses both. Measured against desktop PowerPoint on the fixture in
that file, which is where the constants below come from:

    slide's picture placeholder     AutoShapeType 138 (msoShapeNotPrimitive)
    the layout placeholder it       AutoShapeType 9   (msoShapeOval)
    inherits from
    the same shape's Left/Top/      already the effective value, no lookup
    Width/Height                    needed

So the frame can be read off the slide and the geometry cannot. Fakes stand in
for the COM objects here; the round trip through real PowerPoint is not
something a test suite can depend on.
"""

from __future__ import annotations

from formatting_tool.rebuild.master_apply import (
    _MSO_NOT_PRIMITIVE,
    _designed_geometry,
    _has_picture_slot,
    _photographs,
    _restore,
)

_MSO_PLACEHOLDER = 14
_MSO_PICTURE = 13
_MSO_TEXT = 1
_MSO_OVAL = 9
_PP_PICTURE = 18
_PP_BODY = 2

FRAME = (141.125, 48.25, 432.0, 324.0)


class _PlaceholderFormat:
    def __init__(self, kind, contained=None):
        self.Type = kind
        if contained is not None:
            self.ContainedType = contained

    def __getattr__(self, name):
        # An empty picture placeholder's `ContainedType` refuses to be read
        # rather than answering with a "nothing in here" value.
        raise RuntimeError("-2147352567")


class _Shape:
    def __init__(
        self,
        shape_id,
        name,
        *,
        kind=_MSO_PLACEHOLDER,
        ph=_PP_PICTURE,
        contained=_MSO_PICTURE,
        geometry=_MSO_NOT_PRIMITIVE,
        frame=FRAME,
    ):
        self.Id = shape_id
        self.Name = name
        self.Type = kind
        self.AutoShapeType = geometry
        self.Left, self.Top, self.Width, self.Height = frame
        self.PlaceholderFormat = _PlaceholderFormat(ph, contained)


class _Shapes:
    def __init__(self, shapes):
        self._shapes = shapes
        self.Count = len(shapes)

    def __call__(self, index):
        return self._shapes[index - 1]


class _Container:
    def __init__(self, shapes, layout=None):
        self.Shapes = _Shapes(shapes)
        if layout is not None:
            self.CustomLayout = layout


def _slide(shapes, layout_shapes=None):
    layout = _Container(layout_shapes) if layout_shapes is not None else None
    return _Container(shapes, layout)


# --------------------------------------------------------------------------- #
# Reading the designed geometry off the old layout
# --------------------------------------------------------------------------- #

def test_the_circle_is_read_off_the_layout_not_the_slide() -> None:
    """The point of the whole exercise. COM answers `msoShapeNotPrimitive` for
    the slide's own shape, which is true and useless: having no geometry of
    its own is the bug."""
    portrait = _Shape(3, "Picture Placeholder 2")
    layout_ph = _Shape(3, "Picture Placeholder 2", geometry=_MSO_OVAL)

    snapshot = _photographs(_slide([portrait], [layout_ph]))

    assert snapshot[3] == (*FRAME, _MSO_OVAL)


def test_geometry_the_slide_states_itself_wins() -> None:
    """A photo the designer cropped on the slide is not inheriting anything,
    and the layout has no say over it."""
    portrait = _Shape(3, "Picture Placeholder 2", geometry=_MSO_OVAL)
    layout_ph = _Shape(3, "Picture Placeholder 2", geometry=5)

    snapshot = _photographs(_slide([portrait], [layout_ph]))

    assert snapshot[3][4] == _MSO_OVAL


def test_a_layout_with_no_geometry_to_give_records_none() -> None:
    portrait = _Shape(3, "Picture Placeholder 2")
    layout_ph = _Shape(3, "Picture Placeholder 2")

    snapshot = _photographs(_slide([portrait], [layout_ph]))

    assert snapshot[3][4] == _MSO_NOT_PRIMITIVE


def test_only_picture_placeholders_are_read_off_the_layout() -> None:
    """A body placeholder's rounded corners are the template's business and
    are not carried onto a photo."""
    layout = _Container([
        _Shape(2, "Text Placeholder 1", ph=_PP_BODY, geometry=_MSO_OVAL),
        _Shape(3, "Picture Placeholder 2", geometry=5),
    ])

    assert _designed_geometry(_Container([], layout)) == [
        (FRAME, "Picture Placeholder 2", 5)
    ]


def test_a_renamed_photograph_still_finds_its_slot() -> None:
    """The bug that survived the first fix. PowerPoint renames the shape when
    a photo goes into a picture placeholder, so the slot has to be found by
    the frame it is handing down, not by the name it no longer shares."""
    portrait = _Shape(3, "Picture 5")
    layout_ph = _Shape(9, "Picture Placeholder 3", geometry=_MSO_OVAL)

    snapshot = _photographs(_slide([portrait], [layout_ph]))

    assert snapshot[3][4] == _MSO_OVAL


def test_a_renamed_photograph_that_moved_falls_back_to_the_only_slot() -> None:
    """Neither the name nor the frame matches, which is what a slide overriding
    its inherited position looks like. One picture slot on the layout leaves
    nothing else it could have been cropped by."""
    portrait = _Shape(3, "Picture 5", frame=(10.0, 20.0, 30.0, 40.0))
    layout_ph = _Shape(9, "Picture Placeholder 3", geometry=_MSO_OVAL)

    snapshot = _photographs(_slide([portrait], [layout_ph]))

    assert snapshot[3][4] == _MSO_OVAL


def test_two_slots_and_no_way_to_tell_records_nothing() -> None:
    """Guessing between them would crop a photo to the wrong shape, which is a
    worse outcome than leaving it as PowerPoint made it."""
    portrait = _Shape(3, "Picture 5", frame=(10.0, 20.0, 30.0, 40.0))
    layout = [
        _Shape(9, "Picture Placeholder 3", geometry=_MSO_OVAL),
        _Shape(10, "Picture Placeholder 4", geometry=5, frame=(1.0, 2.0, 3.0, 4.0)),
    ]

    snapshot = _photographs(_slide([portrait], layout))

    assert snapshot[3][4] == _MSO_NOT_PRIMITIVE


def test_the_right_slot_wins_when_two_are_on_offer() -> None:
    portrait = _Shape(3, "Picture 5", frame=(1.0, 2.0, 3.0, 4.0))
    layout = [
        _Shape(9, "Picture Placeholder 3", geometry=_MSO_OVAL),
        _Shape(10, "Picture Placeholder 4", geometry=5, frame=(1.0, 2.0, 3.0, 4.0)),
    ]

    snapshot = _photographs(_slide([portrait], layout))

    assert snapshot[3][4] == 5


def test_a_slide_whose_layout_cannot_be_read_still_snapshots_the_frame() -> None:
    """Losing the circle is a cosmetic regression. Losing the frame puts the
    photo at the origin, so the frame is never made to depend on the layout."""
    portrait = _Shape(3, "Picture Placeholder 2")

    snapshot = _photographs(_Container([portrait]))    # no CustomLayout at all

    assert snapshot[3] == (*FRAME, _MSO_NOT_PRIMITIVE)


# --------------------------------------------------------------------------- #
# What counts as a photograph
# --------------------------------------------------------------------------- #

def test_a_text_placeholder_is_not_a_photograph() -> None:
    text = _Shape(2, "Title 1", ph=_PP_BODY, contained=_MSO_TEXT)

    assert _photographs(_slide([text], [])) == {}


def test_a_loose_picture_is_not_a_photograph() -> None:
    """It carries its own frame and geometry, so the swap cannot touch it and
    there is nothing to preserve."""
    picture = _Shape(4, "Portrait", kind=_MSO_PICTURE, contained=_MSO_PICTURE)

    assert _photographs(_slide([picture], [])) == {}


def test_an_empty_picture_placeholder_is_skipped() -> None:
    """Its `ContainedType` refuses to be read, and there is no photograph in
    it to preserve. It should still be offered the new layout's frame."""
    empty = _Shape(3, "Picture Placeholder 2", contained=None)

    assert _photographs(_slide([empty], [])) == {}


# --------------------------------------------------------------------------- #
# Putting it back
# --------------------------------------------------------------------------- #

def test_the_circle_goes_back_and_the_frame_does_not() -> None:
    """The new layout has a picture placeholder, so PowerPoint has just put
    the photo in the approved spot. Overriding that would discard the point of
    applying a master. The circle is still the writer's intent."""
    moved = _Shape(
        3, "Picture Placeholder 2", geometry=1, frame=(0.0, 10.0, 100.0, 80.0)
    )

    _restore(_Container([moved]), {3: (*FRAME, _MSO_OVAL)}, True)

    assert moved.AutoShapeType == _MSO_OVAL
    assert (moved.Left, moved.Top, moved.Width, moved.Height) == (
        0.0, 10.0, 100.0, 80.0
    )


def test_both_go_back_when_the_new_layout_has_no_picture_slot() -> None:
    """The reported case: x collapsed to 0 and the crop to a rectangle."""
    moved = _Shape(
        3, "Picture Placeholder 2", geometry=1, frame=(0.0, 48.25, 432.0, 324.0)
    )

    _restore(_Container([moved]), {3: (*FRAME, _MSO_OVAL)}, False)

    assert moved.AutoShapeType == _MSO_OVAL
    assert (moved.Left, moved.Top, moved.Width, moved.Height) == FRAME


def test_a_shape_with_no_recorded_geometry_keeps_what_it_has() -> None:
    """Writing `msoShapeNotPrimitive` back is not a no-op in PowerPoint, so a
    photograph whose old layout said nothing is left alone."""
    moved = _Shape(3, "Picture Placeholder 2", geometry=1)

    _restore(_Container([moved]), {3: (*FRAME, _MSO_NOT_PRIMITIVE)}, False)

    assert moved.AutoShapeType == 1


def test_a_shape_nobody_recorded_is_left_where_the_layout_put_it() -> None:
    """Everything that is not a photograph moving into the new layout's
    geometry is the entire point of the rebuild."""
    title = _Shape(2, "Title 1", frame=(1.0, 2.0, 3.0, 4.0))

    _restore(_Container([title]), {3: (*FRAME, _MSO_OVAL)}, False)

    assert (title.Left, title.Top, title.Width, title.Height) == (1.0, 2.0, 3.0, 4.0)


def test_nothing_recorded_touches_nothing() -> None:
    title = _Shape(2, "Title 1", frame=(1.0, 2.0, 3.0, 4.0))

    _restore(_Container([title]), {}, False)

    assert (title.Left, title.Top) == (1.0, 2.0)


# --------------------------------------------------------------------------- #
# Whether the target layout wants the photo somewhere
# --------------------------------------------------------------------------- #

def test_a_layout_offering_a_picture_slot_is_recognised() -> None:
    layout = _Container([
        _Shape(2, "Title 1", ph=_PP_BODY),
        _Shape(3, "Picture Placeholder 2", ph=_PP_PICTURE),
    ])

    assert _has_picture_slot(layout) is True


def test_a_layout_offering_none_is_recognised() -> None:
    layout = _Container([_Shape(2, "Title 1", ph=_PP_BODY)])

    assert _has_picture_slot(layout) is False


def test_a_layout_that_cannot_be_read_keeps_the_source_frame() -> None:
    """False is the answer that loses nothing: a photo left where the designer
    put it is wrong only cosmetically, and one at the origin is wrong on
    sight."""
    assert _has_picture_slot(object()) is False
