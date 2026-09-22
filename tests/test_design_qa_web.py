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


# --------------------------------------------------------------------------- #
# The second round
#
# `apply_fixes` corrects what its own first round caused -- a box that stops
# clearing its neighbour once the type around it changed size -- and reports
# that separately, because the rule pipeline's page gives it a section of its
# own. The design page has no such section, read only the first round, and so
# made those edits and showed them nowhere. What that looks like from the
# outside is a slide visibly reflowed under a line reading "1 step(s) taken".
# --------------------------------------------------------------------------- #

def _outcome(applied: bool, slide: int, shape: str, detail: str):
    from formatting_tool.apply.applier import FixOutcome
    from formatting_tool.models import (
        Category, FixAction, Issue, Severity, Source,
    )

    issue = Issue(
        category=Category.SPACE, severity=Severity.WARNING, message=detail,
        source=Source.RULE, slide=slide, shape=shape, shape_id=7,
        fix=FixAction(op="move", shape=shape, shape_id=7,
                      left_in=1.0, top_in=1.0),
    )
    issue.id = f"{slide}:{shape}"
    return FixOutcome(issue=issue, applied=applied, detail=detail)


def test_what_the_second_round_changed_is_on_the_list(tmp_path, monkeypatch):
    """Every edit that reaches the deck has to reach the list beside the
    pictures. A designer comparing a before and an after is asking what
    accounts for the difference, and which of the applier's rounds made a
    change is this tool's bookkeeping, not theirs."""
    from formatting_tool.apply.applier import ApplyResult
    from formatting_tool.web import server

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"not really a deck")
    session = server._QaSession(
        id="s", directory=tmp_path, deck=deck,
        report=DesignQaReport(deck="deck.pptx", generated_at="now", model="t"),
    )

    result = ApplyResult(deck="deck.pptx", output=tmp_path / "fixed.pptx")
    result.applied = [_outcome(True, 5, "TextBox 26", "stepped the type down")]
    result.second_round = [
        _outcome(True, 5, "Gateway 1", "moved it clear of its neighbour"),
        _outcome(False, 5, "Gateway 2", "there was nowhere to move it to"),
    ]
    monkeypatch.setattr(server, "apply_fixes", lambda **kw: result)

    out = server._qa_propose(session, [_outcome(True, 5, "TextBox 26", "x").issue])

    details = [c.detail for c in out.applied]
    assert "stepped the type down" in details
    # The second-round change is there, and says where it came from rather
    # than reading as something the designer ticked.
    assert any("moved it clear of its neighbour" in d
               and "following on from the first round" in d for d in details)
    # And one that was attempted and refused is a refusal, not a silence.
    assert any("nowhere to move it to" in s.reason for s in out.skipped)


def test_a_slide_the_second_round_alone_touched_still_gets_an_after(
    tmp_path, monkeypatch,
):
    """The corrected renders are made for the slides that changed, and which
    slides changed is read off the applied list. A slide the first round never
    touched used to be absent from it, so the one picture that would have
    shown the reflow was never drawn."""
    from formatting_tool.apply.applier import ApplyResult
    from formatting_tool.web import server

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"not really a deck")
    session = server._QaSession(
        id="s", directory=tmp_path, deck=deck,
        report=DesignQaReport(deck="deck.pptx", generated_at="now", model="t"),
    )

    result = ApplyResult(deck="deck.pptx", output=tmp_path / "fixed.pptx")
    result.applied = [_outcome(True, 2, "Title 1", "recoloured it")]
    result.second_round = [_outcome(True, 9, "Body 3", "moved it down")]
    monkeypatch.setattr(server, "apply_fixes", lambda **kw: result)

    out = server._qa_propose(session, [_outcome(True, 2, "Title 1", "x").issue])

    assert sorted({c.slide for c in out.applied}) == [2, 9]


# --------------------------------------------------------------------------- #
# Checking the corrected deck
#
# A round corrects what the model saw. Whether the deck now reads right is a
# different question about a different file, and the only way to ask it used to
# be downloading the result and uploading it again. These are about the button
# that does it in place, and about the one thing it must never do: answer the
# new question with the old deck's pictures.
# --------------------------------------------------------------------------- #

