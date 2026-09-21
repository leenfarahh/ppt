"""What the design check's model said about a deck, and what it turns into.

    python what_the_model_saw.py DECK.pptx [SLIDE]

Runs the per-slide design check for real -- it renders through PowerPoint and
calls the model, so it needs both and it costs a call per slide -- then prints
each slide-level finding with the two fields that decide whether anything can
be done about it: the `arrangement` it named, and the shapes it named.

WHY THIS EXISTS. A finding a designer has to act on and a finding the tool
corrects arrive in the same bucket and read almost the same on the page. When
the shapes on a slide do not move, there are three different reasons and none
of them is visible from the outside:

  arrangement empty        the model did not see it as shapes out of line, so
                           nothing was ever going to move
  arrangement set,
  no moves                 the shapes are already in line within the floor, or
                           each row found holds too few shapes to measure from
  moves, then refused      the move was further than an alignment allows, or
                           the shape could not be found again

Pass a slide number to look at one slide instead of the whole deck.
"""

import sys
from pathlib import Path
from typing import Optional

from formatting_tool.ai.client import AIConfig
from formatting_tool.designqa import review_deck, steps_for
from formatting_tool.extract import read_deck
from formatting_tool.render import DESIGN_QA_SIZE, available_renderer, render_deck


def main(deck_path: str, only: Optional[int] = None) -> None:
    deck = Path(deck_path)
    profile = read_deck(deck)

    shots = render_deck(deck, available_renderer(DESIGN_QA_SIZE))
    if not shots:
        print(f"nothing to look at: {shots.reason}")
        return
    wanted = sorted(shots.images) if only is None else [only]
    print(f"{len(wanted)} slide(s) rendered by {shots.renderer}")

    try:
        report = review_deck(deck, shots.for_slides(wanted), AIConfig(),
                             profile=profile)
    finally:
        shots.cleanup()

    if report.reason:
        print(f"the check did not run: {report.reason}")
        return

    for review in report.reviews:
        if not review.reviewed:
            print(f"\nslide {review.slide}: not looked at -- {review.reason}")
            continue
        if not review.slide_issues:
            continue

        print(f"\nslide {review.slide}")
        for index, issue in enumerate(review.slide_issues):
            print(f"  {issue.task or issue.note}")
            print(f"    saw          {issue.note}")
            print("    arrangement  "
                  f"{issue.arrangement or '(none -- a note, not a relation)'}")
            if issue.members:
                print("    shapes       " + ", ".join(
                    f"{m.ref}={m.shape}" for m in issue.members))
            steps = steps_for(report, [f"slide:{review.slide}:{index}"])
            if not steps:
                print("    moves        none")
                continue
            for step in steps:
                print(f"    moves        {step.shape} -> "
                      f"left {step.left_in}in, top {step.top_in}in")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
