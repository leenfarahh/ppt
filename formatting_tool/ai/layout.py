"""Ask the model which master layout each slide belongs on, from its picture.

A separate, deliberately small call, made BEFORE the master is applied and
before anything is validated. It answers one question and returns one line per
slide, so it costs a fraction of the review and can run on every slide of a
deck that has not been looked at yet.

WHY THE MODEL IS ASKED AT ALL. `rebuild.matcher` decides structurally, by
counting the content regions a slide uses against the regions a layout offers.
On a tidy deck that is right. On a messy one it is not, because a messy deck
keeps its content in loose text boxes -- that is what makes it messy -- so the
count is of boxes rather than of regions. Measured on a real deck against a
five-layout master: it read a table of contents as a four-region comparison
layout, and a two-column slide as the same, where the model looking at the
render got both right. Four of five slides the model matched or beat it; the
one they agreed on was the cover.

WHY THE PICTURE AND NOT THE NUMBERS. This is a coarse categorical judgement --
is this a cover, a list, two columns -- which is what a render is good for. It
is not the same kind of question as "which of these eleven badges is 0.2in out
of line", which was measured on the same model and the same deck at roughly
one hit in five with more wrong answers than right ones. Placement stays with
the deterministic rules; what a slide IS comes from here.

BOTH SIDES ARE SHOWN, NOT ONE. The slide arrives as a picture and the layouts
used to arrive as a written list, which is half a comparison. On a real master
seven of fourteen layouts describe identically -- one title, one content
region, at the same inches -- and differ only in decoration no placeholder
records, so the model answered with the first of the seven every time, which is
the best answer available to anyone shown seven identical descriptions. The
master's layouts are now rendered onto one contact sheet and sent with every
slide; see `ai.layoutsheet`.

`matcher.choose_layout` takes the pick ahead of its own structural reading, and
`schema.layout_choices_from_response` drops any pick naming a layout the master
does not have, so the worst case is the structural decision this was meant to
improve on.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, Sequence

from ..models import MARGIN_CHROME, LayoutChoice, MasterSpec
from .gemini import Exhausted, build_client, file_part, generate_json
from .layoutsheet import LayoutSheet, build_sheet
from .schema import LAYOUT_CHOICE_SCHEMA

log = logging.getLogger(__name__)

# Room for the answer, on top of whatever thinking was asked for. Gemini
# counts thinking against `max_output_tokens`, so the flat 1024 this used to
# send was the two budgets sharing one allowance -- which held only while the
# question was easy enough not to think about. Adding the layout sheet made it
# think properly, and five of seventeen slides came back truncated mid-string
# with finish_reason=MAX_TOKENS: no pick at all, for the slides that most
# needed one. The answer is one short line whatever happens, so 1024 is ample
# for it; what it cannot also pay for is the reasoning.
_ANSWER_TOKENS = 1024

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"layout_choices": {"type": "array", "items": LAYOUT_CHOICE_SCHEMA}},
    "required": ["layout_choices"],
    "additionalProperties": False,
}

INSTRUCTIONS = """\
You are a presentation designer moving a slide onto an approved master. You are
shown one slide as it renders, a sheet of the master's layouts as THEY render,
and a written list of what each layout offers. Choose the layout the slide
belongs on.

Judge by what the slide IS and how its content is arranged: a cover, a section
divider, a table of contents, one column of copy, two columns, a comparison, a
full-bleed image, a chart. Match that against the regions each layout offers.
You are not judging which layout is prettiest, and you are not judging the
slide's own formatting -- something else does that.

READ THE SHEET, NOT ONLY THE LIST. Layouts that read identically in words are
routinely different pages: the same title and body region, with a panel down
one side of one and an image band across the top of another. On the sheet each
tile is numbered to match the list, its regions are labelled where they fall,
and everything else on the tile is decoration already on that layout. Content
has to live around that decoration, so a slide whose copy runs the full width
does not belong on a layout with half its page already taken.

PURPOSE BEATS RESEMBLANCE. Where the master has a layout built to be the thing
the slide is -- a cover, an agenda, a section divider, a closing -- that layout
wins, even if some other tile's background art happens to look more like the
slide in front of you. A slide titled "Agenda" belongs on the agenda layout,
whatever decoration it currently carries.

AN IMAGE REGION IS NOT A CONTENT REGION. A layout offering `1 title, 1 content,
1 image` holds ONE block of copy, not two. Putting a text slide on it leaves a
half-page picture region with nothing to fill it, which is a worse result than
a plain layout. Choose a layout with an image region only when the slide
actually carries a photograph for it.

- Copy the layout name exactly as given. A name that is not on the list is
  discarded and the slide keeps the structural choice instead.