def test_a_recheck_with_nothing_applied_is_refused(wired) -> None:
    """Re-checking a deck no round has touched is a second model call for the
    answer already on the screen, and paying for one by accident is exactly
    what a button next to a finished round could do."""
    base, session = wired
    status, body = _post(base, "/api/qa/recheck", {"session": session.id})

    assert status == 400
    assert "nothing has been applied" in json.dumps(body).lower()


def test_the_corrected_deck_becomes_the_deck_the_check_is_about(tmp_path) -> None:
    """`deck` moves to the round's output, so applying, rendering and
    downloading go on meaning "this check's deck" without any of them knowing
    which pass they are in."""
    from formatting_tool.web.server import _QaSession

    report = DesignQaReport(deck="deck.pptx", generated_at="now", model="t")
    session = _QaSession(id="s", directory=tmp_path, deck=tmp_path / "deck.pptx",
                         report=report)

    # Pass 0, round 0: the working filenames are built from the upload's name.
    assert session.name == "deck.pptx"
    assert session.fixed.name == "checked-0-0-deck.pptx"
    first = session.before_dir

    session.run = 1
    written = session.fixed
    session.passes += 1
    session.run = 0
    session.deck = tmp_path / "pass-1-deck.pptx"

    # The name does not compound into pass-2-pass-1-deck.pptx, the new round
    # cannot write over the file it is reading, and the pictures of the two
    # passes are kept apart.
    assert session.name == "deck.pptx"
    assert session.fixed != written
    assert session.fixed.name == "checked-1-0-deck.pptx"
    assert session.before_dir != first


def test_the_two_passes_do_not_share_a_directory_for_their_afters(tmp_path) -> None:
    """Pass 0 round 1 and pass 1 round 1 are different pictures of different
    decks, and one directory for the two of them is the page showing the
    wrong one."""
    from formatting_tool.web.server import _QaSession, _qa_after_dir

    session = _QaSession(id="s", directory=tmp_path, deck=tmp_path / "deck.pptx",
                         report=DesignQaReport(deck="d", generated_at="n", model="t"))
    session.run = 1
    first = _qa_after_dir(session)
    session.passes, session.run = 1, 1
    assert _qa_after_dir(session) != first


def test_a_pass_that_never_happened_is_not_claimed(tmp_path) -> None:
    """The counter starts where the wording does: the first look at the
    uploaded file is not a re-check of anything."""
    from formatting_tool.web.server import _QaSession

    session = _QaSession(id="s", directory=tmp_path, deck=tmp_path / "deck.pptx",
                         report=DesignQaReport(deck="d", generated_at="n", model="t"))
    assert session.passes == 0


# --------------------------------------------------------------------------- #
# The deck check handing over to the design check
#
# `pipeline.run` puts applying the master first and says why: putting content
# into its placeholders settles a great many findings and raises a few, so a
# report written before it describes a deck nobody will send. The design check
# is that argument one stage further on -- it reads a picture, and a picture of
# a deck that has not been restyled is a picture of a deck about to change.
#
# It is a hand-over rather than a stage because applying is the designer's
# decision. There is no restyled deck to look at until they have made one.
# --------------------------------------------------------------------------- #

def test_handing_over_before_anything_was_applied_is_refused(tmp_path) -> None:
    """A single run cannot do this: it would have to either apply everything
    without asking or check the deck it is about to change."""
    from formatting_tool.web import server as web

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"not a real deck")
    source = web._Session(
        id="deck-session", directory=tmp_path, master=tmp_path / "m.pptx",
        deck=deck, report={},
    )
    web._SESSIONS[source.id] = source
    try:
        handler = web._Handler.__new__(web._Handler)
        handler._json_body = lambda: {"session": source.id}
        with pytest.raises(web._BadRequest) as caught:
            handler._run_qa_handoff()
        assert "nothing has been applied" in str(caught.value)
    finally:
        web._SESSIONS.pop(source.id, None)


