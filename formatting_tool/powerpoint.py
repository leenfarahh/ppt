"""The one PowerPoint this process drives, and the one thread allowed to.

Both halves of the tool need desktop PowerPoint: rendering a slide to a PNG,
and applying a master by assigning CustomLayout so PowerPoint's own
placeholder matching moves the content. They must not each keep their own
instance, because there is only one to keep.

PowerPoint's Application is a COM singleton: every Dispatch, in every process
on the machine, reaches the same POWERPNT.EXE. Two things follow, and between
them they decide the shape of everything here.

Quitting after a job poisons the next one. `Quit` starts a shutdown that
outlives the call returning, so the Dispatch that follows attaches to an
instance already on its way down and hands back objects that fail the moment
they are used -- "PowerPoint could not open the file", or a Presentation that
raises on `.Slides`. A preview renders the deck before and after in one pass,
which is precisely that pattern, and it failed about half the time: the before
image appeared, the after one did not. Applying a master and then previewing
the result is the same pattern again.

And the instance is not ours to quit in the first place. It may be the
designer's own PowerPoint, with unsaved work open in it.

So: one instance, made on first use and kept; driven from a single thread that
owns its COM apartment, because an interface pointer is not usable from a
thread other than the one it was made on and the web UI answers on
ThreadingHTTPServer; and quit at exit only if we were the ones who started it.
The single thread serialises concurrent work as a side effect, which is what we
want anyway -- there is only one PowerPoint to go around.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


def available() -> bool:
    """Desktop PowerPoint reachable for COM automation on this host."""
    try:
        import win32com.client  # noqa: F401, PLC0415
    except ImportError:
        return False
    return os.name == "nt"


# 0x80080005 CO_E_SERVER_EXEC_FAILURE. Windows would not launch the server.
# Against an Office singleton the ordinary cause is a launch already in
# flight: the second activation collides with the first and is refused.
_EXEC_FAILURE = -2146959355

# A cold PowerPoint takes something over twenty seconds to become usable on a
# laptop with the deck on OneDrive. A warm one answers in well under a second,
# so the wait is paid once a session and almost never in full.
_READY_TIMEOUT_S = 90
_READY_POLL_S = 0.5


def _ready(app) -> bool:
    """Whether this instance can actually serve a call yet.

    PowerPoint enters the Running Object Table early in its startup, well
    before it can answer, so an instance that exists is not yet an instance
    that works. What makes the gap hard to recognise is how win32com reports
    it: dynamic dispatch asks GetIDsOfNames for the member, a starting
    PowerPoint refuses, and `__getattr__` has nowhere to go but AttributeError.
    The symptom is `AttributeError: PowerPoint.Application.Presentations`,
    which reads like a missing method on the wrong kind of object rather than
    like a slow start, and sends whoever reads it looking in the wrong place.

    Touching Presentations is the cheapest call that settles it.
    """
    try:
        app.Presentations.Count
    except Exception:
        return False
    return True


def _connect():
    """The PowerPoint already running, or a new one, once it can answer.

    Returns the instance and whether we started it, because that is what says
    whether we are allowed to quit it later.

    Nothing unready is handed back. A half-started PowerPoint fails every call
    made against it, and those failures land a long way from here -- a render
    that produced no images, a master that could not be applied so the deck
    was rebuilt instead, notes that never became comments. Each one is caught
    and logged by the step that hit it, so the run finishes and reports
    success while quietly having done less. Waiting the few seconds PowerPoint
    needs costs less than any one of those.
    """
    import win32com.client  # noqa: PLC0415

    started = False
    last: Exception | None = None
    deadline = time.monotonic() + _READY_TIMEOUT_S

    while True:
        app = None
        try:
            app = win32com.client.GetActiveObject("PowerPoint.Application")
        except Exception as exc:          # not in the table: nothing running yet
            last = exc

        if app is None:
            try:
                app = win32com.client.Dispatch("PowerPoint.Application")
                started = True
            except Exception as exc:
                args = getattr(exc, "args", ())
                if not (args and args[0] == _EXEC_FAILURE):
                    raise                 # a real fault; retrying only delays it
                # Someone is already starting PowerPoint, possibly us on an
                # earlier pass round this loop. Wait for that one instead of
                # racing it with another launch.
                last = exc
                app = None

        if app is not None and _ready(app):
            return app, started

        if time.monotonic() >= deadline:
            break
        time.sleep(_READY_POLL_S)

    raise RuntimeError(
        f"PowerPoint would not start for automation within {_READY_TIMEOUT_S}s. "
        "Almost always this is Document Recovery: a PowerPoint that crashed or "
        "was force-quit leaves a recovery entry behind, and every later "
        "/AUTOMATION launch tries to show the recovery pane, cannot show it "
        "without a window, and dies without registering. Nothing clears it "
        "except a person: open PowerPoint normally, dismiss the Document "
        "Recovery pane, close it again, then re-run. End any windowless "
        "POWERPNT.EXE in Task Manager first, since those are the failed "
        f"launches and they keep colliding with new ones. (Last error: {last}.)"
    ) from last


class _PowerPointHost:
    """The one thread allowed to talk to PowerPoint, and the queue into it."""

    # Long enough for the largest deck anyone has rendered, short enough that
    # a COM call wedged forever fails the request instead of the whole server.
    TIMEOUT_S = 1800

    def __init__(self) -> None:
        self._requests: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._broken: Optional[Exception] = None

    def call(self, work, timeout: Optional[float] = None):
        """Run `work(app)` on the PowerPoint thread; raise or return as it did.

        `timeout` is how long this particular caller is prepared to wait. The
        default is the half hour a batch job can afford; something with a
        person watching it should say so and get an error it can show them
        instead of a page that never comes back.
        """
        self._start()
        if self._broken is not None:
            raise self._broken
        done = threading.Event()
        box: dict = {}
        self._requests.put((work, box, done))
        waited = self.TIMEOUT_S if timeout is None else timeout
        if not done.wait(waited):
            raise TimeoutError(
                f"PowerPoint did not answer within {waited:.0f}s. It may be "
                "showing a dialog, or busy with another deck"
            )
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._serve, name="powerpoint", daemon=True
                )
                self._thread.start()
                atexit.register(self._stop)

    def _stop(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._requests.put(None)
            thread.join(timeout=30)

    def _serve(self) -> None:
        import pythoncom  # noqa: PLC0415

        try:
            pythoncom.CoInitialize()
        except Exception as exc:  # nothing can run; do not leave callers waiting
            self._broken = exc
            self._drain(exc)
            return

        app = None
        ours = False
        try:
            while True:
                item = self._requests.get()
                if item is None:
                    return
                work, box, done = item
                try:
                    if app is None:
                        app, ours = _connect()
                    try:
                        box["value"] = work(app)
                    except Exception:
                        # A failure mid-conversation can mean the instance is
                        # gone -- the designer closed PowerPoint under us -- so
                        # let the reference go and reconnect once. Note what is
                        # deliberately absent: we do not quit it first. Quit
                        # then re-Dispatch is the very race this module exists
                        # to avoid, and against a singleton it buys nothing,
                        # since the reconnect returns the same instance anyway.
                        log.debug("reconnecting to PowerPoint", exc_info=True)
                        app, ours = _connect()
                        box["value"] = work(app)
                except Exception as exc:
                    box["error"] = exc
                    app = None
                finally:
                    done.set()
        finally:
            # The only quit in the module, and only for an instance we started.
            if app is not None and ours:
                quietly(lambda: app.Quit())
            quietly(lambda: pythoncom.CoUninitialize())

    def _drain(self, exc: Exception) -> None:
        while True:
            try:
                item = self._requests.get_nowait()
            except queue.Empty:
                return
            if item is None:
                return
            _work, box, done = item
            box["error"] = exc
            done.set()


def quietly(call) -> None:
    """Run a COM call for its side effect and swallow whatever it does.

    Pass a lambda, never a bound method. `quietly(deck.Close)` looks up the
    member before this function is entered, and against PowerPoint that lookup
    is itself a COM call that can fail -- so the failure this was written to
    swallow escapes past it. `quietly(lambda: deck.Close())` does not.
    """
    try:
        call()
    except Exception:
        log.debug("ignoring an error while shutting PowerPoint down", exc_info=True)


# PowerPoint rejects calls while it is busy (RPC_E_CALL_REJECTED /
# RPC_E_SERVERCALL_RETRYLATER). Transient, not a real failure: a call fails
# once and the identical call passes straight after. Left unhandled, a slide
# is silently lost to a race.
_TRANSIENT = (-2147418111, -2147417846)
_ATTEMPTS = 4
_BACKOFF_SEC = 0.4


def _transient(exc: BaseException) -> bool:
    """Whether this is PowerPoint being busy rather than a real fault.

    Two shapes, and the second is the one that misleads. A rejected call
    normally arrives as a com_error carrying one of the HRESULTs above. A
    rejected *name lookup* does not: win32com's dynamic dispatch asks
    GetIDsOfNames for the member, swallows whatever comes back and reports no
    such attribute. What that reads like is `AttributeError: Open.Close` -- a
    Presentation that answered `.Slides` a moment ago and now appears not to
    have `.Close` -- which sends whoever sees it looking for a wrong object
    rather than a busy server. Same cause as the wait in `_ready`, same cure.
    """
    if isinstance(exc, AttributeError):
        return True
    args = getattr(exc, "args", ())
    return bool(args) and args[0] in _TRANSIENT


def retry(call, attempts: int = _ATTEMPTS):
    """Run a COM call, retrying only while PowerPoint is busy.

    `call` is re-run whole, so pass a lambda rather than a bound method: the
    member lookup is itself a COM call and is itself one of the things that
    fails. Any other failure raises immediately, because retrying a real error
    only delays it.
    """
    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            if not _transient(exc):
                raise
            last = exc
            time.sleep(_BACKOFF_SEC * (attempt + 1))
    raise last          # type: ignore[misc]


def close(presentation) -> bool:
    """Close a deck, waiting out a busy PowerPoint, and say whether it closed.

    Not housekeeping. A deck left open turns the next Open of the same path
    into "PowerPoint could not open the file", so a close that did not happen
    is charged to whatever runs next rather than to whoever left it. Callers
    unwinding from another error ignore the answer, because that error is the
    interesting one; callers with nothing else wrong read it.
    """
    try:
        retry(lambda: presentation.Close())
        return True
    except Exception:
        log.debug("could not close a presentation", exc_info=True)
        return False


def run(work, timeout: Optional[float] = None):
    """Run `work(app)` on the PowerPoint thread; raise or return as it did."""
    return _HOST.call(work, timeout)


_HOST = _PowerPointHost()
