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
import tempfile
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import rmtree
from typing import Any, Optional

from .. import __version__
from ..ai.client import AIConfig, AIValidationError, DEFAULT_EFFORT, DEFAULT_MODEL
from ..ai.payload import DEFAULT_BATCH_SIZE
from ..extract import DeckReadError
from ..guidelines import GuidelinesError
from ..pipeline import RunConfig, run
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
        else:
            self._send_json(404, {"error": f"no route for {route}"})

    def do_POST(self) -> None:
        route = self.path.split("?", 1)[0]
        if route != "/api/validate":
            self._send_json(404, {"error": f"no route for {route}"})
            return
        code, payload = self._validate()
        self._send_json(code, payload)

    # -- the one real endpoint --------------------------------------------- #

    def _validate(self) -> tuple[int, dict[str, Any]]:
        try:
            return 200, self._run_pipeline()
        except _BadRequest as exc:
            return 400, {"error": str(exc)}
        except (DeckReadError, GuidelinesError, AIValidationError) as exc:
            # The CLI exits 2 on these and prints the message alone; they are
            # explained errors, so the page gets the same treatment.
            return 400, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a local tool wants the trace
            log.exception("unhandled error during validation")
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
        try:
            config = RunConfig(
                master=_spill(workdir / "master", masters[0]),
                # Each deck gets its own directory: two uploads may share a
                # filename, and the report identifies decks by name.
                decks=[_spill(workdir / f"deck{i}", p) for i, p in enumerate(decks)],
                guidelines=_resolve_guidelines(self.server.root, options.get("guidelines")),
                use_ai=bool(options.get("use_ai")),
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
            return {
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


def serve(
    port: int = 8000,
    host: str = "127.0.0.1",
    root: Optional[Path] = None,
    open_browser: bool = True,
) -> int:
    """Run the UI until interrupted. Returns a CLI exit code."""
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
