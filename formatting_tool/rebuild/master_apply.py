"""Apply a master by letting PowerPoint do it.

    copy slide -> apply the master's layout to the COPY -> delete the original

one slide at a time, in place, never batching.

WHY THIS EXISTS ALONGSIDE `builder.py`. That module rebuilds a deck by writing
OOXML: it adds a slide on the target layout, copies text into the matching
placeholders, and transplants everything else shape by shape. But assigning
`CustomLayout` runs PowerPoint's own placeholder-matching engine, and that is
the thing which actually moves a slide's content into the new layout's
placeholders. Re-pointing the layout relationship by hand moves no content: it
looks right in the XML and wrong on screen.

It also re-serialises nothing, and that is where the XML route kept drawing
blood. Two bugs in one sitting, both making PowerPoint refuse the whole file
with no part named: a media blob relabelled because python-pptx re-sniffs it
through Pillow and Pillow calls EMF "WMF", and an extension left empty when a
reference inside it was removed. Neither breaks OPC integrity, so no amount of
zip or relationship checking finds them. Handing the file to PowerPoint removes
that entire class of failure, because we serialise nothing.

The cost is desktop PowerPoint and Windows, with no cloud path. So this is
preferred where it can run and `builder.py` is the fallback where it cannot,
which is the only arrangement that does not take a working feature away from a
machine without Office.

The per-slide ordering is deliberate. Duplicating the whole deck and then
restyling the copies leaves it at 2n slides mid-run, so a failure halfway
through strands a half-branded double-length file for somebody to untangle by
hand. Per slide keeps it at n slides at every moment, and a failure leaves the
remaining slides simply untouched.

Verified against desktop PowerPoint:

    pres.Designs.Load(master)             adds the foreign master WITHOUT
                                          restyling the existing slides
    dup = pres.Slides(i).Duplicate()(1)   the copy lands at index i+1
    dup.CustomLayout = target             PowerPoint's placeholder matching runs
    pres.Slides(i).Delete()               the copy shifts back into slot i

After each iteration slot i holds the finished slide and slot i+1 the next
untouched original, so a plain 1..n walk is correct and the count never changes.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import powerpoint

log = logging.getLogger(__name__)

# PowerPoint rejects calls while it is busy (RPC_E_CALL_REJECTED /
# SERVERCALL_RETRYLATER). Transient, not a real failure: a call fails once and
# the identical call passes straight after. Left unhandled, a slide is silently
# lost to a race.
_TRANSIENT = (-2147418111, -2147417846)
_ATTEMPTS = 4
_BACKOFF_SEC = 0.4

# COM failures restated as something a person can do. The raw tuple names the
# HRESULT and nothing else, so whoever reads it has no next step.
_ADVICE = {
    # 0x80010100 RPC_E_SYS_CALL_FAILED
    -2147417856: (
        "PowerPoint automation is in a bad state on this machine, usually a "
        "leftover instance from a run that was interrupted. It was started "
        "with /AUTOMATION and has no window, so closing PowerPoint normally "
        "does not touch it: end POWERPNT.EXE in Task Manager, then try again."
    ),
    # 0x80040154 REGDB_E_CLASSNOTREG
    -2147221164: (
        "Windows has no PowerPoint registered for automation. Applying a "
        "master runs PowerPoint's own placeholder matching, so it needs "
        "desktop PowerPoint installed on this machine."
    ),
    # 0x80080005 CO_E_SERVER_EXEC_FAILURE
    -2146959355: (
        "PowerPoint would not start. Check Task Manager for a POWERPNT.EXE "
        "with no window, and otherwise open PowerPoint yourself once to clear "
        "whatever update or repair prompt it is waiting on."
    ),
    # 0x808A0001. PowerPoint refusing the file, not the automation: measured
    # on a real deck that python-pptx reads happily and PowerPoint will not
    # open at all, before or after a clean round-trip through python-pptx. The
    # XML route can still restyle it, which is the case `route="auto"` exists
    # for, so this is worth naming rather than reporting as a COM fault.
    -2138238975: (
        "PowerPoint will not open this deck, which is a property of the file "
        "rather than of the automation: it refuses it opened by hand too. The "
        "master can still be applied by rebuilding the file instead, which is "
        "what route='auto' falls back to."
    ),
}


@dataclass
class SlideOutcome:
    number: int                 # 1-based, as the report counts slides
    target_layout: Optional[str]
    applied: bool
    detail: str = ""


@dataclass
class MasterApplyResult:
    outcomes: list[SlideOutcome] = field(default_factory=list)
    fatal: Optional[str] = None
    # Slides still on a design other than the one applied, and how many
    # masters the output therefore carries.
    #
    # The consequence nobody can otherwise see: a slide that could not be
    # restyled keeps the deck's ORIGINAL design alive, so the output has two
    # masters and PowerPoint's master view lists the original first. A designer
    # opens it, sees a master without the new guides, and reads that as "the
    # master was not applied" when it was, onto the other one.
    stragglers: list[int] = field(default_factory=list)
    masters: int = 1

    @property
    def applied(self) -> int:
        return 0 if self.fatal else sum(1 for o in self.outcomes if o.applied)

    @property
    def failed(self) -> list[SlideOutcome]:
        return [o for o in self.outcomes if not o.applied]


def available() -> bool:
    """Whether this host can apply a master at all."""
    return powerpoint.available()


def apply_master(
    deck: str | Path,
    master: str | Path,
    out: str | Path,
    plans: dict[int, str],
) -> MasterApplyResult:
    """Restyle `deck` onto `master`'s layouts and write the result to `out`.

    `plans` maps a 1-based slide number to the layout name it should land on.
    Taken as DECIDED rather than re-derived here, so the layouts somebody
    approved are the ones applied; re-planning inside the apply would silently
    discard every pick. `rebuild.matcher` is what makes those picks.

    Never raises. A host that cannot drive PowerPoint comes back with `fatal`
    set and nothing written, and the caller falls back to the XML rebuild.
    """
    deck, master, out = Path(deck), Path(master), Path(out)
    if not available():
        return MasterApplyResult(
            fatal=(
                "applying a master through PowerPoint needs Windows with "
                "desktop Office and pywin32; there is no cloud path for it"
            )
        )
    try:
        return powerpoint.run(lambda app: _drive(app, deck, master, out, plans))
    except Exception as exc:
        return MasterApplyResult(fatal=advice(exc))


def advice(exc: Exception) -> str:
    """One sentence a person can act on, with the raw error kept.

    An unknown HRESULT falls through to the error itself rather than to a
    guess: a wrong instruction wastes more time than no instruction.
    """
    args = getattr(exc, "args", ())
    known = _ADVICE.get(args[0] if args else None)
    if not known:
        return f"PowerPoint automation failed: {exc}"
    return f"{known} (The error itself: {exc}.)"


def _retry(call):
    """Run a COM call, retrying only the transient busy/rejected errors.

    Any other failure raises immediately: retrying a real error delays it.
    """
    last: Optional[Exception] = None
    for attempt in range(_ATTEMPTS):
        try:
            return call()
        except Exception as exc:
            args = getattr(exc, "args", ())
            if not (args and args[0] in _TRANSIENT):
                raise
            last = exc
            time.sleep(_BACKOFF_SEC * (attempt + 1))
    raise last          # type: ignore[misc]


def _drive(app: Any, deck: Path, master: Path, out: Path, plans: dict[int, str]):
    """The whole conversation with PowerPoint, on the thread that owns it."""
    result = MasterApplyResult()

    # A working copy, so a failure halfway cannot leave the caller's deck
    # part-restyled. PowerPoint edits in place and saves where it is told; the
    # input is never opened for writing.
    staging = Path(tempfile.mkdtemp(prefix="formatting-tool-apply-"))
    working = staging / deck.name
    shutil.copyfile(deck, working)

    presentation = None
    try:
        presentation = _retry(lambda: app.Presentations.Open(
            str(working.resolve()), False, False, False
        ))
        try:
            design = _retry(
                lambda: presentation.Designs.Load(str(master.resolve()))
            )
        except Exception as exc:
            return MasterApplyResult(
                fatal=f"could not load the master's design: {exc}"
            )

        layouts = _layouts_of(design)
        result.outcomes = _restyle(presentation, layouts, plans)
        result.stragglers = _stragglers(presentation, design)
        _drop_unused_designs(presentation)
        result.masters = _design_count(presentation)

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()        # SaveAs will not overwrite silently
        _retry(lambda: presentation.SaveAs(str(out.resolve())))
        presentation.Close()
        presentation = None
        return result
    finally:
        if presentation is not None:
            powerpoint.quietly(presentation.Close)
        shutil.rmtree(staging, ignore_errors=True)


def _layouts_of(design: Any) -> dict[str, Any]:
    master = design.SlideMaster
    return {
        master.CustomLayouts(i).Name: master.CustomLayouts(i)
        for i in range(1, master.CustomLayouts.Count + 1)
    }


def _restyle(presentation: Any, layouts: dict[str, Any], plans: dict[int, str]):
    """The copy/apply/delete walk. One slide at a time, count never changes."""
    outcomes: list[SlideOutcome] = []
    total = int(presentation.Slides.Count)

    for number in range(1, total + 1):
        wanted = plans.get(number)
        if wanted is None:
            outcomes.append(SlideOutcome(
                number, None, False, "no layout was chosen for this slide"
            ))
            continue
        target = layouts.get(wanted)
        if target is None:
            outcomes.append(SlideOutcome(
                number, wanted, False,
                f"the master has no layout named {wanted!r}",
            ))
            continue
        try:
            index = number                      # COM is 1-based
            copy = _retry(lambda: presentation.Slides(index).Duplicate()(1))
            _retry(lambda: setattr(copy, "CustomLayout", target))
            _retry(lambda: presentation.Slides(index).Delete())
            outcomes.append(SlideOutcome(number, wanted, True))
        except Exception as exc:
            outcomes.append(
                SlideOutcome(number, wanted, False, f"apply failed: {exc}")
            )
            # Leave the deck consistent: if the duplicate survived the failure,
            # drop it rather than shipping a doubled slide.
            try:
                if int(presentation.Slides.Count) > total:
                    presentation.Slides(number + 1).Delete()
            except Exception:
                log.debug("could not remove a stranded duplicate", exc_info=True)
    return outcomes


def _stragglers(presentation: Any, design: Any) -> list[int]:
    """Slides not on the applied design, read BEFORE any design is deleted.

    Deleting a design shifts the indexes, so asking afterwards compares
    against a number that has moved.
    """
    try:
        applied = design.Index
        return [
            i
            for i in range(1, int(presentation.Slides.Count) + 1)
            if presentation.Slides(i).Design.Index != applied
        ]
    except Exception:
        log.debug("could not read which slides kept another design", exc_info=True)
        return []


def _drop_unused_designs(presentation: Any) -> None:
    """A design carrying no slides is dead weight in the file."""
    try:
        used = {
            presentation.Slides(i).Design.Index
            for i in range(1, int(presentation.Slides.Count) + 1)
        }
        for i in range(int(presentation.Designs.Count), 0, -1):
            if i not in used:
                powerpoint.quietly(lambda i=i: presentation.Designs(i).Delete())
    except Exception:
        log.debug("could not tidy unused designs", exc_info=True)


def _design_count(presentation: Any) -> int:
    try:
        return int(presentation.Designs.Count)
    except Exception:
        return 1
