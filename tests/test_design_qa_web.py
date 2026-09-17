"""The design check over the wire: the page, its assets, and what it refuses.

The model call and the COM half are covered by their own tests and cannot run
here -- one needs a key, the other needs PowerPoint. What is left is the part
the page depends on and nothing else tests: that there is a second page at all,
that the assets both pages share are served, that the static route cannot be
talked into serving something else, and that an apply naming things the check
cannot do is refused with a sentence rather than half-done.

The server is started on an ephemeral port with a session put straight into its
table, the same way `test_undo_web` does it.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from formatting_tool.ai.designqa import ShapeVerdict, SlideIssue, SlideReview
from formatting_tool.designqa import DesignQaReport


def _verdict(ref, action, shape_id, status="issue"):
    return ShapeVerdict(
        slide=1, ref=ref, shape=f"Shape {ref}", shape_id=shape_id, role="body",
        status=status, issue="too_small" if action != "none" else "",
        action=action, note="a note", task=f"do something about {ref}",
        box=(0.1, 0.1, 0.4, 0.2),
    )


@pytest.fixture()
def wired(tmp_path: Path):
    """A running server with one design check on it."""
    from formatting_tool.web import server as web

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"not a real deck; nothing here opens it")

    session = web._QaSession(
        id="qa-test-session",
        directory=tmp_path,
        deck=deck,
        report=DesignQaReport(
            deck="deck.pptx", generated_at="now", model="test",
            reviews=[SlideReview(slide=1, reviewed=True, verdicts=[
                _verdict("s1", "grow", 11),
                _verdict("s2", "none", 12),
            ], slide_issues=[SlideIssue(
                note="the right half of the slide is empty",
                task="run the cards across the full width",
            )])],
        ),
    )
    web._QA_SESSIONS[session.id] = session

    server = web._Server(("127.0.0.1", 0), web._Handler, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, session
    finally:
        server.shutdown()
        server.server_close()
        web._QA_SESSIONS.pop(session.id, None)


def _get(base: str, route: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(f"{base}{route}") as response:
            return response.status, response.headers["Content-Type"], response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers["Content-Type"], error.read()


def _post(base: str, route: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{base}{route}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_the_design_check_has_a_page_of_its_own(wired) -> None:
    base, _session = wired
    code, kind, body = _get(base, "/qa")

    assert code == 200
    assert kind.startswith("text/html")
    assert b"Design check" in body


def test_the_two_pages_read_their_colours_from_one_file(wired) -> None:
    """A colour defined twice is a colour that ends up being two colours."""
    base, _session = wired
    code, kind, body = _get(base, "/static/tokens.css")

    assert code == 200
    assert kind.startswith("text/css")
    assert b"--blocker" in body


def test_the_static_route_serves_the_page_and_nothing_else(wired) -> None:
    """The name arrives from a URL. A local tool that joins one onto a path is
    a local tool that serves whatever is asked for."""
    base, _session = wired

    for route in (
        "/static/../server.py",
        "/static/tokens.css/../../../pyproject.toml",
        "/static/.env",
        "/static/nothing.css",
    ):
        code, _kind, _body = _get(base, route)
        assert code == 404, route


def test_an_expired_check_is_a_sentence_rather_than_a_traceback(wired) -> None:
    base, _session = wired
    code, payload = _post(base, "/api/qa/apply", {"session": "gone", "fix": ["1:s1"]})

    assert code == 400
    assert "expired" in payload["error"]


def test_asking_for_nothing_at_all_is_refused(wired) -> None:
    """Nothing ticked and the tasks not to be written in either: the button
    would do nothing, and a request that does nothing should say so."""
    base, session = wired
    code, payload = _post(
        base, "/api/qa/apply", {"session": session.id, "fix": [], "notes": False},
    )

    assert code == 400
    assert "nothing was ticked" in payload["error"]


def test_ticking_only_tasks_nobody_can_apply_says_where_they_go(wired) -> None:
    """Everything on the page can be ticked in the sense that it is on the
    page. Only some of it is a correction, and the difference has to be said
    -- along with what happens to the rest."""
    base, session = wired
    code, payload = _post(
        base, "/api/qa/apply",
        {"session": session.id, "fix": ["1:s2"], "notes": False},
    )

    assert code == 400
    assert "tasks for a designer" in payload["error"]


def test_a_round_that_corrects_nothing_still_files_the_tasks(wired) -> None:
    """The deck here is not a real .pptx, so PowerPoint cannot write comments
    into it. What must survive that is the list: the page is told what is
    outstanding whether or not the deck could be annotated."""
    base, session = wired
    code, payload = _post(
        base, "/api/qa/apply", {"session": session.id, "fix": [], "notes": True},
    )

    assert code == 200
    assert payload["applied"] == []
    assert payload["comments_asked"] is True
    # Every task is still to do, and each one says what to do rather than only
    # what is wrong.
    assert [t["id"] for t in payload["outstanding"]] == ["1:s1", "1:s2", "slide:1:0"]
    assert all(task["what"] for task in payload["outstanding"])


def test_a_re_render_names_a_slide_or_it_is_refused(wired) -> None:
    base, session = wired
    code, payload = _post(base, "/api/qa/render", {"session": session.id, "slides": []})

    assert code == 400
    assert "no slide was named" in payload["error"]


def test_a_re_render_of_a_deck_with_no_corrected_copy_offers_no_after(wired) -> None:
    """Before an apply there is one picture, not two. The page draws the pair
    only where a corrected deck exists to be the other half of it."""
    base, session = wired
    code, payload = _post(
        base, "/api/qa/render", {"session": session.id, "slides": [1], "force": True}
    )

    assert code == 200
    assert [row["slide"] for row in payload["slides"]] == [1]
    assert payload["slides"][0]["after"] is None


def test_nothing_has_been_applied_so_there_is_nothing_to_download(wired) -> None:
    base, session = wired
    code, _kind, _body = _get(base, f"/api/qa/download/{session.id}")
    assert code == 404


def test_a_render_that_was_never_made_is_a_404_not_a_path(wired) -> None:
    base, session = wired
    for route in (
        f"/api/qa/preview/{session.id}/before/1",
        f"/api/qa/preview/{session.id}/before/..%2f..%2fserver.py",
        f"/api/qa/preview/{session.id}/sideways/1",
    ):
        code, _kind, _body = _get(base, route)
        assert code == 404, route


def test_the_page_is_told_whether_this_host_can_render(wired) -> None:
    """The deck check degrades without a renderer; this one cannot run at all,
    and a designer should learn that before uploading 25MB."""
    base, _session = wired
    code, _kind, body = _get(base, "/api/context")

    assert code == 200
    assert "renderer" in json.loads(body)
