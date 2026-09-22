"""A local, single-user web UI over the validation pipeline.

    formatting-tool ui

Stdlib only, on purpose. The tool has four dependencies and a browser page is
not worth a fifth. The server calls `pipeline.run` in-process rather than
shelling out to the CLI, so the report the page renders is the same object the
CLI writes, and a traceback here is the real one.

This is not hardened and is not meant to be. It binds to the loopback
interface, it trusts what it is sent, and it keeps no state between requests.
Run it on your own machine, against your own decks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import secrets
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import copyfile, rmtree
from typing import Any, Optional

from .. import __version__
from ..ai.client import AIConfig, AIValidationError, DEFAULT_EFFORT, DEFAULT_MODEL
from ..ai.payload import DEFAULT_BATCH_SIZE
from ..apply import ApplyError, apply_fixes, fixable
from ..apply.notes import add_comments
from ..apply.qafix import apply_steps
from ..designqa import (
    DesignQaReport,
    comments_for,
    issues_for,
    outstanding,
    review_deck,
    steps_for,
)
from ..extract import DeckReadError, read_deck
from ..guidelines import GuidelinesError
from ..pipeline import RunConfig, run
from ..render import DESIGN_QA_SIZE, available_renderer, render_deck
from ..report.reader import _issue as _issue_from_dict
from ..workdir import WORKDIR_ENV as _WORKDIR_ENV, workroot
from ..rules import build_default_rules, describe_rules

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

# The page prefills this rather than DEFAULT_MODEL. `gemini-2.5-pro` is retired
# for new API keys and answers 404, which the pipeline swallows into a
# rule-only report; prefilling a model that works keeps the first AI run in the
# UI from looking like a silent success. The CLI default is left alone.
SUGGESTED_MODEL = "gemini-3.1-pro-preview"

# One run at a time. Log capture attaches a handler to a shared logger, so
# concurrent runs would hand each other's lines to the wrong page.
_RUN_LOCK = threading.Lock()


class _BadRequest(Exception):
    """Something wrong with what the page sent, not with the pipeline."""


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

# How long an uploaded deck is kept after its run. Ticking findings and
# applying them happens minutes after the check, so the deck has to outlive
# the request that produced the report; it does not have to outlive the day.
SESSION_TTL_S = 4 * 3600
MAX_SESSIONS = 8


@dataclass
class _Session:
    """One upload, kept so its findings can be applied to it later.

    The page ticks findings against a report and then asks for them to be
    applied. Re-uploading a 25 MB deck to do that would be absurd, and
    re-deriving it would risk applying findings to a different file, so the
    deck stays on disk with the report that describes it.
    """

    id: str
    directory: Path
    master: Path
    deck: Path
    report: dict[str, Any]
    # The values the run measured against, taken off the report rather than
    # re-derived. Applying happens minutes later, and re-reading the master
    # then meant reading a file in a temp directory that may no longer be
    # there -- and, where it is, may not be the file the report describes.
    spec: Any = None
    # The model's reading of which off-palette colours mean something, carried
    # as objects rather than through the report dict for the reason `spec` is:
    # an undo replays the whole run, and re-deriving this each time would let
    # a take-back change a verdict nobody revisited.
    color_intents: list = field(default_factory=list)
    created: float = field(default_factory=time.time)
    # The last apply, kept so an undo can replay it without the page having to
    # send the whole tick list back. `selection` is None for "everything
    # fixable", the same as `apply_fixes` reads it, and `undone` accumulates:
    # the designer takes back one change, looks at the result, takes back
    # another, and each run is the original deck minus everything on this list.
    selection: Optional[list[str]] = None
    rebuild: bool = False
    undone: list[str] = field(default_factory=list)
    applied_once: bool = False
    # How many times the corrected deck has been written. Every run produces a
    # different file, so its renders go in a directory of their own and are
    # served from URLs carrying the number.
    #
    # THE ALTERNATIVE DOES NOT WORK, which is what this replaces: deleting the
    # old images and rendering into the same place again. Deletion is allowed
    # to fail -- on Windows a file that anything still holds open cannot be
    # removed, and the removal was deliberately best-effort so it could not
    # fail a request -- and a directory that did not empty reads as a cache
    # that is already full. The page then shows the previous run's pictures
    # beside the new deck, which is the worst thing it can do: the undo was
    # applied, the download was right, and the evidence on screen said it had
    # not happened.
    #
    # A number in the path cannot half-work. A render either exists for this
    # run or is made; nothing older is reachable, by the page or by anything
    # caching on its behalf.
    run: int = 0
    # How the check was run, so a rebuild minutes later can ask the model the
    # one question it is good at. Kept rather than re-derived: the page sends
    # nothing about the AI when it applies, and guessing at a model and a key
    # would be inventing the designer's settings for them.
    ai: Any = None
    use_ai: bool = False
    # Which layout each slide belongs on, as read off its render. Computed at
    # most once per session: it costs a model call a slide, and an undo is a
    # whole run of the fixes, so charging for it again on every take-back would
    # make the cheapest correction the most expensive thing on the page.
    layout_picks: Optional[list] = None

    @property
    def fixed(self) -> Path:
        return self.directory / f"fixed-{self.deck.name}"

    def cleanup(self) -> None:
        rmtree(self.directory, ignore_errors=True)


_SESSIONS: dict[str, _Session] = {}

# The design check keeps its own table. Same shape, same rules, different
# lifetime: a deck can be checked on one page and QA'd on the other, and one
# table would have the second check evicting the first one's deck out from
# under the fixes somebody is halfway through ticking.
_QA_SESSIONS: dict[str, "_QaSession"] = {}
_SESSION_LOCK = threading.Lock()


def _remember(session: Any, table: Optional[dict] = None) -> None:
    table = _SESSIONS if table is None else table
    with _SESSION_LOCK:
        table[session.id] = session
        stale = [
            key
            for key, value in table.items()
            if time.time() - value.created > SESSION_TTL_S
        ]
        # Oldest first, so a burst of checks does not fill the disk with decks
        # nobody is going to apply anything to.
        while len(table) - len(stale) > MAX_SESSIONS:
            oldest = min(
                (k for k in table if k not in stale),
                key=lambda k: table[k].created,
            )
            stale.append(oldest)
        for key in stale:
            table.pop(key).cleanup()


def _session(session_id: str, table: Optional[dict] = None) -> Any:
    table = _SESSIONS if table is None else table
    with _SESSION_LOCK:
        found = table.get(session_id)
    if found is None:
        raise _BadRequest(
            "that check has expired; run it again before applying fixes"
        )
    return found


@dataclass
class _QaSession:
    """One design check, kept so its steps can be applied to the same deck.

    Smaller than `_Session` because the design check has less to remember:
    there is no master and no brand file. What it does have to keep is the
    report -- the refs the page ticks are the refs that review handed out, and
    re-deriving them would mean asking the model again and getting a different
    reading of the same slides.
    """

    id: str
    directory: Path
    deck: Path
    report: DesignQaReport
    created: float = field(default_factory=time.time)
    # How many times a corrected deck has been written. Every round writes a
    # new file under a new name and renders into a directory carrying the
    # number, for the reason `_Session.run` gives at length: a picture served
    # from an unchanged URL is how a page shows the previous deck beside the
    # new one and says the fix did not happen.
    run: int = 0
    # How many times the check has been asked again ON ITS OWN OUTPUT. A round
    # corrects what the model saw; looking at the corrected deck is a second
    # question, and the answer to it is a different report about a different
    # file. `deck` moves to that file, so everything downstream -- applying,
    # rendering, downloading -- goes on meaning "the deck this check is about"
    # without knowing how many passes it took to get here.
    passes: int = 0
    # The uploaded filename, kept because `deck` stops being the upload after
    # the first re-check and the working filenames are built from this. Without
    # it they compound: pass-2-pass-1-deck.pptx.
    name: str = ""
    # The last check's answer, kept so the page can be opened against a session
    # that already exists rather than only by uploading a deck. That is what
    # lets the deck check hand its restyled file over: the design check runs
    # server-side, the page opens on the session id, and nothing goes up the
    # wire twice. It also means a reload does not throw the report away.
    payload: dict[str, Any] = field(default_factory=dict)
    # Where this deck came from, when it was not uploaded here. The page says
    # so, because "before" meaning the output of another pipeline is exactly
    # the sort of thing a page must not leave a designer to infer.
    handed_over: str = ""
    # The master this deck was restyled onto, when it came from the deck check.
    # None for an upload, and then the report derives a reference from the deck
    # itself and the brand rules stay off the list. See `designqa.review_deck`.
    spec: Any = None
    # THE LAST ROUND, KEPT SO ONE CHANGE OUT OF IT CAN BE TAKEN BACK.
    #
    # Undo here is a REPLAY, exactly as it is on the deck check: every round
    # already starts from the deck this session is about and applies the ticks
    # as they stand, so a round run again without one of them produces the file
    # that round would have produced had it never been ticked. Nothing is
    # reversed, which is the only way this could be right -- a type step that
    # was rolled back for spilling and a move a guard refused have no inverse
    # to apply, and a deck edited twice is not the deck edited once.
    #
    # `ticked` is what the last round was asked for, `undone` accumulates what
    # has since been taken back, and `notes` is whether that round also wrote
    # the rest into the deck as comments -- a replay that quietly dropped them
    # would take a designer's list away as the price of undoing one colour.
    ticked: list[str] = field(default_factory=list)
    undone: list[str] = field(default_factory=list)
    notes: bool = True
    applied_once: bool = False

    def __post_init__(self) -> None:
        self.name = self.name or self.deck.name

    @property
    def fixed(self) -> Path:
        return self.directory / f"checked-{self.passes}-{self.run}-{self.name}"

    @property
    def before_dir(self) -> Path:
        """Where this pass's renders of the deck AS IT NOW STANDS live.

        Per pass, because a re-check reads a different file and the pictures
        of the last one are not evidence about this one. Serving them from a
        shared directory is how a page shows the deck before the round that
        already happened.
        """
        return self.directory / "before" / str(self.passes)

    def cleanup(self) -> None:
        rmtree(self.directory, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Multipart, because `cgi` is gone in 3.13
# --------------------------------------------------------------------------- #

@dataclass
class _Part:
    name: str
    filename: Optional[str]
    data: bytes


_BOUNDARY = re.compile(r'boundary=(?:"([^"]+)"|([^\s;]+))', re.IGNORECASE)
_DISPOSITION = re.compile(r'(\w+)="([^"]*)"')


def _boundary_of(content_type: str) -> bytes:
    match = _BOUNDARY.search(content_type or "")
    if not match:
        raise _BadRequest("expected a multipart/form-data body")
    return (match.group(1) or match.group(2)).encode("ascii")


def parse_multipart(body: bytes, boundary: bytes) -> list[_Part]:
    """Split a multipart body into its parts.

    Splitting on the delimiter is safe by definition: a boundary that appears
    in the content is not a legal boundary.
    """
    parts: list[_Part] = []
    for chunk in body.split(b"--" + boundary):
        chunk = chunk.lstrip(b"\r\n")
        if not chunk or chunk.startswith(b"--"):
            continue
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        name, filename = _disposition_of(head)
        if name:
            parts.append(_Part(name=name, filename=filename, data=data))
    return parts


def _disposition_of(head: bytes) -> tuple[Optional[str], Optional[str]]:
    for line in head.decode("utf-8", "replace").splitlines():
        if not line.lower().startswith("content-disposition:"):
            continue
        found = dict(_DISPOSITION.findall(line))
        return found.get("name"), found.get("filename") or None
    return None, None


# --------------------------------------------------------------------------- #
# Log capture
# --------------------------------------------------------------------------- #

class _Collector(logging.Handler):
    """Collects the pipeline's own log lines so the page can show them.

    Worth the trouble because two things are only ever reported through the
    log: a deck built at the wrong slide size, and an AI layer that failed and
    left a rule-only report behind. Neither appears in the report object, and
    the run still exits 0.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {
                "level": record.levelname.lower(),
                "logger": record.name,
                "message": record.getMessage(),
            }
        )


