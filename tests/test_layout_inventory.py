"""What the model is told the master offers.

`ai.layout` shows the model a picture of the slide and describes the layouts in
words. Both halves have been wrong in ways with a visible consequence, and
these are the two.
"""

from __future__ import annotations

from formatting_tool.ai.layout import _inventory
from formatting_tool.models import (
    BrandGuidelines,
    Geometry,
    LayoutProfile,
    MasterSpec,
    ShapeProfile,
)


def _ph(name, token, left, top, width, height):
    return ShapeProfile(
        shape_id=abs(hash(name)) % 9999,
        name=name,
        shape_type="PLACEHOLDER (14)",
        geometry=Geometry(left_in=left, top_in=top, width_in=width, height_in=height),
        placeholder_type=token,
    )


def _decoration(name, left, top, width, height):
    return ShapeProfile(
        shape_id=abs(hash(name)) % 9999,
        name=name,
        shape_type="AUTO_SHAPE (1)",
        geometry=Geometry(left_in=left, top_in=top, width_in=width, height_in=height),
    )


def _spec(layouts) -> MasterSpec:
    return MasterSpec(
        source="master.pptx",
        width_in=13.333,
        height_in=7.5,
        guidelines=BrandGuidelines(name="none-supplied"),
        layouts=layouts,
    )


def test_an_image_region_is_not_described_as_content() -> None:
    """The defect this fixes, with the slide it broke.

    `Content with Image 01` was described as "offers 2 content, 1 title",
    because a PICTURE placeholder was folded in with the copy regions. Asked
    where a two-column text slide belonged, the model answered it, reasoning
    out loud that "this layout offers a title and two content placeholders" --
    the right deduction from what it had been told. The slide arrived on a
    layout with a half-page picture region nothing could fill.
    """
    layout = LayoutProfile(name="Content with Image 01", index=0, shapes=[
        _ph("Title", "TITLE (1)", 0.7, 0.7, 5.8, 0.6),
        _ph("Body", "BODY (2)", 0.7, 1.2, 5.8, 0.5),
        _ph("Picture", "PICTURE (18)", 0.0, 0.0, 6.5, 7.5),
    ])
    written = _inventory(_spec([layout]))

    assert "1 content" in written
    assert "1 image" in written
    assert "2 content" not in written


def test_the_decoration_a_layout_carries_is_described() -> None:
    """Placeholders are not what separates one layout from another on a real
    master. Seven of one master's fourteen layouts offer a title and a body
    region at the same inches, and differ only in a panel down one side or a
    band across the top -- which content has to live around and which nothing
    else the tool reads can see.
    """
    plain = LayoutProfile(name="Title with Content 01", index=0, shapes=[
        _ph("Title", "TITLE (1)", 0.7, 0.7, 11.0, 0.6),
        _ph("Body", "BODY (2)", 0.7, 1.2, 11.0, 0.5),
    ])
    panelled = LayoutProfile(name="Title with Content 03", index=1, shapes=[
        _ph("Title", "TITLE (1)", 0.7, 0.7, 11.0, 0.6),
        _ph("Body", "BODY (2)", 0.7, 1.2, 11.0, 0.5),
        _decoration("Rectangle 36", 9.68, 0.0, 3.65, 7.5),
    ])
    written = _inventory(_spec([plain, panelled]))

    assert "already on the page" in written
    assert "panel down the right" in written
    # And the plain one says nothing, which is the difference being drawn.
    assert written.count("already on the page") == 1


def test_a_thin_rule_is_not_called_decoration() -> None:
    """A hairline under a title is furniture; a panel down half the page is the
    layout. Reporting every logo and rule would bury the difference that
    matters in the ones that do not."""
    layout = LayoutProfile(name="Title with Content 01", index=0, shapes=[
        _ph("Title", "TITLE (1)", 0.7, 0.7, 11.0, 0.6),
        _decoration("Straight Connector 19", 0.71, 7.02, 11.92, 0.0),
        _decoration("Graphic 7", 11.96, 0.70, 0.67, 0.56),
    ])
    assert "already on the page" not in _inventory(_spec([layout]))


def test_the_sheet_numbers_key_the_written_list() -> None:
    """The tiles carry a number and the list has to carry the same one, or the
    two halves of the comparison cannot be put together."""
    from formatting_tool.ai.layoutsheet import LayoutSheet
    from pathlib import Path

    layouts = [
        LayoutProfile(name="Title Slide", index=0, shapes=[
            _ph("Title", "CENTER_TITLE (3)", 0.7, 2.6, 6.1, 2.2),
        ]),
        LayoutProfile(name="Agenda", index=1, shapes=[
            _ph("Item", "BODY (2)", 4.4, 2.0, 3.9, 0.6),
        ]),
    ]
    sheet = LayoutSheet(path=Path("layouts.png"),
                        numbers={"Title Slide": 1, "Agenda": 2})
    written = _inventory(_spec(layouts), sheet)

    assert "1. Title Slide" in written
    assert "2. Agenda" in written
