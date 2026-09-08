"""What every colour in a deck would be recoloured to, and what is left alone.

    python what_snaps_where.py MASTER.pptx DECK.pptx

Prints the master's palette, then every hardcoded colour the deck uses and
what becomes of it. A colour is off-palette past 3.0 delta-E, but it is only
recoloured when the palette holds an entry of the same hue family and the same
neutrality within 12.0 -- which is what stops a red becoming an orange and a
page of blue headings becoming grey.
"""

import sys
from collections import Counter

from formatting_tool.colorutil import intended_palette_entry, nearest_palette_entry
from formatting_tool.extract import read_deck
from formatting_tool.extract.master_spec import derive_master_spec
from formatting_tool.models import BrandGuidelines, RuleTuning, Tolerances, walk_shapes

TOLERANCE = Tolerances().color_delta_e
GATE = TOLERANCE * RuleTuning().suggestion_factor


def main(master_path: str, deck_path: str) -> None:
    spec = derive_master_spec(read_deck(master_path), BrandGuidelines())
    palette = spec.palette
    deck = read_deck(deck_path)

    print(f"\nmaster palette ({len(palette)} entries)")
    for label, value in palette.items():
        print(f"   {label:20} #{value}")

    used: Counter = Counter()
    for slide in deck.slides:
        for shape in walk_shapes(slide.shapes):
            for value in (shape.fill_hex, shape.line_hex):
                if value:
                    used[value.upper()] += 1
            for colour in shape.graphic_colors:
                if not colour.theme:
                    used[colour.hex] += 1
            for paragraph in shape.paragraphs:
                for run in paragraph.runs:
                    if run.color_hex and run.text.strip():
                        used[run.color_hex.upper()] += 1

    print(f"\n{len(used)} distinct hardcoded colour(s) in {deck.name}")
    print(f"{'colour':>10}  {'uses':>5}  outcome")
    for value, count in used.most_common():
        label, distance = nearest_palette_entry(value, palette)
        if distance is None:
            continue
        target, gap = intended_palette_entry(value, palette, GATE)
        if distance <= TOLERANCE:
            verdict = "on palette, left alone"
        elif target:
            verdict = f"recoloured to {target} (delta-E {gap:.1f})"
        else:
            verdict = f"LEFT FOR A DESIGNER (nearest {label} is {distance:.1f} away)"
        print(f"   #{value}  {count:>5}  {verdict}")

    print(
        f"\n   Under {TOLERANCE} delta-E is on palette. Recoloured only within"
        f" {GATE}, the same"
        f"\n   hue family and the same neutrality. Anything else is a design call."
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python what_snaps_where.py MASTER.pptx DECK.pptx")
    main(sys.argv[1], sys.argv[2])
