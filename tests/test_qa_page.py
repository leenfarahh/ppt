"""The design check's page, running its own JavaScript in a real browser.

THE PAGE IS THE HALF OF THIS TOOL NOTHING ELSE TESTS. pytest cannot parse it,
the route tests only prove the server answers, and a single typo in it does not
fail anything -- it blanks the results and leaves a page that loads, looks
calm, and shows nothing. That is the failure this exists to catch.

So the real script is lifted out of `qa.html`, given a `fetch` that answers with
the payloads the server actually sends, and driven through the sequence a
designer performs: run the check, apply the ticked steps, ask for a slide to be
drawn again. Headless Edge or Chrome dumps the resulting DOM and the assertions
read it.

The canned payloads are written out here rather than captured to a file on
purpose: they ARE the contract between `web/server.py` and the page, and a
change on the server side that this file does not follow should fail here
rather than in a designer's browser.

Skipped where no browser is installed. A test that needs Chrome is not a test
worth failing a checkout over.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

PAGE = Path("formatting_tool/web/static/qa.html")
TOKENS = Path("formatting_tool/web/static/tokens.css")

_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def _browsers() -> list[str]:
    """Every browser on this host, in the order worth trying.

    ALL OF THEM, NOT THE FIRST ONE, and that is not caution for its own sake:
    Edge stopped answering `--dump-dom` at some point between one run of this
    file and the next, exiting 0 with an empty stdout and nothing on stderr.
    Read as "no browser", that turns a test of the page into a skip, and a
    skip is indistinguishable from a pass in a summary line. So each is tried
    until one actually produces a document.
    """
    found = [path for path in _BROWSERS if Path(path).is_file()]
    if not found:
        pytest.skip("no Chrome or Edge on this host to run the page in")
    return found


# --------------------------------------------------------------------------- #
# What the server sends
# --------------------------------------------------------------------------- #

def _verdict(ref, shape_id, shape, status, issue, action, box, parent_id=None,
             proposal=None):
    verb = action != "none" and (action != "center" or bool(parent_id))
    return {
        "slide": 1, "ref": ref, "shape": shape, "shape_id": shape_id,
        "role": "body", "status": status, "issue": issue, "action": action,
        "note": f"{shape}: something to say",
        "task": f"do something about {shape}",
        "parent": "Circle 3" if parent_id else "", "parent_id": parent_id,
        "box": box, "fix": proposal,
        "executable": bool(verb or proposal),
    }


def _check() -> dict:
    """`/api/qa/check`, as `_run_qa` returns it."""
    shapes = [
        _verdict("s1", 2, "Title 1", "ok", "", "none", [0.05, 0.05, 0.6, 0.12]),
        _verdict("s4", 7, "Label 17", "issue", "cut_off", "widen",
                 [0.13, 0.24, 0.05, 0.12]),
        _verdict("s7.2", 9, "Icon 3", "issue", "off_center", "center",
                 [0.06, 0.59, 0.02, 0.04], parent_id=8),
        _verdict("s9", 12, "Chart 4", "issue", "crowded", "none",
                 [0.55, 0.3, 0.4, 0.4]),
        _verdict("s11", 14, "Caption 9", "issue", "too_small", "none",
                 [0.1, 0.8, 0.3, 0.06], proposal={"op": "set_font_size",
                                                  "size_pt": 11}),
    ]
    return {
        "session": "page-test",
        "renderer_ok": True,
        "aspect": 1.777778,
        "slides": [
            {"slide": 1, "before": "/api/qa/preview/page-test/before/1",
             "after": None},
        ],
        "logs": [],
        "elapsed_s": 12.3,
        "report": {
            "deck": "narrow.pptx", "generated_at": "now",
            "model": "gemini-3.1-pro-preview",
            "width_in": 13.333, "height_in": 7.5,
            "slides": [{
                "slide": 1, "shapes": shapes,
                "slide_issues": [{
                    "note": "the right half of the slide is empty",
                    "task": "run the cards across the full width",
                    "arrangement": "", "members": [],
                }, {
                    # The other kind of slide-level finding: a relation the
                    # file can measure, which is work this tool does rather
                    # than work it hands over.
                    "note": "the top row of circles does not line up",
                    "task": "level the top row on its top edge",
                    "arrangement": "align_top",
                    "members": [
                        {"ref": "s2", "shape": "Oval 2", "shape_id": 20,
                         "path": [2]},
                        {"ref": "s3", "shape": "Oval 3", "shape_id": 21,
                         "path": [3]},
                    ],
                }],
                "reviewed": True, "reason": "",
            }],
            # Every finding, as work. The page reads this rather than
            # re-deriving it, so the list on the screen and the list written
            # into the deck cannot disagree.
            "tasks": [
                {"id": "deck:0", "kind": "deck",
                 "what": "move the title on slide 1 up to match the rest",
                 "why": "the title sits lower here than on the other slides",
                 "slide": 1, "slides": [1], "shape": "", "shape_id": None,
                 "issue": "position", "fixable": True, "op": "align",
                 "box": None},
                {"id": "1:s4", "kind": "shape",
                 "what": "do something about Label 17",
                 "why": "Label 17: something to say", "slide": 1, "slides": [],
                 "shape": "Label 17", "shape_id": 7, "issue": "cut_off",
                 "fixable": True, "op": "widen",
                 "box": [0.13, 0.24, 0.05, 0.12]},
                {"id": "1:s7.2", "kind": "shape",
                 "what": "do something about Icon 3",
                 "why": "Icon 3: something to say", "slide": 1, "slides": [],
                 "shape": "Icon 3", "shape_id": 9, "issue": "off_center",
                 "fixable": True, "op": "center",
                 "box": [0.06, 0.59, 0.02, 0.04]},
                {"id": "1:s9", "kind": "shape",
                 "what": "do something about Chart 4",
                 "why": "Chart 4: something to say", "slide": 1, "slides": [],
                 "shape": "Chart 4", "shape_id": 12, "issue": "crowded",
                 "fixable": False, "op": "", "box": [0.55, 0.3, 0.4, 0.4]},
                # A proposal rather than a measured verb: applied by the
                # shared applier, and the page must not care which half of the
                # tool carries a correction out.
                {"id": "1:s11", "kind": "shape",
                 "what": "set this to 11pt, which is what the rest of the deck uses",
                 "why": "Caption 9: smaller than the rest", "slide": 1,
                 "slides": [], "shape": "Caption 9", "shape_id": 14,
                 "issue": "too_small", "fixable": True, "op": "set_font_size",
                 "box": [0.1, 0.8, 0.3, 0.06]},
                {"id": "slide:1:0", "kind": "slide",
                 "what": "run the cards across the full width",
                 "why": "the right half of the slide is empty",
                 "slide": 1, "slides": [], "shape": "", "shape_id": None,
                 "issue": "", "fixable": False, "op": "", "box": None},
                {"id": "slide:1:1", "kind": "slide",
                 "what": "level the top row on its top edge",
                 "why": "the top row of circles does not line up",
                 "slide": 1, "slides": [], "shape": "", "shape_id": None,
                 "issue": "align_top", "fixable": True, "op": "align",
                 "box": None},
            ],
            "deck_issues": [
                {"kind": "position", "slides": [1],
                 "note": "the title sits lower here than on the other slides",
                 "task": "move the title on slide 1 up to match the rest"},
            ],
            "consistency_reason": "",
            "notes": ["slide 1: run the cards across the full width"],
            "stats": {"slides": 1, "reviewed": 1, "shapes": 5, "issues": 4,
                      "actions": 5, "notes": 2, "tasks": 7, "mismatches": 1},
            "reason": "",
        },
    }


def _apply() -> dict:
    """`/api/qa/apply`, as `_run_qa_apply` returns it."""
    return {
        "session": "page-test",
        "applied": [{
            "op": "widen", "slide": 1, "shape_id": 7, "shape": "Label 17",
            "detail": "widened the box from 0.72in to 1.05in, so its words "
                      "stop breaking",
        }],
        "skipped": [{
            "op": "center", "slide": 1, "shape_id": 9, "shape": "Icon 3",
            "reason": "it is already centred in the shape that holds it",
        }],
        "reason": "",
        # What the round leaves behind, which is the other half of what the
        # button does: these went into the deck as comments.
        "outstanding": [
            {"id": "1:s7.2", "kind": "shape", "what": "do something about Icon 3",
             "why": "", "slide": 1, "slides": [], "shape": "Icon 3",
             "shape_id": 9, "issue": "off_center", "fixable": True,
             "op": "center", "box": None},
            {"id": "1:s9", "kind": "shape", "what": "do something about Chart 4",
             "why": "", "slide": 1, "slides": [], "shape": "Chart 4",
             "shape_id": 12, "issue": "crowded", "fixable": False, "op": "",
             "box": None},
            {"id": "slide:1:0", "kind": "slide",
             "what": "run the cards across the full width", "why": "",
             "slide": 1, "slides": [], "shape": "", "shape_id": None,
             "issue": "", "fixable": False, "op": "", "box": None},
        ],
        "comments_written": 3,
        "comments_asked": True,
        "slides": [{
            "slide": 1,
            "before": "/api/qa/preview/page-test/before/1",
            "after": "/api/qa/preview/page-test/after/1?run=1",
        }],
        "download": "/api/qa/download/page-test",
        "logs": [],
        "elapsed_s": 0.4,
    }


def _rendered() -> dict:
    """`/api/qa/render`, which answers a press of Re-render."""
    return {
        "session": "page-test",
        "slides": [{
            "slide": 1,
            "before": "/api/qa/preview/page-test/before/1?t=99",
            "after": "/api/qa/preview/page-test/after/1?run=1&t=99",
        }],
    }


# The renders are URLs on a server that is not running here, so the pictures do
# not load. Deliberately left that way: what is being tested is the markup the
# page builds -- which src it asks for, and where it puts the boxes -- and
# substituting a placeholder would throw away the half of that the assertions
# read.


def _harness(tmp_path: Path) -> Path:
    html = PAGE.read_text(encoding="utf-8")
    script = re.search(r"<script>\n(.*)</script>", html, re.S).group(1)
    body = re.search(r"<body>\n(.*?)\n<script>", html, re.S).group(1)
    style = re.search(r"<style>\n(.*?)</style>", html, re.S).group(1)

    canned = {
        "/api/context": {
            "version": "test", "api_key": True, "renderer": True,
            "guidelines": [], "rules": [],
            "defaults": {"model": "a-model", "effort": "high", "batch_size": 1},
        },
        "/api/qa/check": _check(),
        "/api/qa/apply": _apply(),
        "/api/qa/render": _rendered(),
    }

    harness = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>harness</title>
<style>{TOKENS.read_text(encoding="utf-8")}</style>
<style>{style}</style>
<script>
const CANNED = {json.dumps(canned)};
window.fetch = async (url) => {{
  const route = String(url).split("?")[0];
  const data = CANNED[route];
  if (!data) return {{ ok: false, status: 404,
                       json: async () => ({{ error: "no stub for " + route }}) }};
  return {{ ok: true, status: 200, json: async () => data }};
}};
</script>
</head>
<body>
{body}
<script>
{script}
</script>
<script>
(async () => {{
  const fail = (why) => {{
    document.title = "HARNESS-FAILED";
    const pre = document.createElement("pre");
    pre.id = "broke";
    pre.textContent = why;
    document.body.appendChild(pre);
  }};
  try {{
    state.deck = new File([new Uint8Array([1, 2, 3])], "narrow.pptx");
    renderFiles();
    await runCheck();
    if (!document.querySelector('input[data-key="deck:0"]')) {{
      return fail("the deck-level correction cannot be ticked");
    }}
    if (!document.querySelector("#results section")) return fail("the check drew nothing");
    await applyTicked();
    const again = document.querySelector(".rerender");
    if (!again) return fail("no way to render a slide again");
    await rerender(Number(again.dataset.slide), again);
    document.title = "HARNESS-OK";
  }} catch (err) {{
    fail(err && err.stack ? err.stack : String(err));
  }}
}})();
</script>
</body></html>
"""
    path = tmp_path / "harness.html"
    path.write_text(harness, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def dom(tmp_path_factory) -> str:
    """The page's DOM after a check, an apply and a re-render."""
    if not PAGE.is_file():
        pytest.skip("run from the project root")
    harness = _harness(tmp_path_factory.mktemp("page"))

    complaints = []
    for index, browser in enumerate(_browsers()):
        profile = tmp_path_factory.mktemp(f"profile{index}")
        finished = subprocess.run(
            [
                browser, "--headless=new", "--disable-gpu", "--no-sandbox",
                f"--user-data-dir={profile}",
                # Long enough for the awaited stubs to settle; they resolve
                # immediately, so this is a ceiling rather than a wait.
                "--virtual-time-budget=8000",
                "--dump-dom", harness.resolve().as_uri(),
            ],
            # Bytes, decoded here rather than by subprocess: the DOM is UTF-8
            # and Python would otherwise decode it with the console's code
            # page, which on Windows is cp1252 and raises on the first byte it
            # does not know.
            capture_output=True, timeout=180,
        )
        if finished.stdout:
            return finished.stdout.decode("utf-8", "replace")
        complaints.append(
            f"{Path(browser).name}: {finished.stderr.decode('utf-8', 'replace')[:200]}"
        )
    pytest.skip("no browser produced a DOM -- " + "; ".join(complaints))


def _results(dom: str) -> str:
    """Just the results, so an assertion cannot pass on the script's source.

    `--dump-dom` includes the inline script, which contains every string this
    file looks for. A match anywhere in the document would prove nothing.
    """
    return dom.split('<div id="results">', 1)[-1].split("<script>", 1)[0]


def test_the_page_runs_a_check_and_an_apply_without_throwing(dom: str) -> None:
    assert "<title>HARNESS-OK</title>" in dom, (
        re.search(r'<pre id="broke">(.{0,800})', dom, re.S).group(1)
        if 'id="broke">' in dom else "the page did not finish"
    )


def test_the_corrected_slide_is_shown_beside_the_original(dom: str) -> None:
    """The point of the page after an apply: a step described in points is a
    claim, and two pictures are the evidence for it."""
    results = _results(dom)

    assert "Before and after" in results
    assert 'class="shots pair"' in results
    assert ">before</span>" in results and ">after</span>" in results
    assert "/before/1" in results and "/after/1" in results


def test_what_changed_is_marked_on_both_pictures(dom: str) -> None:
    """One box a side, in the same place, so the eye lands on the shape that
    changed rather than hunting for it."""
    preview = _results(dom).split('<section class="preview">', 1)[1]
    shots = preview.split('<ul class="changed">', 1)[0].split('<div class="shot">')

    assert len(shots) == 3                        # the head, then two pictures
    assert shots[1].count('<span class="hl') == 1
    assert shots[2].count('<span class="hl') == 1
    # The same rectangle on both, because a font step does not move the box.
    assert re.search(r'style="left:([\d.]+%)', shots[1]).group(1) ==         re.search(r'style="left:([\d.]+%)', shots[2]).group(1)
    assert "widened the box from 0.72in to 1.05in" in preview


def test_a_step_that_was_refused_says_so_rather_than_showing_a_pair(dom: str) -> None:
    """A refused step has no second picture to be evidence of anything; what
    it has is a reason, and the reason is the finding."""
    pair = _results(dom).split('<section class="preview">', 1)[1]

    assert "not taken: it is already centred in the shape that holds it" in pair


def test_every_finding_is_offered_as_something_to_do(dom: str) -> None:
    """The whole list is work: what this tool can do carries a tick, and what
    it cannot carries the instruction and goes into the deck."""
    results = _results(dom)

    assert "task(s)" in results
    # The instruction, on the row, for a finding nothing can correct.
    assert "do something about Chart 4" in results
    assert "run the cards across the full width" in results
    # And the corrections named in words rather than in the schema's verbs,
    # whichever half of the tool carries them out.
    assert "widen the box" in results and "centre it in its holder" in results
    assert "align it to the deck" in results and "set the type size" in results


def test_what_is_left_after_a_round_is_listed_and_filed(dom: str) -> None:
    applied = _results(dom).split('<section class="applied">', 1)[1]

    assert "3 task(s) written into the deck as comments" in applied
    assert "task(s) still to do" in applied
    assert "run the cards across the full width" in applied


def test_a_slide_can_be_drawn_again(dom: str) -> None:
    """The cache is right nearly always, and "nearly always" is not something
    a designer can check from the outside."""
    results = _results(dom)

    assert 'class="linkish rerender"' in results
    assert "t=99" in results           # the URLs the re-render answered with


def test_a_slide_finding_sits_in_the_panel_that_matches_what_it_is(dom: str) -> None:
    """Both kinds arrive in the same bucket and no longer belong in the same
    panel. A row that does not line up is measured off the file and carries a
    tick; an empty half of a slide has no arithmetic and carries an
    instruction. Reading the task's `fixable` rather than the finding's shape
    is what keeps that honest as the arithmetic grows again."""
    results = _results(dom)

    ticked, handed = results.split("For a designer, on this slide", 1)
    assert "On this slide, and ticking it does it" in ticked
    assert 'data-key="slide:1:1"' in ticked
    assert "level the top row on its top edge" in ticked
    # And the one nothing can measure stayed where it was, with no tick.
    assert "run the cards across the full width" in handed
    assert 'data-key="slide:1:0"' not in results


def test_a_finished_round_offers_to_check_what_it_produced(dom: str) -> None:
    """The button sits with the round it is about, beside the download and
    after the pictures, because it is the thing to press once they have been
    looked at -- pressing it replaces them."""
    applied = _results(dom).split('<section class="applied">', 1)[1]

    assert 'id="recheck"' in applied
    assert "Check the corrected deck" in applied


def test_the_findings_still_read_as_findings(dom: str) -> None:
    results = _results(dom)

    assert "off_center" in results and "cut_off" in results
    assert "For a designer, on this slide" in results
    assert "Across the deck" in results          # the cross-slide mismatches
    assert 'href="#pair-1"' in results           # and the way to the evidence