def test_the_design_check_reads_a_copy_rather_than_the_other_session(wired) -> None:
    """The two checks have different lifetimes. The deck check sweeps its
    files as its rounds move on, and a design check holding a path into that
    directory would be reading a file another page is entitled to delete."""
    base, session = wired

    # The design session owns a directory of its own, and its deck is inside
    # it -- never a path into somebody else's working directory.
    assert session.deck.parent == session.directory
    assert session.fixed.parent == session.directory


def test_a_check_can_be_opened_again_by_its_id(wired) -> None:
    """What makes the hand-over possible: the check runs server-side and what
    crosses to the browser is an id. It is also what makes a reload survivable,
    and a design check is a render of every slide plus a model call for each
    one -- losing that to a refresh loses all of it."""
    base, session = wired

    session.payload = {"session": session.id, "report": {"deck": "x"}, "pass": 0}
    status, _ctype, raw = _get(base, f"/api/qa/session/{session.id}")
    assert status == 200
    assert json.loads(raw)["report"]["deck"] == "x"


def test_a_check_with_no_answer_yet_is_a_404_rather_than_an_empty_page(wired) -> None:
    base, session = wired
    session.payload = {}
    status, _ctype, raw = _get(base, f"/api/qa/session/{session.id}")
    assert status == 404
    assert "no answer" in json.loads(raw)["error"]


def test_where_the_deck_came_from_travels_with_the_report(tmp_path) -> None:
    """"Before" meaning another pipeline's output is exactly the sort of thing
    a page must not leave somebody to infer from a filename."""
    from formatting_tool.web.server import _QaSession, _qa_check_payload

    session = _QaSession(
        id="s", directory=tmp_path, deck=tmp_path / "fixed-deck.pptx",
        report=DesignQaReport(deck="fixed-deck.pptx", generated_at="n", model="t"),
        handed_over="deck.pptx",
    )
    payload = _qa_check_payload(session, {}, [], 1.0)

    assert payload["handed_over"] == "deck.pptx"
    assert payload["pass"] == 0


def test_an_uploaded_check_says_it_came_from_nowhere(tmp_path) -> None:
    """The same field, empty, rather than absent: the page reads it on every
    render and a missing key would be a different bug on each route."""
    from formatting_tool.web.server import _QaSession, _qa_check_payload

    session = _QaSession(
        id="s", directory=tmp_path, deck=tmp_path / "deck.pptx",
        report=DesignQaReport(deck="deck.pptx", generated_at="n", model="t"),
    )
    assert _qa_check_payload(session, {}, [], 1.0)["handed_over"] == ""


# --------------------------------------------------------------------------- #
# Serving the pictures
#
# The page asks for a rendered slide by session, side and number, and the
# server works out the directory from the session's counters. Those counters
# grew a second dimension when the re-check landed, and a directory name that
# disagrees with the route that reads it is an <img> that 404s -- which on the
# page is not an error message but an empty frame with the marks piled in the
# corner, beside a change list saying the corrections were made.
# --------------------------------------------------------------------------- #

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001" "0d0a2db4" "0000000049454e44ae426082"
)


