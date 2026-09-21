"""The deck check's script compiles, and carries the hand-over to the design check.

`test_qa_page.py` drives the design page's script in a real browser because
pytest cannot parse it and a typo in it does not fail anything -- it blanks the
results and leaves a page that loads, looks calm, and shows nothing. The deck
check's script had no such cover at all, and it is the larger of the two.

This asks a much smaller question than that file does, and on purpose. It does
not drive a run, stub a fetch or assert on rendered markup; it hands the script
to the engine and asks whether it COMPILES. That is the failure worth catching
cheaply here: a page whose every button is dead because one line above them did
not parse.

Compiled rather than run, through `new Function`, which is the distinction that
makes this reliable. Running it would need the page's markup, its stubs and its
boot sequence -- everything `test_qa_page.py` builds for the other page -- and
would then fail on a missing element rather than on the syntax this is about.

Skipped where no browser is installed, on the same reasoning as the other file:
a test that needs Chrome is not a test worth failing a checkout over.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from test_qa_page import _browsers, _dump_dom

PAGE = Path("formatting_tool/web/static/index.html")


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> str:
    """What the engine said when asked to compile the page's script."""
    if not PAGE.is_file():
        pytest.skip("run from the project root")
    html = PAGE.read_text(encoding="utf-8")
    script = re.search(r"<script>\n(.*)</script>", html, re.S).group(1)

    # Base64 so the script crosses into the harness as data rather than as
    # source: it is full of quotes, backticks and template literals, and
    # embedding it literally inside another script is how the test starts
    # failing on its own escaping rather than on the page.
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    harness = tmp_path_factory.mktemp("index") / "harness.html"
    harness.write_text(
        "<!doctype html><meta charset='utf-8'><body><pre id='out'></pre>\n"
        "<script>\n"
        f"const b64 = '{encoded}';\n"
        "const src = new TextDecoder().decode(\n"
        "  Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));\n"
        "try {\n"
        "  new Function(src);\n"
        "  document.getElementById('out').textContent = 'COMPILED';\n"
        "} catch (err) {\n"
        "  document.getElementById('out').textContent =\n"
        "    'THREW ' + err.name + ': ' + err.message;\n"
        "}\n"
        "</script></body>",
        encoding="utf-8",
    )
    dom = _dump_dom(harness, tmp_path_factory, _browsers())
    # Only what the harness WROTE. `--dump-dom` includes the inline script,
    # which contains both words this file looks for, so a match anywhere in
    # the document would prove nothing -- the same trap `_results` documents
    # next door.
    found = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    assert found, "the harness wrote nothing; the browser did not run it"
    return found.group(1).strip()


def test_the_deck_checks_script_compiles(compiled: str) -> None:
    """A syntax error here is a page that loads, looks right, and does nothing
    at all when anything on it is pressed."""
    if compiled.startswith("THREW"):
        pytest.fail(compiled[:300])
    assert compiled == "COMPILED"


def test_the_hand_over_button_and_its_handler_agree() -> None:
    """Read off the file rather than the DOM: the section the button lives in
    is only rendered once a run has been applied, and the point of the check is
    the two ends matching, which is a property of the source.

    The failure it guards is a rename that catches one end: a button that is
    there, looks live, and is wired to nothing."""
    html = PAGE.read_text(encoding="utf-8")

    assert 'id="to-qa"' in html
    assert '$("#to-qa")' in html
    assert html.count("handOverToDesignCheck") >= 2   # defined, and wired
    assert "/api/qa/handoff" in html
