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

The pick is advisory. `matcher.choose_layout` puts it below a layout-name match
that both files agree on, and `schema.layout_choices_from_response` drops any
pick naming a layout the master does not have, so the worst case is the
structural decision this was meant to improve on.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional, Sequence

from ..models import LayoutChoice, MasterSpec
from .gemini import Exhausted, build_client, file_part, generate_json
from .schema import LAYOUT_CHOICE_SCHEMA

log = logging.getLogger(__name__)

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"layout_choices": {"type": "array", "items": LAYOUT_CHOICE_SCHEMA}},
    "required": ["layout_choices"],
    "additionalProperties": False,
}

INSTRUCTIONS = """\
You are a presentation designer moving a slide onto an approved master. You are
shown one slide as it renders, and the layouts the master offers. Choose the
layout the slide belongs on.

Judge by what the slide IS and how its content is arranged: a cover, a section
divider, a table of contents, one column of copy, two columns, a comparison, a
full-bleed image, a chart. Match that against the regions each layout offers.
You are not judging which layout is prettiest, and you are not judging the
slide's own formatting -- something else does that.

- Copy the layout name exactly as given. A name that is not on the list is
  discarded and the slide keeps the structural choice instead.
- Set a low confidence when the master offers nothing that really fits, and say
  so in `why`. That the master has no cover layout is worth knowing; a
  confident guess is not.
"""


def choose_layouts(
    spec: MasterSpec,
    images: Sequence[tuple[int, Path]],
    model: str,
    thinking_budget: int,
    api_key_env: str = "GEMINI_API_KEY",
    concurrency: int = 6,
) -> list[LayoutChoice]:
    """One pick per rendered slide. Never raises; an empty list means the
    structural matcher decides on its own, which is what it did before."""
    if not images or not spec.layouts:
        return []

    inventory = _inventory(spec)
    allowed = {layout.name for layout in spec.layouts}
    try:
        client = build_client(api_key_env)
    except Exception as exc:
        log.warning("no layout pass: %s", exc)
        return []

    # One call per slide, several at a time. They are independent and spend
    # their time waiting, and a pick for slide 4 tells you nothing about slide
    # 5, so there is nothing to serialise them for.
    # Set when the account turns out to be spent, so the remaining slides do
    # not each ask and each be refused. One message, not one per slide.
    spent: list[Exception] = []

    def one(number, path):
        if spent:
            return []
        try:
            return _ask(client, number, path, inventory, allowed, model,
                        thinking_budget)
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
    if workers == 1:
        for number, path in images:
            picks.extend(one(number, path))
    else:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="layout") as pool:
            for got in pool.map(lambda pair: one(*pair), list(images)):
                picks.extend(got)
    # Back into slide order: the picks are keyed by slide downstream, but a
    # report reading them in the order they happened to finish is a report
    # that reads differently on every run.
    picks.sort(key=lambda choice: choice.slide)
    log.info("the model chose a layout for %d of %d rendered slide(s)",
             len(picks), len(images))
    return picks


def _ask(client, number, path, inventory, allowed, model, thinking_budget):
    from .schema import layout_choices_from_response  # noqa: PLC0415 - cycle

    contents = [
        f"The master offers these layouts:\n{inventory}\n\n"
        f"This is slide {number}, as PowerPoint renders it. Reply with one "
        f"entry whose `slide` is {number}.",
        file_part(client, path, "image/png"),
    ]
    data, _response = generate_json(
        client,
        model=model,
        contents=contents,
        system_instruction=INSTRUCTIONS,
        schema=_RESPONSE_SCHEMA,
        thinking_budget=thinking_budget,
        max_output_tokens=1024,
    )
    # Forced onto the slide actually shown. The model is asked for one entry
    # and given the number, but a wrong number here would move the wrong
    # slide, and that is not a mistake worth trusting it not to make.
    return [
        LayoutChoice(slide=number, layout=c.layout, confidence=c.confidence,
                     why=c.why)
        for c in layout_choices_from_response(data, allowed)
    ]


def _inventory(spec: MasterSpec) -> str:
    """Each layout and the regions it offers, one per line.

    Names alone do not describe them: "title_comparison" and
    "title_two_columns" are indistinguishable until you say one holds four
    content regions and the other two.
    """
    from ..models import MARGIN_CHROME  # noqa: PLC0415 - cycle

    lines = []
    for layout in spec.layouts:
        regions: dict[str, int] = {}
        for placeholder in layout.placeholders:
            token = (placeholder.placeholder_token or "BODY").upper()
            if token in MARGIN_CHROME:
                continue
            kind = (
                "title" if "TITLE" in token
                else "subtitle" if token == "SUBTITLE"
                else "content"
            )
            regions[kind] = regions.get(kind, 0) + 1
        offers = ", ".join(f"{n} {k}" for k, n in sorted(regions.items())) or "nothing"
        lines.append(f"- {layout.name}: offers {offers}")
    return "\n".join(lines)