def _write_render(directory, number: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{number}.png").write_bytes(_PNG)


def test_an_after_picture_is_served_from_where_the_round_wrote_it(wired) -> None:
    """Round 1 of pass 0 writes its pictures somewhere, and the route the page
    asks on has to read that same somewhere. They are computed in two places
    and only agree by construction."""
    from formatting_tool.web.server import _qa_after_dir

    base, session = wired
    session.run = 1
    _write_render(_qa_after_dir(session), 3)

    status, ctype, body = _get(
        base, f"/api/qa/preview/{session.id}/after/3"
        f"?pass={session.passes}&run={session.run}")
    assert status == 200, body
    assert ctype == "image/png"


def test_an_after_picture_survives_a_pass(wired) -> None:
    """The counters are two-dimensional now: pass 0 round 1 and pass 1 round 1
    are different pictures of different decks."""
    from formatting_tool.web.server import _qa_after_dir

    base, session = wired
    session.run = 1
    _write_render(_qa_after_dir(session), 3)
    session.passes, session.run = 1, 1
    _write_render(_qa_after_dir(session), 3)

    status, _ctype, _body = _get(
        base, f"/api/qa/preview/{session.id}/after/3?pass=1&run=1")
    assert status == 200


def test_the_sweep_keeps_the_round_that_is_running(wired) -> None:
    """THE BUG THIS EXISTS FOR. The sweep drops the directories of rounds that
    are over, and it recognised them by name -- a name that gained the pass
    number when the re-check landed. Comparing the new name against the old
    one made every directory look finished, including the one the round about
    to render was going to write into."""
    from formatting_tool.web.server import _qa_after_dir, _qa_sweep

    base, session = wired
    session.run = 2
    current = _qa_after_dir(session)
    _write_render(current, 3)
    session.run = 1
    stale = _qa_after_dir(session)
    _write_render(stale, 3)
    session.run = 2

    _qa_sweep(session)

    assert (current / "3.png").is_file(), "the running round's pictures went"
    assert not stale.is_dir(), "a finished round's pictures were kept"


def test_the_sweep_finds_the_working_decks_after_a_pass(wired, tmp_path) -> None:
    """The working filenames are built from the UPLOAD's name, which `deck`
    stops being after a re-check. Globbing on `deck.name` therefore matched
    nothing from the second pass on, and every round's output stayed on disk."""
    from formatting_tool.web.server import _qa_sweep

    base, session = wired
    session.passes, session.run = 1, 1
    session.deck = session.directory / f"pass-1-{session.name}"
    session.deck.write_bytes(b"the baseline this pass reads")

    stale = session.directory / f"checked-0-1-{session.name}"
    stale.write_bytes(b"the round before this one")
    session.fixed.write_bytes(b"this round")

    _qa_sweep(session)

    assert not stale.is_file(), "the finished round's deck was left on disk"
    assert session.fixed.is_file(), "this round's deck was swept"
    assert session.deck.is_file(), "the baseline this pass reads was swept"


# --------------------------------------------------------------------------- #
# Every picture the server promises, fetched
#
# The page does not check that a URL it was handed resolves; it puts it in an
# <img> and moves on. A 404 there is not an error message -- `.frame img` has
# no aspect ratio, so a missing picture collapses to the height of its alt text
# and the marks that should be over the slide pile into the corner. What is on
# screen is an empty white strip beside a list saying the corrections were made.
#
# So: run a round for real, take every URL the answer contains, and fetch it.
# --------------------------------------------------------------------------- #

def _png(directory, number: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{number}.png").write_bytes(_PNG)


def _urls_in(payload) -> list[str]:
    """Every preview URL in an answer, whichever route produced it."""
    out = []
    for row in payload.get("slides") or []:
        for side in ("before", "after"):
            if row.get(side):
                out.append(row[side])
    return out


def test_every_picture_an_apply_promises_can_be_fetched(wired, monkeypatch) -> None:
    """THE FAILURE THIS CATCHES. The directory a round renders into and the
    directory the preview route reads from are computed in two places, and they
    agree only by construction. When they stopped agreeing the page did not
    complain -- it showed an empty frame with the marks in the corner, beside a
    change list saying the work had been done."""
    from formatting_tool.apply.qafix import Change, QaFixResult
    from formatting_tool.web import server as web

    base, session = wired

    # A round that corrects slide 1 and renders it, without PowerPoint or the
    # model: the question here is the plumbing between the two directories.
    def fake_propose(sess, issues):
        sess.fixed.write_bytes(b"the corrected deck")
        return QaFixResult(output=sess.fixed, applied=[Change(
            op="grow", slide=1, shape_id=11, shape="Shape s1",
            detail="stepped the type up")])

    def fake_render(deck, directory, slides=None):
        for n in (slides or [1]):
            _png(directory, n)
        return {n: f"{n}.png" for n in (slides or [1])}

    monkeypatch.setattr(web, "_qa_propose", fake_propose)
    monkeypatch.setattr(web, "_qa_render", fake_render)
    monkeypatch.setattr(web, "add_comments", lambda *a, **k: 0)
    _png(session.before_dir, 1)

    status, payload = _post(base, "/api/qa/apply",
                            {"session": session.id, "fix": ["1:s1"], "notes": False})
    assert status == 200, payload

    urls = _urls_in(payload)
    assert any("/after/" in u for u in urls), "the round rendered nothing to show"
    for url in urls:
        code, ctype, _body = _get(base, url)
        assert code == 200, f"{url} came back {code}"
        assert ctype == "image/png", f"{url} served {ctype}"


def test_the_corrected_deck_is_still_downloadable_after_a_recheck(wired) -> None:
    """A re-check promotes the round's output to the deck the next pass reads,
    and that pass has written nothing yet. The file is right there, and asking
    for it used to answer "nothing has been applied yet"."""
    base, session = wired
    session.run = 1
    session.fixed.write_bytes(b"pass 0's corrected deck")

    status, _ctype, body = _get(base, f"/api/qa/download/{session.id}")
    assert status == 200 and body == b"pass 0's corrected deck"

    # The re-check: that output becomes the baseline, and the round counter
    # starts again with nothing written under it.
    session.passes, session.run = 1, 0
    session.deck = session.directory / f"pass-1-{session.name}"
    session.deck.write_bytes(b"pass 0's corrected deck")
    assert not session.fixed.exists()

    status, _ctype, body = _get(base, f"/api/qa/download/{session.id}")
    assert status == 200, body
    assert body == b"pass 0's corrected deck"


def test_an_untouched_upload_is_never_offered_as_a_corrected_deck(wired) -> None:
    """The fallback is only past pass 0. On the first pass `deck` is still the
    file they gave us, and handing that back labelled as corrected is a lie
    about a deck they would then send."""
    base, session = wired
    assert session.passes == 0 and not session.fixed.exists()

    status, _ctype, body = _get(base, f"/api/qa/download/{session.id}")
    assert status == 404
    assert "nothing has been applied" in json.loads(body)["error"]


def test_a_deck_wide_correction_does_not_ask_for_slide_zero(wired, monkeypatch) -> None:
    """A correction about the deck rather than about one slide reports slide 0,
    because `_qa_propose` has nothing else to put there. Passed on as a slide
    to render, 0 costs twice: the page gets a preview URL that 404s, and
    `render_deck` reads a request naming no valid slide as a request for the
    WHOLE deck -- on a ninety-slide deck, a long render nobody asked for and
    eighty-nine pairs of identical pictures."""
    from formatting_tool.apply.qafix import Change, QaFixResult
    from formatting_tool.web import server as web

    base, session = wired
    asked = []

    def fake_propose(sess, issues):
        sess.fixed.write_bytes(b"the corrected deck")
        return QaFixResult(output=sess.fixed, applied=[
            Change(op="recolor_text", slide=0, shape_id=0, shape="",
                   detail="a finding about the deck", task_id="deck:0"),
            Change(op="grow", slide=1, shape_id=11, shape="Shape s1",
                   detail="stepped the type up"),
        ])

    def fake_render(deck, directory, slides=None):
        asked.append(slides)
        for n in (slides or []):
            _png(directory, n)
        return {n: f"{n}.png" for n in (slides or [])}

    monkeypatch.setattr(web, "_qa_propose", fake_propose)
    monkeypatch.setattr(web, "_qa_render", fake_render)
    monkeypatch.setattr(web, "add_comments", lambda *a, **k: 0)
    _png(session.before_dir, 1)

    status, payload = _post(base, "/api/qa/apply",
                            {"session": session.id, "fix": ["1:s1"], "notes": False})
    assert status == 200, payload
    assert asked == [[1]], f"the renderer was asked for {asked}"
    assert [row["slide"] for row in payload["slides"]] == [1]


def test_the_pictures_of_a_round_outlive_the_answer_that_named_them(
    wired, monkeypatch,
) -> None:
    """A designer reads the page, then presses Re-render on a slide. Nothing
    between those two moments may remove what the first answer pointed at."""
    from formatting_tool.web import server as web

    base, session = wired
    session.run = 1
    _png(web._qa_after_dir(session), 1)
    _png(session.before_dir, 1)

    def fake_render(deck, directory, slides=None):
        for n in (slides or [1]):
            _png(directory, n)
        return {n: f"{n}.png" for n in (slides or [1])}

    monkeypatch.setattr(web, "_qa_render", fake_render)
    session.fixed.write_bytes(b"the corrected deck")

    status, payload = _post(base, "/api/qa/render",
                            {"session": session.id, "slides": [1], "force": True})
    assert status == 200, payload
    for url in _urls_in(payload):
        code, _ctype, _body = _get(base, url)
        assert code == 200, f"{url} came back {code}"


# --------------------------------------------------------------------------- #
# Undo
#
# A REPLAY, NOT A REVERSE. None of the corrections this page makes has an
# inverse -- a type step rolled back for spilling and a move a guard refused
# are not deltas anybody can subtract -- so the round is run again from the
# same starting file without the row named, and what comes back is the deck
# that round would have produced had the row never been ticked.
# --------------------------------------------------------------------------- #

def test_nothing_can_be_undone_before_anything_is_applied(wired) -> None:
    base, _session = wired
    code, payload = _post(base, "/api/qa/undo",
                          {"session": "qa-test-session", "undo": ["1:s1"]})

    assert code == 400
    assert "nothing has been applied" in payload["error"]


def test_an_undo_has_to_name_something(wired) -> None:
    base, session = wired
    session.applied_once = True
    code, payload = _post(base, "/api/qa/undo", {"session": "qa-test-session"})

    assert code == 400
    assert "no change was named" in payload["error"]


def test_a_row_taken_back_is_replayed_without_it(wired, monkeypatch) -> None:
    """The round is re-run with the ticks as they now stand, so the server is
    asked for the deck it would have produced rather than for a reversal."""
    from formatting_tool.web import server as web

    base, session = wired
    session.applied_once = True
    session.ticked = ["1:s1", "1:s2"]
    session.notes = False

    seen: list[list] = []

    def _watch(self, sess, chosen, file_the_rest, strict=True):
        seen.append(list(chosen))
        return {"session": sess.id, "undone": list(sess.undone), "applied": []}

    monkeypatch.setattr(web._Handler, "_qa_apply", _watch, raising=True)

    code, payload = _post(base, "/api/qa/undo",
                          {"session": "qa-test-session", "undo": ["1:s1"]})
    assert code == 200
    assert seen[-1] == ["1:s2"]                  # replayed without the row
    assert payload["undone"] == ["1:s1"]
    assert session.ticked == ["1:s1", "1:s2"]    # the tick itself is untouched

    # Cumulative, and reversible from the same list.
    code, payload = _post(base, "/api/qa/undo",
                          {"session": "qa-test-session", "undo": ["1:s2"]})
    assert seen[-1] == []                        # and an empty round is legal
    assert payload["undone"] == ["1:s1", "1:s2"]

    code, payload = _post(base, "/api/qa/undo",
                          {"session": "qa-test-session", "redo": ["1:s1"]})
    assert seen[-1] == ["1:s1"]
    assert payload["undone"] == ["1:s2"]


def test_a_fresh_apply_forgets_what_was_undone(wired, monkeypatch) -> None:
    """A new set of ticks is a new decision about the whole deck. Carrying the
    old undo list into it would silently drop a fix just asked for."""
    from formatting_tool.web import server as web

    base, session = wired
    session.applied_once = True
    session.ticked = ["1:s1"]
    session.undone = ["1:s1"]

    monkeypatch.setattr(
        web._Handler, "_qa_apply",
        lambda self, sess, chosen, notes, strict=True: {"chosen": list(chosen)},
        raising=True,
    )
    code, payload = _post(base, "/api/qa/apply",
                          {"session": "qa-test-session", "fix": ["1:s1"],
                           "notes": False})

    assert code == 200 and payload["chosen"] == ["1:s1"]
    assert session.undone == []