class _capture:
    """Attach a collector to the package logger for the length of a run."""

    def __enter__(self) -> _Collector:
        self.collector = _Collector()
        self.logger = logging.getLogger("formatting_tool")
        self.previous = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.collector)
        return self.collector

    def __exit__(self, *exc_info) -> bool:
        self.logger.removeHandler(self.collector)
        self.logger.setLevel(self.previous)
        return False


# --------------------------------------------------------------------------- #
# Request handling
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    server_version = f"formatting-tool/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    # -- routes ------------------------------------------------------------ #

    def do_GET(self) -> None:
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            self._send_file(STATIC / "index.html", "text/html; charset=utf-8")
        elif route in ("/qa", "/qa.html"):
            self._send_file(STATIC / "qa.html", "text/html; charset=utf-8")
        elif route.startswith("/static/"):
            self._static(route)
        elif route == "/api/context":
            self._send_json(200, _context(self.server.root))
        elif route.startswith("/api/qa/preview/"):
            self._qa_preview(route)
        elif route.startswith("/api/qa/download/"):
            self._qa_download(route)
        elif route.startswith("/api/qa/session/"):
            self._qa_session(route)
        elif route.startswith("/api/preview/"):
            self._preview(route)
        elif route.startswith("/api/download/"):
            self._download(route)
        else:
            self._send_json(404, {"error": f"no route for {route}"})

    def do_POST(self) -> None:
        route = self.path.split("?", 1)[0]
        if route == "/api/validate":
            code, payload = self._guarded(self._run_pipeline)
        elif route == "/api/apply":
            code, payload = self._guarded(self._run_apply)
        elif route == "/api/undo":
            code, payload = self._guarded(self._run_undo)
        elif route == "/api/preview":
            code, payload = self._guarded(self._run_preview)
        elif route == "/api/qa/check":
            code, payload = self._guarded(self._run_qa)
        elif route == "/api/qa/handoff":
            code, payload = self._guarded(self._run_qa_handoff)
        elif route == "/api/qa/recheck":
            code, payload = self._guarded(self._run_qa_recheck)
        elif route == "/api/qa/undo":
            code, payload = self._guarded(self._run_qa_undo)
        elif route == "/api/qa/apply":
            code, payload = self._guarded(self._run_qa_apply)
        elif route == "/api/qa/render":
            code, payload = self._guarded(self._run_qa_render)
        else:
            self._send_json(404, {"error": f"no route for {route}"})
            return
        self._send_json(code, payload)

    # -- apply and preview -------------------------------------------------- #

    def _run_apply(self) -> dict[str, Any]:
        """Apply the ticked findings to the session's deck."""
        body = self._json_body()
        session = _session(str(body.get("session", "")))
        chosen = [str(i) for i in body.get("fix", [])]
        rebuild_too = bool(body.get("rebuild"))

        if not chosen and not rebuild_too:
            raise _BadRequest("nothing was ticked")

        # A fresh apply is a fresh run, so anything held back on the last one
        # is not held back on this one. The designer has just re-ticked the
        # list; taking an old undo forward would silently drop a fix they can
        # see they asked for.
        session.selection = chosen
        session.rebuild = rebuild_too
        session.undone = []
        return self._apply_session(session)

    def _run_undo(self) -> dict[str, Any]:
        """Take back one applied change, or put one back.

        The deck is written again from the ORIGINAL upload without the changes
        on the undo list, which is why this and `/api/apply` return the same
        payload: both of them are one whole run of the fixes, and the file the
        page offers for download after an undo is the file it would have
        offered had that fix never been ticked. See `apply_fixes(undone=...)`
        for why it is a replay rather than a reverse.

        Cumulative and reversible: `undo` adds ids to the list, `redo` takes
        them off, and either can name several at once.
        """
        body = self._json_body()
        session = _session(str(body.get("session", "")))
        if not session.applied_once:
            raise _BadRequest("nothing has been applied to this deck yet")

        undo = [str(i) for i in body.get("undo", [])]
        redo = {str(i) for i in body.get("redo", [])}
        if not undo and not redo:
            raise _BadRequest("no change was named to undo")

        held = [key for key in (*session.undone, *undo) if key not in redo]
        session.undone = list(dict.fromkeys(held))
        return self._apply_session(session)

    def _apply_session(self, session: _Session) -> dict[str, Any]:
        """One run of the fixes on this session, as its state now stands."""
        issues = [_issue_from_dict(e) for e in session.report.get("issues", [])]
        for issue in issues:
            if not issue.id:
                issue.id = issue.fingerprint()

        with _RUN_LOCK, _capture() as collected:
            started = time.perf_counter()
            result = apply_fixes(
                deck=session.deck,
                issues=issues,
                out=session.fixed,
                selected=session.selection,
                master=session.master if session.rebuild else None,
                spec=session.spec,
                undone=session.undone,
                layout_choices=_layout_picks(session) if session.rebuild else None,
                color_intents=session.color_intents,
            )
            elapsed = time.perf_counter() - started
        session.applied_once = True
        # A new file, so a new place for its pictures, and the old ones swept
        # up if they will go.
        session.run += 1
        _sweep(session)

        return {
            "session": session.id,
            # Colour fixes left alone because the model read the colours as
            # carrying meaning. Listed, not silent: a correction that did not
            # happen has to be as visible as one that did, or the page is a
            # list that lies by omission.
            "kept_colors": [
                {
                    "id": issue.id,
                    "slide": issue.slide,
                    "shape": issue.shape,
                    "detail": issue.message,
                    "scheme": intent.scheme,
                    "why": intent.why,
                    "confidence": intent.confidence,
                }
                for issue, intent in result.kept_colors
            ],
            # Listed apart from the rest: a removal is the one change with
            # nothing left on the slide to check it against.
            "removed": [
                {"id": _outcome_id(o), "slide": o.issue.slide, "detail": o.detail,
                 "box_before": o.box_before, "box_after": o.box_after}
                for o in result.removed
            ],
            # The second pass. The report the page ticked describes the deck
            # as it arrived; this describes the one it is about to download.
            "recheck": {
                "ran": result.rechecked,
                "total": len(result.recheck),
                # What the second round corrected on the written deck, and
                # what the deck measures now that it has.
                "second_round": [
                    # Shape and rule as well as the change itself. The page
                    # groups changes by what kind of rule made them and names
                    # the shape beside each one, and a second-pass change that
                    # carried neither read as an anonymous line in "Other" --
                    # which is exactly the change a designer is least likely to
                    # recognise, having never ticked it.
                    {"id": _outcome_id(o), "detail": o.detail,
                     "applied": o.applied, "slide": o.issue.slide,
                     "shape": o.issue.shape, "rule_id": o.issue.rule_id,
                     "box_before": o.box_before, "box_after": o.box_after}
                    for o in result.second_round
                ],
                "settled": len(result.settled),
                "introduced": [
                    {
                        "id": i.id or i.fingerprint(),
                        "rule_id": i.rule_id,
                        "severity": i.severity.value,
                        "slide": i.slide,
                        "shape": i.shape,
                        "message": i.message,
                    }
                    for i in result.introduced
                ],
            },
            "applied": [
                {"id": _outcome_id(o), "detail": o.detail, "slide": o.issue.slide,
                 "shape": o.issue.shape, "rule_id": o.issue.rule_id,
                 # Where to draw the change on the rendered slides. See
                 # FixOutcome.
                 "box_before": o.box_before, "box_after": o.box_after}
                for o in result.applied
            ],
            "skipped": [
                {"id": _outcome_id(o), "detail": o.detail, "slide": o.issue.slide}
                for o in result.skipped
            ],
            # What is being held back, and enough about each one to offer it
            # back. The detail cannot come from this run -- the change was
            # never made on it -- so it comes from the finding itself, which is
            # what the page was showing before it was applied anyway.
            "undone": [
                {
                    "id": key,
                    "slide": _undone_field(key, issues, "slide"),
                    "shape": _undone_field(key, issues, "shape"),
                    "rule_id": _undone_field(key, issues, "rule_id"),
                    "message": _undone_field(key, issues, "message"),
                    "unknown": key in result.undone_unknown,
                }
                for key in result.undone
            ],
            "download": f"/api/download/{session.id}",
            "rebuilt": result.rebuilt is not None,
            "logs": collected.records,
            "elapsed_s": round(elapsed, 2),
        }

    def _run_preview(self) -> dict[str, Any]:
        """Render the deck before and, if it exists, after the fixes.

        Rendering is what makes a fix reviewable. "moved 0.65in back onto the
        canvas" is a claim; two pictures are the evidence, and a designer can
        reject the result before it reaches a client.
        """
        body = self._json_body()
        session = _session(str(body.get("session", "")))
        slides = [int(n) for n in body.get("slides", [])] or None
        # Slides to render again whatever is cached for them. The cache is
        # right nearly always -- the upload never changes, and a run that
        # rewrites the corrected deck drops the after images itself -- but
        # "nearly always" is not something a designer can check from the
        # outside, and PowerPoint does occasionally export a slide mid-repaint.
        # So there is a way to say: render this one again, now.
        _forget(session, [int(n) for n in body.get("force", [])])

        before = _render_into(session.deck, session.directory / "before", slides)
        after: dict[int, str] = {}
        if session.fixed.exists():
            after = _render_into(session.fixed, _after_dir(session), slides)

        numbers = sorted(set(before) | set(after))
        if slides:
            numbers = [n for n in numbers if n in slides]

        return {
            "session": session.id,
            "renderer_ok": bool(before),
            # The rendered shape of a slide, so the page can reserve the right
            # space before the images arrive. Without it the overlay boxes are
            # positioned against a collapsed <img> and scatter outside it.
            # Full paths, because the dicts hold bare filenames: a bare name
            # is read relative to the working directory, which is not where
            # the renders are, so this answered None on every run and the page
            # positioned its overlays against a collapsed <img>.
            "aspect": _aspect_of(
                {n: str(session.directory / "before" / name)
                 for n, name in before.items()},
                {n: str(_after_dir(session) / name) for n, name in after.items()},
            ),
            "slides": [
                {
                    "slide": n,
                    "before": f"/api/preview/{session.id}/before/{n}" if n in before else None,
                    # The run number rides along so that a page, and anything
                    # caching for it, cannot answer with the last run's picture
                    # from a URL that has not changed.
                    "after": (
                        f"/api/preview/{session.id}/after/{n}?run={session.run}"
                        if n in after else None
                    ),
                }
                for n in numbers
            ],
        }

    # -- the design check --------------------------------------------------- #

    def _run_qa(self) -> dict[str, Any]:
        """Render a deck and ask the model what it looks like.

        One deck, no master, no brand file. The check reads a picture, so the
        only thing it cannot do without is the renderer -- and a run with no
        renderer answers with a report that says so rather than an empty page.
        """
        content_type = self.headers.get("Content-Type", "")
        parts = parse_multipart(self._body(), _boundary_of(content_type))
        options = _options_of(parts)

        decks = [p for p in parts if p.name == "deck" and p.filename]
        if not decks:
            raise _BadRequest("no deck to check was sent")

        workdir = Path(tempfile.mkdtemp(prefix="formatting-tool-qa-", dir=_workroot()))
        keep = False
        try:
            deck = _spill(workdir / "deck", decks[0])
            ai = AIConfig(
                model=(options.get("model") or SUGGESTED_MODEL).strip(),
                effort=options.get("effort") or DEFAULT_EFFORT,
            )
            slides = [int(n) for n in options.get("slides") or []] or None

            with _RUN_LOCK, _capture() as collected:
                started = time.perf_counter()
                profile = read_deck(deck)
                # Pass 0's renders. `_QaSession.before_dir` is the same path;
                # it cannot be asked for it yet because the session is made
                # from the report this is about to produce.
                rendered = _qa_render(deck, workdir / "before" / "0", slides)
                # Narrowed to what was asked for, because asking is not
                # getting: a renderer free to ignore the request comes back
                # with the whole deck, and PowerPoint does exactly that for
                # anything past a fifth of it. Every extra slide here is a
                # model call somebody did not ask to pay for.
                if slides:
                    rendered = {
                        n: name for n, name in rendered.items() if n in set(slides)
                    }
                report = review_deck(
                    deck,
                    [(n, workdir / "before" / "0" / name)
                     for n, name in rendered.items()],
                    ai=ai,
                    profile=profile,
                )
                elapsed = time.perf_counter() - started

            session = _QaSession(
                id=secrets.token_hex(8), directory=workdir, deck=deck, report=report
            )
            _remember(session, _QA_SESSIONS)
            keep = True
            session.payload = _qa_check_payload(
                session, rendered, collected.records, elapsed
            )
            return session.payload
        finally:
            # The deck stays on disk when a session owns it: the page will ask
            # for the ticked steps to be applied to this exact file.
            if not keep:
                rmtree(workdir, ignore_errors=True)

    def _run_qa_handoff(self) -> dict[str, Any]:
        """Run the design check on what the deck check just produced.

        THE ORDER IS THE PIPELINE'S OWN. `pipeline.run` puts applying the
        master first and says why: putting content into the master's
        placeholders resolves a great many findings by itself and raises a few
        of its own, so a report made before it describes a deck nobody will
        send. The design check is the same argument one stage further on. It
        reads a picture, and the picture of a deck that has not been restyled
        yet is a picture of a deck that is about to change.

        WHY IT IS A HAND-OVER AND NOT A STAGE. Applying is the designer's
        decision -- they tick what to apply and leave the rest -- so there is
        no restyled deck to look at until they have made it. A single run
        would have to either apply everything without asking or check the deck
        it was about to change, and both answer a question nobody posed.

        A SESSION OF ITS OWN, on a copy. The two checks have different
        lifetimes: the deck check's files are swept when its rounds move on,
        and a design check holding a path into that directory would be reading
        a file another page is entitled to delete.
        """
        body = self._json_body()
        source = _session(str(body.get("session", "")))
        if not source.applied_once or not source.fixed.is_file():
            raise _BadRequest(
                "nothing has been applied yet, so there is no restyled deck "
                "to check"
            )

        options = body.get("options") or {}
        ai = AIConfig(
            model=(str(options.get("model") or "") or SUGGESTED_MODEL).strip(),
            effort=str(options.get("effort") or "") or DEFAULT_EFFORT,
        )
        slides = [int(n) for n in options.get("slides") or []] or None

        workdir = Path(tempfile.mkdtemp(prefix="formatting-tool-qa-", dir=_workroot()))
        keep = False
        try:
            deck = workdir / source.fixed.name
            copyfile(source.fixed, deck)

            with _RUN_LOCK, _capture() as collected:
                started = time.perf_counter()
                profile = read_deck(deck)
                rendered = _qa_render(deck, workdir / "before" / "0", slides)
                if slides:
                    rendered = {
                        n: name for n, name in rendered.items() if n in set(slides)
                    }
                report = review_deck(
                    deck,
                    [(n, workdir / "before" / "0" / name)
                     for n, name in rendered.items()],
                    ai=ai,
                    profile=profile,
                    # THE MASTER THE DECK CHECK RESTYLED THIS DECK ONTO. Without
                    # it this page infers a reference from the file it is
                    # auditing and can only ask whether the deck agrees with
                    # itself; with it, it can ask whether the colours are the
                    # brand's -- which is the question somebody looking at a
                    # freshly restyled deck is actually asking. See
                    # `designqa.WITH_MASTER_RULES`.
                    spec=source.spec,
                )
                elapsed = time.perf_counter() - started

            session = _QaSession(
                id=secrets.token_hex(8), directory=workdir, deck=deck, report=report,
                handed_over=source.deck.name, spec=source.spec,
            )
            _remember(session, _QA_SESSIONS)
            keep = True
            session.payload = _qa_check_payload(
                session, rendered, collected.records, elapsed
            )
            return session.payload
        finally:
            if not keep:
                rmtree(workdir, ignore_errors=True)

    def _run_qa_recheck(self) -> dict[str, Any]:
        """Ask the model again, about the deck the last round produced.

        WHY THIS IS A BUTTON AND NOT A LOOP. `apply.qafix` takes every step
        once and stops, because a tool that keeps correcting until the model is
        happy converges on a deck nobody chose. That argument is about the
        tool deciding to go round again on its own. A designer who has looked
        at a before and an after and wants the corrected deck read fresh is
        making the opposite kind of decision -- they have seen the result and
        are asking a new question about it -- and there is no reason the answer
        to that should mean uploading the download.

        THE CORRECTED DECK BECOMES THE DECK. `session.deck` moves to it, so
        applying, rendering and downloading go on meaning "this check's deck"
        without knowing which pass they are in. What that costs is the original:
        after a re-check, the before pictures are of the deck as the last round
        left it, which is the only honest baseline for a report about that file.
        The uploaded file is still on disk, and untouched -- nothing here has
        ever written to it -- but the page stops offering it, because a before
        and after spanning two rounds is not a comparison anybody asked for.

        Refused when there is nothing new to read. Re-checking a deck no round
        has touched is a second model call for the answer already on the page,
        and paying for that by accident is the sort of thing a button does.
        """
        body = self._json_body()
        session = _session(str(body.get("session", "")), _QA_SESSIONS)
        if not session.fixed.is_file():
            raise _BadRequest(
                "nothing has been applied yet, so a re-check would be the same "
                "question about the same deck"
            )

        options = body.get("options") or {}
        ai = AIConfig(
            model=(str(options.get("model") or "") or SUGGESTED_MODEL).strip(),
            effort=str(options.get("effort") or "") or DEFAULT_EFFORT,
        )
        slides = [int(n) for n in options.get("slides") or []] or None

        with _RUN_LOCK, _capture() as collected:
            started = time.perf_counter()
            # Promote first, so `before_dir` and `fixed` already name this
            # pass's paths and nothing is written over the round being read.
            corrected = session.fixed
            session.passes += 1
            session.run = 0
            baseline = session.directory / f"pass-{session.passes}-{session.name}"
            try:
                copyfile(corrected, baseline)
            except OSError as exc:
                raise _BadRequest(f"could not take the corrected deck: {exc}")
            session.deck = baseline

            profile = read_deck(baseline)
            rendered = _qa_render(baseline, session.before_dir, slides)
            if slides:
                rendered = {
                    n: name for n, name in rendered.items() if n in set(slides)
                }
            report = review_deck(
                baseline,
                [(n, session.before_dir / name) for n, name in rendered.items()],
                ai=ai,
                profile=profile,
                # A re-check looks at what this session produced, so it is the
                # same deck on the same master. Dropping it here would make the
                # second pass quieter than the first for no reason a designer
                # could see.
                spec=session.spec,
            )
            session.report = report
            elapsed = time.perf_counter() - started

        session.payload = _qa_check_payload(
            session, rendered, collected.records, elapsed
        )
        return session.payload

    def _run_qa_apply(self) -> dict[str, Any]:
        """Step the type on the ticked shapes, and render what it did.

        Every round starts from the deck this session is about and applies the
        ticks as they now stand, which is also what makes undo a replay rather
        than a reverse -- see `_run_qa_undo`. One round, no re-check: see
        `apply.qafix` for why a loop that steps until the model is happy is the
        wrong shape.
        """
        body = self._json_body()
        session = _session(str(body.get("session", "")), _QA_SESSIONS)
        chosen = [str(key) for key in body.get("fix", [])]
        # On by default, and it is half of what this button does: the
        # corrections that can be made are made, and everything else is
        # written into the deck as a comment so it is work somebody can pick
        # up rather than a list on a page they will close.
        file_the_rest = bool(body.get("notes", True))
        if not chosen and not file_the_rest:
            raise _BadRequest("nothing was ticked")

        # WHAT THIS ROUND WAS ASKED FOR, kept for the undo to replay, and the
        # undo list cleared: a fresh set of ticks is a new decision about the
        # whole deck, and carrying yesterday's undo into it would silently drop
        # a fix somebody has just asked for.
        session.ticked = list(chosen)
        session.undone = []
        session.notes = file_the_rest
        return self._qa_apply(session, chosen, file_the_rest)

    def _run_qa_undo(self) -> dict[str, Any]:
        """Take one applied change back, or put one back.

        A REPLAY, NOT A REVERSE, for the reason `_QaSession.undone` gives: the
        corrections this page makes have no inverses. A type step that was
        rolled back for spilling, a move a guard refused, a widening that
        stopped at a neighbour -- none of those is a delta that can be
        subtracted, and a deck edited twice is not the deck edited once. So the
        round is run again without the change, from the same starting file, and
        what comes back is the deck that round would have produced had the
        change never been ticked.

        Cumulative and reversible: `undo` adds rows to the list, `redo` takes
        them off, and either may name several at once. The answer is the same
        shape as `/api/qa/apply` because it is the same act.
        """
        body = self._json_body()
        session = _session(str(body.get("session", "")), _QA_SESSIONS)
        if not session.applied_once:
            raise _BadRequest("nothing has been applied to this deck yet")

        undo = [str(key) for key in body.get("undo", [])]
        redo = {str(key) for key in body.get("redo", [])}
        if not undo and not redo:
            raise _BadRequest("no change was named to undo")

        held = [key for key in (*session.undone, *undo) if key not in redo]
        session.undone = list(dict.fromkeys(held))
        chosen = [key for key in session.ticked if key not in session.undone]
        return self._qa_apply(session, chosen, session.notes, strict=False)

    def _qa_apply(
        self, session: "_QaSession", chosen: list, file_the_rest: bool,
        strict: bool = True,
    ) -> dict[str, Any]:
        """One round of the corrections on this session, as it now stands.

        `strict` is off for a replay. Taking back the last change a round made
        leaves nothing to apply, and that is the correct end of an undo rather
        than a request nobody can carry out: what comes back is the deck this
        session started from, which is exactly what was asked for.
        """

        # Two kinds of correction, applied by two halves of the tool. The
        # proposals go through `apply.apply_fixes`, which is where the brand
        # checks and the geometric guards live; the measured verbs go through
        # PowerPoint, because their targets are read off the renderer. The page
        # is shown one list.
        issues = issues_for(session.report, chosen)
        steps = steps_for(session.report, chosen)
        if strict and not steps and not issues and not file_the_rest:
            raise _BadRequest(
                "none of those are corrections this check can make; they are "
                "tasks for a designer, and ticking the box beside Apply writes "
                "them into the deck"
            )

        session.run += 1
        with _RUN_LOCK, _capture() as collected:
            started = time.perf_counter()
            result = _qa_propose(session, issues)
            if steps:
                # In place: the proposals have already written this file, and
                # copying the upload over it would undo them.
                measured = apply_steps(session.fixed, session.fixed, steps)
                result.applied.extend(measured.applied)
                result.skipped.extend(measured.skipped)
                result.output = measured.output or result.output
                result.reason = result.reason or measured.reason
            left = outstanding(session.report, chosen, result.applied)
            written = 0
            if file_the_rest and result.output is not None:
                written = add_comments(result.output, comments_for(session.report, left))
            elapsed = time.perf_counter() - started
        session.applied_once = True
        _qa_sweep(session)

        # Real slide numbers only. A correction about the deck rather than
        # about one slide reports slide 0 -- `_qa_propose` has nothing else to
        # put there -- and 0 costs twice: the page is handed a preview URL that
        # 404s, and `render_deck` reads a request naming no valid slide as a
        # request for the WHOLE DECK, which on a ninety-slide deck is a render
        # nobody asked for and eighty-nine pairs of identical pictures.
        touched = sorted({c.slide for c in result.applied if c.slide})
        after: dict[int, str] = {}
        if result.output is not None and result.applied:
            after = _qa_render(session.fixed, _qa_after_dir(session), touched)

        return {
            "session": session.id,
            **result.to_dict(),
            # The rows that have been taken back, so a reload or a second undo
            # does not have to be told again. The page reads this rather than
            # keeping its own copy: the replay is the server's and the list it
            # replayed from is the only one that is true.
            "undone": list(session.undone),
            # What is left to do by hand, and how much of it is now in the
            # deck. Reported rather than assumed: writing a comment needs
            # PowerPoint, and a page that says "filed" when nothing was filed
            # is worse than one that says nothing.
            "outstanding": [task.to_dict() for task in left],
            "comments_written": written,
            "comments_asked": bool(file_the_rest),
            # Named per round so nothing -- the page, or anything caching for
            # it -- can answer with the last round's picture.
            "slides": [
                {
                    "slide": n,
                    "before": (
                        f"/api/qa/preview/{session.id}/before/{n}"
                        f"?pass={session.passes}"
                    ),
                    "after": (
                        f"/api/qa/preview/{session.id}/after/{n}"
                        f"?pass={session.passes}&run={session.run}"
                        if n in after else None
                    ),
                }
                for n in touched
            ],
            "download": (
                f"/api/qa/download/{session.id}"
                if result.output is not None else None
            ),
            "logs": collected.records,
            "elapsed_s": round(elapsed, 2),
        }

    def _run_qa_render(self) -> dict[str, Any]:
        """Render these slides again, both sides of the pair.

        Both, because the two are read together: asking for a slide again is
        asking whether the comparison on the screen is true, and refreshing
        half of it answers half the question. The same reasoning as
        `_forget` on the deck check, and the same failure posture -- a render
        that will not come back leaves the cached one in place and the button
        looks like it did nothing, which the designer can answer by pressing
        it again.
        """
        body = self._json_body()
        session = _session(str(body.get("session", "")), _QA_SESSIONS)
        numbers = sorted({int(n) for n in body.get("slides", [])})
        if not numbers:
            raise _BadRequest("no slide was named")
        if body.get("force"):
            _qa_forget(session, numbers)

        with _RUN_LOCK:
            before = _qa_render(session.deck, session.before_dir, numbers)
            after: dict[int, str] = {}
            if session.fixed.exists():
                after = _qa_render(session.fixed, _qa_after_dir(session), numbers)

        return {
            "session": session.id,
            "slides": [
                {
                    "slide": n,
                    "before": (
                        f"/api/qa/preview/{session.id}/before/{n}"
                        f"?pass={session.passes}&t={int(time.time())}"
                        if n in before else None
                    ),
                    "after": (
                        f"/api/qa/preview/{session.id}/after/{n}"
                        f"?pass={session.passes}&run={session.run}&t={int(time.time())}"
                        if n in after else None
                    ),
                }
                for n in numbers
            ],
        }

    def _qa_session(self, route: str) -> None:
        """A check that has already run, by id.

        The page normally gets its report as the answer to the upload that
        produced it. It cannot when the deck check hands a restyled file over:
        that check runs server-side, and what crosses to the browser is an id
        in a link. So the last answer is kept on the session and served here.

        A reload gets the same thing, which is worth having on its own: the
        design check is minutes of rendering and a model call per slide, and
        losing it to a refresh is losing all of that.
        """
        session_id = route.rsplit("/", 1)[-1]
        try:
            session = _session(session_id, _QA_SESSIONS)
        except _BadRequest as exc:
            self._send_json(404, {"error": str(exc)})
            return
        if not session.payload:
            self._send_json(404, {"error": "that check has no answer to show"})
            return
        self._send_json(200, session.payload)

    def _qa_preview(self, route: str) -> None:
        parts = route.strip("/").split("/")   # api qa preview <id> <which> <n>
        if len(parts) != 6 or parts[4] not in ("before", "after"):
            self._send_json(404, {"error": "bad preview path"})
            return
        try:
            session = _session(parts[3], _QA_SESSIONS)
        except _BadRequest as exc:
            self._send_json(404, {"error": str(exc)})
            return

        directory = (
            _qa_after_dir(session) if parts[4] == "after" else session.before_dir
        )
        image = _image_for(directory, parts[5])
        if image is None:
            self._send_json(404, {"error": "no such rendered slide"})
            return
        self._send_file(image, "image/png")

    def _qa_download(self, route: str) -> None:
        session_id = route.rsplit("/", 1)[-1]
        try:
            session = _session(session_id, _QA_SESSIONS)
        except _BadRequest as exc:
            self._send_json(404, {"error": str(exc)})
            return
        # This pass's output where there is one, and otherwise the deck this
        # pass is READING -- which after a re-check is the previous pass's
        # output, already corrected. Without the fallback, re-checking made the
        # corrected deck unreachable: the round that produced it belongs to a
        # pass that is over, and the new pass has written nothing yet.
        #
        # Only past pass 0. There, `deck` is still the file they uploaded, and
        # handing that back labelled as the corrected deck is a lie about a
        # deck they would then send.
        source = session.fixed
        if not source.exists() and session.passes:
            source = session.deck
        if not source.exists():
            self._send_json(404, {"error": "nothing has been applied yet"})
            return
        body = source.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        self.send_header(
            "Content-Disposition", f'attachment; filename="{source.name}"'
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, route: str) -> None:
        """One file out of the static directory, by name and nothing else.

        The name arrives from a URL. Joining an arbitrary string onto a path
        is how a local tool starts serving files it was never asked about, so
        this takes the last segment, refuses anything that is not a plain
        name, and refuses any extension the pages do not use.
        """
        name = route.rsplit("/", 1)[-1]
        kind = _STATIC_TYPES.get(Path(name).suffix.lower())
        if kind is None or name != Path(name).name or name.startswith("."):
            self._send_json(404, {"error": f"no asset named {name}"})
            return
        target = STATIC / name
        if not target.is_file():
            self._send_json(404, {"error": f"no asset named {name}"})
            return
        self._send_file(target, kind)

    def _preview(self, route: str) -> None:
        parts = route.strip("/").split("/")      # api preview <id> <which> <n>
        if len(parts) != 5 or parts[3] not in ("before", "after"):
            self._send_json(404, {"error": "bad preview path"})
            return
        try:
            session = _session(parts[2])
        except _BadRequest as exc:
            self._send_json(404, {"error": str(exc)})
            return

        directory = (
            _after_dir(session) if parts[3] == "after"
            else session.directory / "before"
        )
        image = _image_for(directory, parts[4])
        if image is None:
            self._send_json(404, {"error": "no such rendered slide"})
            return
        self._send_file(image, "image/png")

    def _download(self, route: str) -> None:
        session_id = route.rsplit("/", 1)[-1]
        try:
            session = _session(session_id)
        except _BadRequest as exc:
            self._send_json(404, {"error": str(exc)})
            return
        if not session.fixed.exists():
            self._send_json(404, {"error": "nothing has been applied yet"})
            return
        body = session.fixed.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        self.send_header(
            "Content-Disposition", f'attachment; filename="{session.fixed.name}"'
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict[str, Any]:
        try:
            return json.loads(self._body().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _BadRequest(f"could not read the request body: {exc}") from exc

    # -- the one real endpoint --------------------------------------------- #

    def _guarded(self, work) -> tuple[int, dict[str, Any]]:
        try:
            return 200, work()
        except _BadRequest as exc:
            return 400, {"error": str(exc)}
        except (DeckReadError, GuidelinesError, AIValidationError, ApplyError) as exc:
            # The CLI exits 2 on these and prints the message alone; they are
            # explained errors, so the page gets the same treatment.
            return 400, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a local tool wants the trace
            log.exception("unhandled error handling a request")
            return 500, {"error": str(exc), "detail": traceback.format_exc()}

    def _run_pipeline(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        parts = parse_multipart(self._body(), _boundary_of(content_type))
        options = _options_of(parts)

        masters = [p for p in parts if p.name == "master" and p.filename]
        decks = [p for p in parts if p.name == "deck" and p.filename]
        if not masters:
            raise _BadRequest("no master deck was sent")
        if not decks:
            raise _BadRequest("no deck to check was sent")

        workdir = Path(tempfile.mkdtemp(prefix="formatting-tool-ui-", dir=_workroot()))
        keep = False
        try:
            config = RunConfig(
                master=_spill(workdir / "master", masters[0]),
                # Each deck gets its own directory: two uploads may share a
                # filename, and the report identifies decks by name.
                decks=[_spill(workdir / f"deck{i}", p) for i, p in enumerate(decks)],
                guidelines=_resolve_guidelines(self.server.root, options.get("guidelines")),
                use_ai=bool(options.get("use_ai")),
                render=bool(options.get("render")),
                # Always on in the page: it is a local tool, and the raw
                # exchange is what makes an AI miss debuggable at all.
                ai_debug=True,
                min_confidence=float(options.get("min_confidence") or 0.0),
                batch_size=int(options.get("batch_size") or DEFAULT_BATCH_SIZE),
                ai=AIConfig(
                    model=(options.get("model") or SUGGESTED_MODEL).strip(),
                    effort=options.get("effort") or DEFAULT_EFFORT,
                ),
            )

            with _RUN_LOCK, _capture() as collected:
                started = time.perf_counter()
                report = run(config)
                elapsed = time.perf_counter() - started

            records = collected.records
            session = _Session(
                id=secrets.token_hex(8),
                directory=workdir,
                master=config.master,
                deck=config.decks[0],
                report=report.to_dict(),
                spec=report.spec,
                color_intents=list(report.color_intents),
                ai=config.ai,
                use_ai=config.use_ai,
            )
            _remember(session)
            keep = True

            return {
                "session": session.id,
                "fixable": [i.id for i in fixable(report.issues)],
                "report": report.to_dict(),
                "logs": records,
                "elapsed_s": round(elapsed, 2),
                "ai": {
                    "requested": config.use_ai,
                    # An AI failure is logged and swallowed: the run still
                    # returns a report and exits 0. Say so plainly instead.
                    "ran": config.use_ai
                    and not any(r["level"] == "error" for r in records),
                    "model": config.ai.model,
                },
            }
        finally:
            # The deck stays on disk when a session owns it: the page will ask
            # for fixes to be applied to this exact file, minutes from now.
            if not keep:
                rmtree(workdir, ignore_errors=True)

    # -- plumbing ---------------------------------------------------------- #

    def _body(self) -> bytes:
        """The whole request body.

        No size cap. There was a 256 MB one and it was the wrong shape of
        guard for this server: a real deck of photographs reaches that, and
        the person hitting the limit is the one who most needs the check run.

        What remains is the machine. The body is read whole and the multipart
        parse copies out of it, so a deck needs a small multiple of its own
        size in memory. That is a resource question rather than a policy one,
        and the host answers it.

        Safe here because of what this server is: loopback by default, one run
        at a time behind `_RUN_LOCK`, and not hardened -- see the module
        docstring. Anyone passing `--host` to put it on a network is past the
        point where a byte count was the protection.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise _BadRequest("empty request body")
        return self.rfile.read(length)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json")

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(500, {"error": f"missing UI asset: {path}"})
            return
        self._send(200, body, content_type)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Served straight off disk so editing index.html and refreshing the
        # page is the whole edit loop.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Re-exported under their old names, which `tests.test_workdir_root` imports
# from here. Not redefined: this was the implementation, and keeping it here
# is what let the render directory miss it: `render_deck` makes its own
# directory and had no way to reach this one, so FORMATTING_TOOL_WORKDIR moved
# the uploads somewhere safe and left the PNGs in the directory that gets
# swept. That is the directory a real run then lost a slide out of. Both read
# `formatting_tool.workdir` now, so setting the variable covers everything the
# run writes.
_workroot = workroot
WORKDIR_ENV = _WORKDIR_ENV


def _outcome_id(outcome: Any) -> str:
    """The id a change is addressed by, which every change has to have.

    A first-round finding carries the id the report gave it. A SECOND-ROUND one
    often does not: it was measured on the written deck by `_recheck`, which
    builds findings rather than reading them back off a report, and nothing had
    needed an id for them before. The page sent `null` for those, so the Undo
    beside a second-pass change was addressed to nothing and did nothing when
    it was pressed -- silently, because an empty id looks exactly like a click
    on nothing at all.

    The fingerprint is the same id the report would have given it, so undoing
    one names the same finding `apply_fixes(undone=...)` matches on.
    """
    return outcome.issue.id or outcome.issue.fingerprint()


def _forget(session: _Session, slides: list[int]) -> None:
    """Drop the cached renders of these slides, both sides of the pair.

    Both, because the two are read together: a designer asking for a slide
    again is asking whether the comparison in front of them is true, and
    refreshing half of it answers half the question.

    This one may fail quietly: it is a designer asking for a picture to be made
    again, so the worst case is the button appearing not to work, and they can
    press it again. Nothing silently presents an old image as a new one -- that
    is what the per-run directories are for.
    """
    for number in slides:
        for directory in (session.directory / "before", _after_dir(session)):
            try:
                (directory / f"{number}.png").unlink()
            except OSError:
                pass


def _layout_picks(session: _Session) -> list:
    """Which layout each slide belongs on, as somebody looking would say.

    The structural matcher counts the content regions a slide uses against the
    regions a layout offers, which is right on a tidy deck and not on a messy
    one: a messy deck keeps its copy in loose text boxes -- that is what makes
    it messy -- so the count is of boxes rather than of regions. `ai.layout`
    exists for exactly that, and was reachable only from
    `validate --apply-master`; the rebuild a designer runs from this page never
    asked it, and picked structurally every time.

    Silent and empty whenever it cannot help: the AI layer off, no renderer on
    this machine, no API key, a model that declines. The structural matcher
    then decides alone, which is what happened before this was wired in, so the
    worst case is the behaviour that was already there.

    Computed once and kept. It costs a call a slide, and an undo replays the
    whole run -- charging again for the same answer on every take-back would
    make the cheapest correction on the page the most expensive.
    """
    if session.layout_picks is not None:
        return session.layout_picks
    session.layout_picks = []           # so a failure is not retried per run
    if not session.use_ai or session.spec is None or session.ai is None:
        return session.layout_picks

    try:
        from ..ai.layout import choose_layouts  # noqa: PLC0415 - lazy, optional

        directory = session.directory / "before"
        rendered = _render_into(session.deck, directory)
        if not rendered:
            log.info("no layout pass: nothing rendered for %s", session.deck.name)
            return session.layout_picks

        images = sorted(
            (number, directory / name) for number, name in rendered.items()
        )
        session.layout_picks = choose_layouts(
            session.spec,
            images,
            model=session.ai.model,
            thinking_budget=session.ai.thinking_budget,
            api_key_env=session.ai.api_key_env,
            master=session.master,
        )
        log.info(
            "the model chose a layout for %d of %d slide(s)",
            len(session.layout_picks), len(images),
        )
    except Exception:
        log.warning(
            "could not ask the model which layouts these slides belong on; "
            "the structural matcher decides alone", exc_info=True,
        )
    return session.layout_picks


# What the static route will serve. A whitelist rather than a guess from the
# file: this directory is the page, and a page is HTML, CSS and pictures.
_STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def _qa_render(
    deck: Path, directory: Path, slides: Optional[list[int]] = None
) -> dict[int, str]:
    """Render for the design check, which renders bigger than the rest.

    See `render.DESIGN_QA_SIZE`: this is the one call where the picture IS the
    evidence, so it goes up at the largest size the model will look at without
    downsizing it first.
    """
    return _render_into(
        deck, directory, slides, renderer=available_renderer(DESIGN_QA_SIZE)
    )


def _qa_propose(session: "_QaSession", issues: list) -> Any:
    """Carry out the model's proposals through the shared applier.

    Everything a proposal needs was written for the rule layer: the value
    checks in `apply.fixers.fix_ai_action`, the overlap and alignment guards
    every geometric fix goes through, and the second round that clears up what
    the first one caused. This hands it findings and translates what comes
    back into the shape the design page speaks.

    With no proposals it still writes the file: a round whose whole purpose is
    to file the tasks as comments needs a copy to put them in, and this tool
    never edits the deck it was given.
    """
    from ..apply.qafix import Change, QaFixResult, SkippedStep  # noqa: PLC0415

    result = QaFixResult(output=session.fixed)
    if not issues:
        try:
            session.fixed.parent.mkdir(parents=True, exist_ok=True)
            copyfile(session.deck, session.fixed)
        except OSError as exc:
            return QaFixResult(reason=f"could not copy the deck: {exc}")
        return result

    # WHAT THE DECK ALREADY HAD GOES IN BESIDE WHAT WAS TICKED, and only the
    # ticked half is selected. `apply_fixes` runs a second round over the
    # findings its own work INTRODUCED, and it works out which those are by
    # asking what the deck had before -- which is the list it was handed. Given
    # only the ticked rows, every other deterministic finding on the deck reads
    # as newly introduced and is corrected without anyone asking for it.
    #
    # Measured on the test deck before this line existed: 56 rows ticked, and a
    # second round of 121 corrections nobody chose -- 72 margin moves and 27
    # recolours, the recolours measured against a spec derived from the deck's
    # own theme. That is the design check applying a brand it inferred from the
    # file it was auditing, which is the one thing this page must never do.
    baseline = list(session.report.rule_issues)
    known = {issue.id for issue in baseline}
    everything = baseline + [i for i in issues if i.id not in known]
    try:
        applied = apply_fixes(
            deck=session.deck,
            issues=everything,
            out=session.fixed,
            selected=[issue.id for issue in issues],
            spec=session.report.spec,
        )
    except ApplyError as exc:
        return QaFixResult(reason=str(exc))

    for outcome in applied.applied:
        result.applied.append(Change(
            op=outcome.issue.fix.op if outcome.issue.fix else "",
            slide=outcome.issue.slide or 0,
            shape_id=outcome.issue.shape_id or 0,
            shape=outcome.issue.shape or "",
            detail=outcome.detail,
            task_id=outcome.issue.id or "",
        ))
    for outcome in applied.skipped:
        result.skipped.append(SkippedStep(
            op=outcome.issue.fix.op if outcome.issue.fix else "",
            slide=outcome.issue.slide or 0,
            shape_id=outcome.issue.shape_id or 0,
            shape=outcome.issue.shape or "",
            # The refusal, which is the interesting half of a guarded
            # applier: a proposal the deck would not vouch for says so.
            reason=outcome.detail,
            task_id=outcome.issue.id or "",
        ))

    # THE SECOND ROUND IS A CHANGE TO THE DECK AND HAS TO BE ON THE LIST.
    # `apply_fixes` runs a second pass that clears up what the first one
    # caused -- a box that no longer clears its neighbour once the type around
    # it changed size -- and reports it separately, because the rule pipeline's
    # page gives it a section of its own. This page had no such section and
    # read only the first round, so those edits were made and shown nowhere:
    # a slide visibly reflowed under a line saying "1 step(s) taken", which is
    # the page telling a designer something untrue about their own deck.
    #
    # They go in beside the rest rather than into a section of their own. A
    # designer looking at a before and an after wants to know what accounts for
    # the difference, and which round a change belongs to is this tool's
    # bookkeeping. The wording says where it came from.
    for outcome in applied.second_round:
        where = "following on from the first round"
        if outcome.applied:
            result.applied.append(Change(
                op=outcome.issue.fix.op if outcome.issue.fix else "",
                slide=outcome.issue.slide or 0,
                shape_id=outcome.issue.shape_id or 0,
                shape=outcome.issue.shape or "",
                detail=f"{outcome.detail} ({where})",
                task_id=outcome.issue.id or "",
            ))
        else:
            result.skipped.append(SkippedStep(
                op=outcome.issue.fix.op if outcome.issue.fix else "",
                slide=outcome.issue.slide or 0,
                shape_id=outcome.issue.shape_id or 0,
                shape=outcome.issue.shape or "",
                reason=f"{outcome.detail} ({where})",
                task_id=outcome.issue.id or "",
            ))
    return result


def _qa_forget(session: "_QaSession", slides: list[int]) -> None:
    """Drop the cached renders of these slides, both sides.

    May fail quietly: this is a designer asking for a picture to be made
    again, so the worst case is a button that appears not to work and can be
    pressed again. Nothing here presents an old image as a new one -- the
    per-round directories are what guarantee that.
    """
    for number in slides:
        for directory in (session.before_dir, _qa_after_dir(session)):
            try:
                (directory / f"{number}.png").unlink()
            except OSError:
                pass


def _qa_check_payload(
    session: "_QaSession", rendered: dict, logs: list, elapsed: float
) -> dict[str, Any]:
    """One answer shape for every way the design check can be started.

    Three routes produce it -- an upload, a re-check of the corrected deck, and
    a hand-over from the deck check -- and the page reads one of them. Built
    here so a field added for one arrives in all three, which is not
    hypothetical: `pass` was added for the re-check and the page reads it on
    every render.
    """
    return {
        "session": session.id,
        "report": session.report.to_dict(),
        "renderer_ok": bool(rendered),
        "aspect": _aspect_of(
            {n: str(session.before_dir / name) for n, name in rendered.items()}
        ),
        "pass": session.passes,
        "handed_over": session.handed_over,
        "slides": [
            {
                "slide": n,
                # The pass is in the URL so a re-check's pictures are not the
                # last pass's out of the browser's cache.
                "before": (
                    f"/api/qa/preview/{session.id}/before/{n}"
                    f"?pass={session.passes}"
                ),
                "after": None,
            }
            for n in sorted(rendered)
        ],
        "logs": logs,
        "elapsed_s": round(elapsed, 2),
    }


def _qa_round(session: "_QaSession") -> str:
    """What this round is called on disk.

    BOTH COUNTERS, because a re-check resets the round number: pass 0 round 1
    and pass 1 round 1 are different pictures of different decks, and one
    directory for the two of them is the page showing the wrong one.

    In a function of its own because two places need it and they must agree.
    `_qa_after_dir` writes the name and `_qa_sweep` reads it to decide which
    rounds are over, and when only the first of them learned about passes the
    sweep could no longer recognise the round that was running.
    """
    return f"{session.passes}-{session.run}"


def _qa_after_dir(session: "_QaSession") -> Path:
    return session.directory / "after" / _qa_round(session)


def _qa_sweep(session: "_QaSession") -> None:
    """Drop the renders and the deck of rounds that are over. Best effort.

    Safe to fail: the current round reads its own directory and writes its own
    filename, so a file Windows will not let go of costs disk space rather
    than correctness. That is exactly what could not be said when deleting was
    what made the pictures on the page true -- see `_Session.run`.
    """
    # On `name` rather than on `deck.name`: `deck` stops being the upload after
    # the first re-check, and a glob built from it then matched none of the
    # files this ever writes, so every round's output stayed on disk.
    for old_deck in session.directory.glob(f"checked-*-{session.name}"):
        if old_deck != session.fixed:
            try:
                old_deck.unlink()
            except OSError:
                pass
    root = session.directory / "after"
    if not root.is_dir():
        return
    keep = _qa_round(session)
    for directory in root.iterdir():
        if directory.is_dir() and directory.name != keep:
            rmtree(directory, ignore_errors=True)


def _after_dir(session: _Session) -> Path:
    """Where this run's renders of the corrected deck live.

    One directory per run. See `_Session.run` for why the images are not simply
    replaced in place.
    """
    return session.directory / "after" / str(session.run)


def _sweep(session: _Session) -> None:
    """Remove renders of runs that are over. Best effort, and that is safe.

    Nothing depends on this working: the current run reads its own directory
    and cannot see these whether they go or not. It is housekeeping, so a file
    Windows will not let go of costs disk space rather than correctness --
    which is exactly what could not be said when the same deletion was what
    made the pictures on the page true.
    """
    root = session.directory / "after"
    if not root.is_dir():
        return
    for directory in root.iterdir():
        if directory.is_dir() and directory.name != str(session.run):
            rmtree(directory, ignore_errors=True)


def _undone_field(key: str, issues: list[Any], field_name: str) -> Any:
    """One field of the finding an undo id names, or None if it names none.

    None is a real answer here: an id can come from the recheck rather than
    from the report -- a second-round fix is undoable too -- and then there is
    no report finding to read a shape name off. The page shows the id alone,
    which is what it has.
    """
    for issue in issues:
        if issue.id == key:
            value = getattr(issue, field_name, None)
            return getattr(value, "value", value)
    return None


def _options_of(parts: list[_Part]) -> dict[str, Any]:
    for part in parts:
        if part.name == "options" and part.filename is None:
            try:
                return json.loads(part.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _BadRequest(f"could not read the options field: {exc}") from exc
    return {}


def _spill(directory: Path, part: _Part) -> Path:
    """Write an uploaded part to disk under its own original filename."""
    directory.mkdir(parents=True, exist_ok=True)
    name = Path(part.filename or "deck.pptx").name or "deck.pptx"
    target = directory / name
    target.write_bytes(part.data)
    return target


def _render_into(
    deck: Path,
    directory: Path,
    wanted: Optional[list[int]] = None,
    renderer: Optional[Any] = None,
) -> dict[int, str]:
    """Render a deck into `directory`, reusing what is already there.

    Rendering drives PowerPoint and costs seconds, and the before-images do not
    change between one apply and the next, so they are rendered once.

    `wanted` is the slides the page is about to show. Given them, only the ones
    not already on disk are rendered, and the renderer is asked for those alone
    -- which on a big deck is the difference between eleven seconds and one.
    Without them this renders whatever it has to and keeps everything, which is
    what every caller did before.
    """
    existing = _existing_images(directory)
    missing = [n for n in (wanted or []) if n not in existing]
    if existing and (wanted is None or not missing):
        return existing

    # Named only when one was asked for. `render_deck` picks the host's own
    # renderer when it is not told, so passing None would mean the same thing
    # -- but it would mean it in a call that has grown an argument, and the
    # tests that stand in for the renderer bind the call this module has
    # always made.
    extra = {"renderer": renderer} if renderer is not None else {}
    images = render_deck(deck, slides=missing or None, **extra)
    if not images:
        log.info("no preview for %s: %s", deck.name, images.reason)
        return existing
    directory.mkdir(parents=True, exist_ok=True)
    out = dict(existing)
    for number, path in images.images.items():
        target = directory / f"{number}.png"
        target.write_bytes(path.read_bytes())
        out[number] = target.name
    images.cleanup()
    return out


def _aspect_of(*renders: dict[int, str]) -> Optional[float]:
    """Width over height of the rendered slides, read off a PNG header.

    Exact rather than assumed: a 4:3 deck is still a thing, and guessing 16:9
    would put every overlay box in the wrong place on one. Read from the file
    the renderer just wrote, which costs 24 bytes.
    """
    for render in renders:
        for path in render.values():
            size = _png_size(Path(path))
            if size and size[1]:
                return round(size[0] / size[1], 6)
    return None


def _png_size(path: Path) -> Optional[tuple[int, int]]:
    """A PNG's pixel dimensions, from its IHDR chunk."""
    try:
        with path.open("rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or not head.startswith(b"\x89PNG"):
        return None
    return (
        int.from_bytes(head[16:20], "big"),
        int.from_bytes(head[20:24], "big"),
    )


def _existing_images(directory: Path) -> dict[int, str]:
    if not directory.is_dir():
        return {}
    found: dict[int, str] = {}
    for path in directory.glob("*.png"):
        if path.stem.isdigit():
            found[int(path.stem)] = path.name
    return found


def _image_for(directory: Path, number: str) -> Optional[Path]:
    """One rendered slide, refusing anything that is not a plain number.

    The number arrives from a URL, and joining an arbitrary string onto a path
    is how a local tool starts serving files it was never asked about.
    """
    if not number.isdigit():
        return None
    candidate = directory / f"{int(number)}.png"
    return candidate if candidate.is_file() else None


def _guidelines_dir(root: Path) -> Path:
    return root / "config"


def _resolve_guidelines(root: Path, name: Optional[str]) -> Optional[Path]:
    """Turn a filename from the dropdown back into a path under config/.

    The page only ever offers names this module listed, so the containment
    check is here to catch a bug, not an attacker.
    """
    if not name:
        return None
    directory = _guidelines_dir(root).resolve()
    candidate = (directory / name).resolve()
    if not candidate.is_relative_to(directory) or not candidate.is_file():
        raise _BadRequest(f"no guidelines file named {name}")
    return candidate


def _context(root: Path) -> dict[str, Any]:
    """Everything the page needs to draw itself before the first run."""
    directory = _guidelines_dir(root)
    files: list[str] = []
    if directory.is_dir():
        files = sorted(
            p.name
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in {".yaml", ".yml", ".json"}
        )
    return {
        "version": __version__,
        "root": str(root),
        "guidelines_dir": str(directory),
        "guidelines": files,
        "rules": describe_rules(build_default_rules()),
        "defaults": {
            "model": SUGGESTED_MODEL,
            "cli_model": DEFAULT_MODEL,
            "effort": DEFAULT_EFFORT,
            "batch_size": DEFAULT_BATCH_SIZE,
        },
        "api_key": bool(os.environ.get("GEMINI_API_KEY")),
        # Whether this host can render at all. The deck check degrades without
        # a renderer; the design check cannot run at all, and saying so before
        # a designer uploads 25MB is the difference between a warning and a
        # wasted minute.
        "renderer": available_renderer().available,
    }


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, root: Path) -> None:
        super().__init__(address, handler)
        self.root = root


# Set in the child so it knows not to supervise in turn.
_CHILD_ENV = "FORMATTING_TOOL_RELOAD_CHILD"


def _watched_files() -> list[Path]:
    package = Path(__file__).resolve().parents[1]
    return list(package.rglob("*.py")) + list(STATIC.glob("*"))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def _supervise() -> int:
    """Run the server as a child and restart it whenever the source changes.

    Python imports a module once and never re-reads it, so a server started
    before an edit serves the old code for as long as it runs, silently. That
    is not theoretical: an afternoon of reports came out of a stale process,
    looked like the tool had not changed, and cost an investigation to explain.

    A supervisor rather than `os.execv`, which on Windows re-joins the argument
    list without quoting and tears a path containing a space in half. A
    supervisor rather than `importlib.reload`, which leaves old classes alive
    in other modules' namespaces so half the process is new and half is not.
    """
    import subprocess  # noqa: PLC0415

    command = [sys.executable, "-m", "formatting_tool", *sys.argv[1:]]
    env = {**os.environ, _CHILD_ENV: "1"}

    child = subprocess.Popen(command, env=env)
    stamps = {p: _mtime(p) for p in _watched_files()}
    try:
        while True:
            time.sleep(1.0)
            if child.poll() is not None:
                return child.returncode or 0
            changed = next(
                (p for p, was in stamps.items() if _mtime(p) != was), None
            )
            if changed is None:
                continue
            print(f"\n{changed.name} changed, restarting...", flush=True)
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
            child = subprocess.Popen(command, env=env)
            stamps = {p: _mtime(p) for p in _watched_files()}
    except KeyboardInterrupt:
        print()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except Exception:
                child.kill()
    return 0


def serve(
    port: int = 8000,
    host: str = "127.0.0.1",
    root: Optional[Path] = None,
    open_browser: bool = True,
    reload: bool = False,
) -> int:
    """Run the UI until interrupted. Returns a CLI exit code."""
    if reload and not os.environ.get(_CHILD_ENV):
        return _supervise()

    root = Path(root or Path.cwd()).resolve()

    server = None
    for candidate in range(port, port + 10):
        try:
            server = _Server((host, candidate), _Handler, root)
            break
        except OSError:
            log.debug("port %d is busy", candidate)
    if server is None:
        log.error("no free port in the range %d-%d", port, port + 9)
        return 2

    url = f"http://{host}:{server.server_address[1]}"
    print(f"Deck check UI on {url}")
    print(f"Guidelines read from {_guidelines_dir(root)}")
    print(f"Build: {__version__} from {Path(__file__).resolve().parents[1]}")
    if os.environ.get(_CHILD_ENV):
        print("Reload: on. The server restarts when a source file changes.")
    else:
        print("Reload: OFF. Restart after editing the source, or use --reload.")
    print("Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.shutdown()
        server.server_close()
    return 0
