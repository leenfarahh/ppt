"""Where a deck's pictures actually live: on the slide, or on its layout.

Run it against the messy deck and, if you have it, the rebuilt one:

    python where_are_the_pictures.py "Presentation Design Support.pptx"
    python where_are_the_pictures.py "fixed-Presentation Design Support.pptx"

A picture ON THE SLIDE survives a layout swap. A picture on the LAYOUT is
inherited, was never a shape on the slide at all, and vanishes the moment the
slide is pointed at a different layout -- there is nothing to carry across
because there was never anything there. A picture PLACEHOLDER is the third
case: the shape is on the slide but its frame and its cropped-to shape are on
the layout, which is what `rebuild.pictures` freezes before the swap.

Which of the three it is decides what can be done about it, so this prints all
three per slide rather than a total.
"""

import sys
from pathlib import Path

from pptx import Presentation


def walk(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == 6:        # GROUP
            yield from walk(shape.shapes)


def is_picture(shape) -> bool:
    return shape.shape_type == 13        # PICTURE


def kind_of(shape) -> str:
    try:
        if shape.is_placeholder:
            token = str(shape.placeholder_format.type)
            filled = shape._element.find(
                ".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
            ) is not None
            return f"placeholder {token} ({'holds an image' if filled else 'EMPTY'})"
    except Exception:
        pass
    return "loose picture"


def main(path: str) -> None:
    prs = Presentation(path)
    print(f"\n{Path(path).name}\n" + "=" * len(Path(path).name))

    totals = {"slide": 0, "layout": 0, "empty": 0}
    for number, slide in enumerate(prs.slides, 1):
        on_slide = [s for s in walk(slide.shapes) if is_picture(s)]
        placeholders = [
            s for s in walk(slide.shapes)
            if not is_picture(s) and "placeholder" in kind_of(s)
            and "PICTURE" in kind_of(s).upper()
        ]
        layout = slide.slide_layout
        on_layout = [s for s in walk(layout.shapes) if is_picture(s)]

        totals["slide"] += len(on_slide)
        totals["layout"] += len(on_layout)
        totals["empty"] += len(placeholders)

        print(
            f"  slide {number:>3}  layout {layout.name!r:<40} "
            f"{len(on_slide)} on the slide, {len(on_layout)} on the layout"
        )
        for shape in on_slide:
            box = shape.width and shape.height
            print(
                f"           - {shape.name!r}: {kind_of(shape)}"
                f"{'' if box else '  (no frame of its own)'}"
            )
        for shape in on_layout:
            print(f"           ~ {shape.name!r} is on the LAYOUT, inherited")

    print(
        f"\n  TOTAL  {totals['slide']} picture(s) on slides, "
        f"{totals['layout']} inherited from layouts, "
        f"{totals['empty']} empty picture placeholder(s)"
    )
    print(
        "\n  A picture on a slide survives a rebuild. One on a layout does not:\n"
        "  it was never a shape on the slide, so there is nothing to carry."
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python where_are_the_pictures.py DECK.pptx [...]")
    for arg in sys.argv[1:]:
        main(arg)