- Set a low confidence when the master offers nothing that really fits, and say
  so in `why`. That the master has no cover layout is worth knowing; a
  confident guess is not. Low confidence does NOT mean your answer is thrown
  away -- it is still used when nothing else fits better -- so name the least
  wrong layout and explain what is missing rather than declining to choose.
"""


def choose_layouts(
    spec: MasterSpec,
    images: Sequence[tuple[int, Path]],
    model: str,
    thinking_budget: int,
    api_key_env: str = "GEMINI_API_KEY",
    concurrency: int = 6,
    master: Optional[Path] = None,
) -> list[LayoutChoice]:
    """One pick per rendered slide. Never raises; an empty list means the
    structural matcher decides on its own, which is what it did before.

    `master` is the template file. Given one, its layouts are rendered onto a
    single contact sheet and shown with every slide -- see `ai.layoutsheet` for
    why a written list is not enough on its own. Without one, or when the sheet
    cannot be made, the question is asked from the written list alone.
    """
    if not images or not spec.layouts:
        return []

    sheet = build_sheet(master, spec.layouts) if master else None
    inventory = _inventory(spec, sheet)
    allowed = {layout.name for layout in spec.layouts}
    try:
        client = build_client(api_key_env)
    except Exception as exc:
        log.warning("no layout pass: %s", exc)
        if sheet:
            sheet.cleanup()
        return []

    # One call per slide, several at a time. They are independent and spend
    # their time waiting, and a pick for slide 4 tells you nothing about slide
    # 5, so there is nothing to serialise them for.
    # Set when the account turns out to be spent, so the remaining slides do
    # not each ask and each be refused. One message, not one per slide.
    spent: list[Exception] = []

    # Built once and handed to every call. The same bytes go up with all
    # seventeen slides, and reading and encoding the sheet once per slide is
    # work with no answer attached to it.
    sheet_part = None
    if sheet is not None:
        try:
            sheet_part = file_part(client, sheet.path, "image/png")
        except Exception as exc:
            log.warning("could not attach the layout sheet: %s", exc)

    def one(number, path):
        if spent:
            return []
        try:
            return _ask(client, number, path, inventory, allowed, model,
                        thinking_budget, sheet_part)
        except Exhausted as exc:
            if not spent:
                spent.append(exc)
                log.error("no layout pass: %s", exc)
            return []
        except Exception as exc:      # never fatal: the matcher decides alone
            log.warning("no layout pick for slide %d: %s", number, exc)
            return []

    picks: list[LayoutChoice] = []
    workers = max(1, min(concurrency, len(images)))
    try:
        if workers == 1:
            for number, path in images:
                picks.extend(one(number, path))
        else:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="layout") as pool:
                for got in pool.map(lambda pair: one(*pair), list(images)):
                    picks.extend(got)
    finally:
        if sheet is not None:
            sheet.cleanup()
    # Back into slide order: the picks are keyed by slide downstream, but a
    # report reading them in the order they happened to finish is a report
    # that reads differently on every run.
    picks.sort(key=lambda choice: choice.slide)
    log.info("the model chose a layout for %d of %d rendered slide(s)",
             len(picks), len(images))
    return picks


def _ask(client, number, path, inventory, allowed, model, thinking_budget,
         sheet_part=None):
    from .schema import layout_choices_from_response  # noqa: PLC0415 - cycle

    contents: list[Any] = [f"The master offers these layouts:\n{inventory}"]
    if sheet_part is not None:
        contents.append(
            "This is every one of those layouts as PowerPoint renders it, "
            "numbered to match the list above. The labelled boxes are the "
            "regions; everything else on a tile is decoration the layout "
            "already carries."
        )
        contents.append(sheet_part)
    contents.append(
        f"This is slide {number}, as PowerPoint renders it. Reply with one "
        f"entry whose `slide` is {number}."
    )
    contents.append(file_part(client, path, "image/png"))
    data, _response = generate_json(
        client,
        model=model,
        contents=contents,
        system_instruction=INSTRUCTIONS,
        schema=_RESPONSE_SCHEMA,
        thinking_budget=thinking_budget,
        max_output_tokens=_ANSWER_TOKENS + max(0, thinking_budget),
    )
    # Forced onto the slide actually shown. The model is asked for one entry
    # and given the number, but a wrong number here would move the wrong
    # slide, and that is not a mistake worth trusting it not to make.
    return [
        LayoutChoice(slide=number, layout=c.layout, confidence=c.confidence,
                     why=c.why)
        for c in layout_choices_from_response(data, allowed)
    ]


def _inventory(spec: MasterSpec, sheet: Optional[LayoutSheet] = None) -> str:
    """Each layout, the regions it offers, and what is already on the page.

    A PICTURE REGION IS COUNTED AS ITS OWN KIND, and that correction is the
    point of this. It used to be folded in with the rest, so a master's
    `Content with Image 01` was described as "offers 2 content, 1 title" and
    `Project Card` the same. The model then did the sensible thing with what it
    had been told: on a two-column text slide it answered `Content with Image
    01`, reasoning out loud that "this layout offers a title and two content
    placeholders". The slide arrived on a layout with a half-page picture
    region nothing could fill. `rebuild.matcher` was taught the difference
    between a picture region and a block of copy; this was still saying they
    were the same thing.

    THE DECORATION IS DESCRIBED TOO, because on a real master it is most of
    what separates one layout from another -- seven layouts offering one title
    and one body region at the same inches, differing only in a panel down one
    side or a band across the top. The sheet shows that far better than words
    do, and the words are here for when there is no sheet.
    """
    lines = []
    for layout in spec.layouts:
        regions: dict[str, int] = {}
        for placeholder in layout.placeholders:
            token = (placeholder.placeholder_token or "BODY").upper()
            if token in MARGIN_CHROME:
                continue
            kind = (
                "title" if "TITLE" in token and token != "SUBTITLE"
                else "subtitle" if token == "SUBTITLE"
                else "image" if token == "PICTURE"
                else "content"
            )
            regions[kind] = regions.get(kind, 0) + 1
        offers = ", ".join(f"{n} {k}" for k, n in sorted(regions.items())) or "nothing"
        number = sheet.number_of(layout.name) if sheet else None
        head = f"{number}. {layout.name}" if number else f"- {layout.name}"
        lines.append(f"{head}: offers {offers}")
        for region in _region_lines(layout, spec):
            lines.append(f"    {region}")
        already = _furniture(layout, spec)
        if already:
            lines.append(f"    already on the page: {already}")
    return "\n".join(lines)


def _region_lines(layout: Any, spec: MasterSpec) -> list[str]:
    """Where each region sits, as a share of the page.

    Shares rather than inches so that a 4:3 master and a 16:9 one read the
    same, and because the question is which part of the page a region occupies
    rather than how far it is from the edge.
    """
    out = []
    for placeholder in layout.placeholders:
        token = (placeholder.placeholder_token or "BODY").upper()
        if token in MARGIN_CHROME:
            continue
        box = placeholder.geometry
        if box is None or box.width_in <= 0 or box.height_in <= 0:
            continue
        kind = (
            "title" if "TITLE" in token and token != "SUBTITLE"
            else "subtitle" if token == "SUBTITLE"
            else "image region" if token == "PICTURE"
            else "content"
        )
        out.append(f"{kind}: {_where(box, spec)}")
    return out


def _where(box: Any, spec: MasterSpec) -> str:
    width = box.width_in / spec.width_in
    height = box.height_in / spec.height_in
    left = box.left_in / spec.width_in
    top = box.top_in / spec.height_in
    return (
        f"{width * 100:.0f}% of the page wide and {height * 100:.0f}% tall, "
        f"starting {left * 100:.0f}% across and {top * 100:.0f}% down"
    )


# How much of the page a shape has to cover before it shapes where content can
# go. A thin rule under a title is furniture; a panel down half the page is the
# layout. Measured against one real master: its decorative rules and 0.67in
# logos come to well under a twentieth of the canvas, and the panels and bands
# that actually differ between layouts run from 7% to 50%.
_FURNITURE_SHARE = 0.06


def _furniture(layout: Any, spec: MasterSpec) -> str:
    """The decoration already on a layout, described the way a designer would.

    Only the shapes big enough to matter. Content has to live around these, and
    they are invisible to everything else the tool reads -- a layout's
    placeholders say nothing about the image band sitting on top of them.
    """
    seen: list[str] = []
    for shape in layout.shapes:
        if shape.placeholder_type:
            continue
        box = shape.geometry
        if box is None or box.width_in <= 0 or box.height_in <= 0:
            continue
        share = (box.width_in * box.height_in) / (spec.width_in * spec.height_in)
        if share < _FURNITURE_SHARE:
            continue
        phrase = _shape_phrase(box, spec)
        if phrase not in seen:
            seen.append(phrase)
    return "; ".join(seen[:4])


def _shape_phrase(box: Any, spec: MasterSpec) -> str:
    width = box.width_in / spec.width_in
    height = box.height_in / spec.height_in
    left = box.left_in / spec.width_in
    top = box.top_in / spec.height_in
    if width >= 0.9 and height < 0.7:
        side = "top" if top < 0.2 else "bottom" if top + height > 0.8 else "middle"
        return f"a band across the {side} {height * 100:.0f}% of the page"
    if height >= 0.9 and width < 0.7:
        side = "left" if left < 0.2 else "right"
        return f"a panel down the {side} {width * 100:.0f}% of the page"
    if width >= 0.9 and height >= 0.9:
        return "artwork covering the whole page"
    return (
        f"a block {width * 100:.0f}% wide and {height * 100:.0f}% tall, "
        f"{left * 100:.0f}% across and {top * 100:.0f}% down"
    )
