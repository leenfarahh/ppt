"""Group transforms, composed into slide coordinates.

A shape inside a group stores its position in the group's own child coordinate
system, not in slide inches. These tests pin the composition down without a
fixture deck: `_Frame` is arithmetic, and `descend` needs only an element with
an `a:xfrm` on it.
"""

from __future__ import annotations

from xml.etree import ElementTree

from formatting_tool.extract.deck_reader import _Frame

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_IN = 914400  # EMU per inch


def _group(off, ext, child_off, child_ext):
    """The smallest thing `_Frame.descend` will read: an object with an xfrm."""
    xml = (
        f'<grpSp xmlns:a="{_A}"><grpSpPr><a:xfrm>'
        f'<a:off x="{off[0]}" y="{off[1]}"/>'
        f'<a:ext cx="{ext[0]}" cy="{ext[1]}"/>'
        f'<a:chOff x="{child_off[0]}" y="{child_off[1]}"/>'
        f'<a:chExt cx="{child_ext[0]}" cy="{child_ext[1]}"/>'
        f"</a:xfrm></grpSpPr></grpSp>"
    )

    class _Shape:
        _element = ElementTree.fromstring(xml)

    return _Shape()


def test_identity_frame_leaves_a_top_level_shape_alone():
    box = _Frame().apply(3.0, 4.0, 5.0, 6.0)
    assert box == (3.0, 4.0, 5.0, 6.0)


def test_child_space_is_mapped_onto_the_slide():
    # The group sits at 1in,1in and is 1in square, but declares a 2in-square
    # child space: everything inside it draws at half the size it claims.
    frame = _Frame().descend(
        _group(off=(_IN, _IN), ext=(_IN, _IN), child_off=(0, 0), child_ext=(2 * _IN, 2 * _IN))
    )
    left, top, width, height = frame.apply(0.0, 0.0, 2 * _IN, 2 * _IN)
    assert (left, top) == (_IN, _IN)
    assert (width, height) == (_IN, _IN)


def test_child_offset_is_subtracted_before_scaling():
    # chOff is the origin of the child space, so a child sitting exactly on it
    # lands on the group's own top-left corner.
    frame = _Frame().descend(
        _group(off=(2 * _IN, 3 * _IN), ext=(_IN, _IN),
               child_off=(5 * _IN, 7 * _IN), child_ext=(_IN, _IN))
    )
    left, top, _, _ = frame.apply(5 * _IN, 7 * _IN, 0, 0)
    assert (left, top) == (2 * _IN, 3 * _IN)


def test_nested_groups_compose():
    half = _group(off=(0, 0), ext=(_IN, _IN), child_off=(0, 0), child_ext=(2 * _IN, 2 * _IN))
    frame = _Frame().descend(half).descend(half)
    # Halved twice: a 4in child inside two such groups occupies 1in.
    _, _, width, _ = frame.apply(0, 0, 4 * _IN, 0)
    assert width == _IN


def test_a_degenerate_group_is_left_alone_rather_than_dividing_by_zero():
    frame = _Frame().descend(
        _group(off=(_IN, _IN), ext=(_IN, _IN), child_off=(0, 0), child_ext=(0, 0))
    )
    assert frame.sx == 1.0 and frame.sy == 1.0


def test_a_group_with_no_xfrm_is_left_alone():
    class _Bare:
        _element = ElementTree.fromstring("<grpSp><grpSpPr/></grpSp>")

    before = _Frame(1.0, 2.0, 3.0, 4.0)
    after = before.descend(_Bare())
    assert (after.dx, after.dy, after.sx, after.sy) == (1.0, 2.0, 3.0, 4.0)


def test_a_child_transform_is_not_mistaken_for_the_groups_own():
    # grpSpPr carries no xfrm; the only one in the subtree belongs to a child
    # shape. A descendant search would scale by it. The frame must not move.
    xml = (
        f'<grpSp xmlns:a="{_A}"><grpSpPr/><sp><a:xfrm>'
        f'<a:off x="{9 * _IN}" y="{9 * _IN}"/><a:ext cx="{_IN}" cy="{_IN}"/>'
        f'<a:chOff x="0" y="0"/><a:chExt cx="{_IN}" cy="{_IN}"/>'
        f"</a:xfrm></sp></grpSp>"
    )

    class _Shape:
        _element = ElementTree.fromstring(xml)

    after = _Frame().descend(_Shape())
    assert (after.dx, after.dy, after.sx, after.sy) == (0.0, 0.0, 1.0, 1.0)
