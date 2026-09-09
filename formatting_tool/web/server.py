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
from shutil import rmtree
from typing import Any, Optional

from .. import __version__
from ..ai.client import AIConfig, AIValidationError, DEFAULT_EFFORT, DEFAULT_MODEL
from ..ai.payload import DEFAULT_BATCH_SIZE
from ..apply import ApplyError, apply_fixes, fixable
from ..extract import DeckReadError
from ..guidelines import GuidelinesError
from ..pipeline import RunConfig, run
from ..render import render_deck
from ..report.reader import _issue as _issue_from_dict
from ..rules import build_default_rules, describe_rules

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 256 * 1024 * 1024

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
    created: float = field(default_factory=time.time)

    @property
    def fixed(self) -> Path:
        return self.directory / f"fixed-{self.deck.name}"

    def cleanup(self) -> None:
        rmtree(self.directory, ignore_errors=True)


_SESSIONS: dict[str, _Session] = {}
_SESSION_LOCK = threading.Lock()


def _remember(session: _Session) -> None:
    with _SESSION_LOCK:
        _SESSIONS[session.id] = session
        stale = [
            key
            for key, value in _SESSIONS.items()
            if time.time() - value.created > SESSION_TTL_S
        ]
        # Oldest first, so a burst of checks does not fill the disk with decks
        # nobody is going to apply anything to.
        while len(_SESSIONS) - len(stale) > MAX_SESSIONS:
            oldest = min(
                (k for k in _SESSIONS if k not in stale),
                key=lambda k: _SESSIONS[k].created,
            )
            stale.append(oldest)
        for key in stale:
            _SESSIONS.pop(key).cleanup()


def _session(session_id: str) -> _Session:
    with _SESSION_LOCK:
        found = _SESSIONS.get(session_id)
    if found is None:
        raise _BadRequest(
            "that check has expired; run it again before applying fixes"
        )
    return found


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
        elif route == "/api/context":
            self._send_json(200, _context(self.server.root))
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
        elif route == "/api/preview":
            code, payload = self._guarded(self._run_preview)
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
                selected=chosen,
                master=session.master if rebuild_too else None,
                spec=session.spec,
            )
            elapsed = time.perf_counter() - started

        return {
            "session": session.id,
            # Listed apart from the rest: a removal is the one change with
            # nothing left on the slide to check it against.
            "removed": [
                {"id": o.issue.id, "slide": o.issue.slide, "detail": o.detail,
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
                    {"id": o.issue.id, "detail": o.detail,
                     "applied": o.applied, "slide": o.issue.slide,
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
                {"id": o.issue.id, "detail": o.detail, "slide": o.issue.slide,
                 "shape": o.issue.shape, "rule_id": o.issue.rule_id,
                 # Where to draw the change on the rendered slides. See
                 # FixOutcome.
                 "box_before": o.box_before, "box_after": o.box_after}
                for o in result.applied
            ],
            "skipped": [
                {"id": o.issue.id, "detail": o.detail, "slide": o.issue.slide}
                for o in result.skipped
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

        before = _render_into(session.deck, session.directory / "before")
        after: dict[int, str] = {}
        if session.fixed.exists():
            after = _render_into(session.fixed, session.directory / "after")

        numbers = sorted(set(before) | set(after))
        if slides:
            numbers = [n for n in numbers if n in slides]

        return {
            "session": session.id,
            "renderer_ok": bool(before),
            # The rendered shape of a slide, so the page can reserve the right
            # space before the images arrive. Without it the overlay boxes are
            # positioned against a collapsed <img> and scatter outside it.
            "aspect": _aspect_of(before, after),
            "slides": [
                {
                    "slide": n,
                    "before": f"/api/preview/{session.id}/before/{n}" if n in before else None,
                    "after": f"/api/preview/{session.id}/after/{n}" if n in after else None,
                }
                for n in numbers
            ],
        }

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

        directory = session.directory / parts[3]
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

        workdir = Path(tempfile.mkdtemp(prefix="formatting-tool-ui-"))
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
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise _BadRequest("empty request body")
        if length > MAX_UPLOAD:
            raise _BadRequest(
                f"upload is {length / 1e6:.0f} MB; the limit is {MAX_UPLOAD / 1e6:.0f} MB"
            )
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


def _render_into(deck: Path, directory: Path) -> dict[int, str]:
    """Render a deck into `directory`, reusing what is already there.

    Rendering drives PowerPoint and costs seconds, and the before-images do not
    change between one apply and the next, so they are rendered once.
    """
    existing = _existing_images(directory)
    if existing:
        return existing

    images = render_deck(deck)
    if not images:
        log.info("no preview for %s: %s", deck.name, images.reason)
        return {}
    directory.mkdir(parents=True, exist_ok=True)
    out: dict[int, str] = {}
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
