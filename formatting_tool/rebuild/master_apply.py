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
from . import pictures

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

    # Written onto the working copy before PowerPoint sees it, because a
    # picture placeholder's frame and its cropped-to shape belong to the
    # layout it is about to stop pointing at. Doing it to the file rather than
    # through automation is what makes it work for a `a:custGeom` mask, which
    # COM cannot describe at all -- see `rebuild.pictures`. A no-op on a deck
    # with no picture placeholders, which then reaches PowerPoint untouched.
    pictures.freeze_file(working)

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
            # Read before the swap, put back after. See `_photographs`.
            photographs = _photographs(copy)
            _retry(lambda: setattr(copy, "CustomLayout", target))
            _restore(copy, photographs, _has_picture_slot(target))
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


# --------------------------------------------------------------------------- #
# Photographs across the swap -- the fallback
# --------------------------------------------------------------------------- #
#
# A BACKSTOP, not the mechanism. `rebuild.pictures.freeze_file` has already
# written the frame and the geometry onto the working copy by the time
# PowerPoint opens it, and a frozen photograph is no longer a placeholder, so
# on a deck python-pptx can read `_photographs` finds nothing and this does
# nothing. What is left for it is the deck python-pptx cannot open and
# PowerPoint can, where the freeze logged a warning and gave up. There it
# recovers the frame in full and the geometry where it is a preset; a
# `a:custGeom` mask is beyond COM either way, which is why the freeze moved
# onto the file in the first place.
#
# What PowerPoint gets wrong here, from a real deck: a portrait cropped to a
# circle came out square and hard against the left margin, while the
# decorative arcs drawn around it stayed put. The arcs are ordinary
# autoshapes, so they carry their own position. The portrait was a picture
# PLACEHOLDER, and its circle and its position were never on the slide at all
# -- they were on the old layout. Assigning `CustomLayout` re-runs
# inheritance against the new layout, which has nothing to say about that
# shape, so the frame collapses to the origin and the geometry to a plain
# rectangle.
#
# The two halves are not restored on the same terms, because they are not the
# same kind of thing:
#
#   the cropped-to shape    always put back. A circular portrait is the
#                           writer's intent about the content, not a property
#                           of the template it happened to be built in.
#   the frame               put back only where the new layout offers no
#                           picture placeholder of its own. Where it does,
#                           PowerPoint has just placed the photo in the
#                           approved spot and overriding that would throw
#                           away the point of applying a master.

# msoShapeType, MsoAutoShapeType and ppPlaceholderType values, spelled out
# rather than imported so this module keeps working without the PowerPoint
# type library generated.
_MSO_PLACEHOLDER = 14
_MSO_PICTURE = 13
_MSO_SHAPE_MIXED = -2
_MSO_NOT_PRIMITIVE = 138        # what COM answers for geometry it is inheriting
_PICTURE_SLOTS = frozenset({18, 9})     # ppPlaceholderPicture, ppPlaceholderBitmap


def _photographs(slide: Any) -> dict[int, tuple[float, float, float, float, int]]:
    """Every placeholder on the slide that currently holds a picture.

    Keyed by `Shape.Id`, which is per-slide and survives the `CustomLayout`
    assignment because that repositions shapes rather than recreating them. A
    shape whose Id does not survive is simply not restored, which leaves the
    old behaviour rather than moving the wrong shape.

    `Left` and friends need no special handling: COM already answers with the
    effective value, so a placeholder inheriting its frame reports the
    layout's numbers. Geometry does not work that way -- see
    `_designed_geometry` and `_designed_for`.
    """
    designed = _designed_geometry(slide)
    found: dict[int, tuple[float, float, float, float, int]] = {}
    for shape in _shapes(slide):
        try:
            if int(shape.Type) != _MSO_PLACEHOLDER:
                continue
            if int(shape.PlaceholderFormat.ContainedType) != _MSO_PICTURE:
                continue
            frame = (
                float(shape.Left), float(shape.Top),
                float(shape.Width), float(shape.Height),
            )
            geometry = int(shape.AutoShapeType)
            name = str(shape.Name)
            shape_id = int(shape.Id)
        except Exception:
            # An empty picture placeholder has no ContainedType and raises.
            # There is no photograph in it to preserve.
            continue
        if geometry in (_MSO_NOT_PRIMITIVE, _MSO_SHAPE_MIXED):
            geometry = _designed_for(frame, name, designed)
        found[shape_id] = (*frame, geometry)
    return found


def _designed_geometry(slide: Any) -> list[tuple[tuple, str, int]]:
    """The preset each picture slot on the OLD layout is cropped to.

    Measured, because it is not where you would look for it. Asked about the
    slide's own shape COM answers `msoShapeNotPrimitive`, which is correct and
    useless: that shape has no geometry of its own, and having none is the
    whole bug. Asked about the layout placeholder it inherits from, COM
    answers `msoShapeOval`. So the circle has to be read off the layout, while
    the slide is still pointing at it.

    Returned as (frame, name, preset) per slot. Slots with no preset of their
    own are left out: they have nothing to tell us.
    """
    slots: list[tuple[tuple, str, int]] = []
    try:
        layout = slide.CustomLayout
    except Exception:
        return slots
    for shape in _shapes(layout):
        try:
            if int(shape.PlaceholderFormat.Type) not in _PICTURE_SLOTS:
                continue
            preset = int(shape.AutoShapeType)
            frame = (
                float(shape.Left), float(shape.Top),
                float(shape.Width), float(shape.Height),
            )
            name = str(shape.Name)
        except Exception:
            continue
        if preset not in (_MSO_NOT_PRIMITIVE, _MSO_SHAPE_MIXED):
            slots.append((frame, name, preset))
    return slots


def _designed_for(frame: tuple, name: str, slots: list) -> int:
    """Which of the old layout's picture slots this photograph inherits from.

    By frame first, and this is the correction that made it work on a real
    deck. A placeholder inheriting its position reports the layout's own
    numbers, so the two agree exactly, and that holds however PowerPoint has
    renamed the shape -- which it does. Dropping a photo into a slot called
    "Picture Placeholder 3" leaves a shape called "Picture 5", so matching on
    the name misses in precisely the case this exists for. Matching on the
    frame was already sitting in data that had to be read anyway.

    Then by name, which separates two slots sitting on top of each other.
    Then, where the layout has exactly one picture slot, that one: there is
    nothing else the photograph could be inheriting from.
    """
    for slot_frame, _slot_name, preset in slots:
        if _same_frame(slot_frame, frame):
            return preset
    for _slot_frame, slot_name, preset in slots:
        if slot_name == name:
            return preset
    if len(slots) == 1:
        return slots[0][2]
    return _MSO_NOT_PRIMITIVE


def _same_frame(one: tuple, other: tuple, tolerance: float = 0.5) -> bool:
    """Equal to within half a point.

    The two numbers come from the same place -- one read through the shape
    that is inheriting it, one off the slot itself -- so in practice they
    agree exactly. The tolerance is insurance against a float round trip
    through COM, and half a point is far below the distance between two
    genuinely different slots.
    """
    return len(one) == len(other) and all(
        abs(a - b) <= tolerance for a, b in zip(one, other)
    )


def _restore(slide: Any, photographs: dict, keep_frame_from_layout: bool) -> None:
    """Put the cropped-to shape back, and the frame where the layout has none.

    Order matters and cost a wrong result once: setting `AutoShapeType`
    re-fits the shape and moves it, so the geometry goes back first and the
    frame after it, never the other way round.
    """
    if not photographs:
        return
    for shape in _shapes(slide):
        try:
            before = photographs.get(int(shape.Id))
        except Exception:
            continue
        if before is None:
            continue
        left, top, width, height, geometry = before
        # Every write retried and none of them fatal. Retried because a
        # transient rejection here would leave one photograph square while its
        # neighbours came out round, which is worse to chase than a consistent
        # fault. Not fatal because the restyle itself has already succeeded,
        # and reporting the slide as failed over a photograph would discard it.
        try:
            if geometry != _MSO_NOT_PRIMITIVE:
                if geometry != int(_retry(lambda: shape.AutoShapeType)):
                    _retry(lambda: setattr(shape, "AutoShapeType", geometry))
            if not keep_frame_from_layout:
                _retry(lambda: setattr(shape, "Left", left))
                _retry(lambda: setattr(shape, "Top", top))
                _retry(lambda: setattr(shape, "Width", width))
                _retry(lambda: setattr(shape, "Height", height))
        except Exception:
            log.debug(
                "could not restore a photograph's frame or shape", exc_info=True
            )


def _has_picture_slot(layout: Any) -> bool:
    """Whether the target layout has a picture placeholder of its own.

    False when it cannot be read: the fallback that keeps the source's frame
    is the one that loses nothing, because a photo left where the designer put
    it is wrong only cosmetically, and a photo at the origin is wrong visibly.
    """
    for shape in _shapes(layout):
        try:
            kind = int(shape.PlaceholderFormat.Type)
        except Exception:
            continue
        if kind in _PICTURE_SLOTS:
            return True
    return False


def _shapes(container: Any):
    """A 1-based COM shape collection as an iterator.

    Every read here is against a live PowerPoint that can refuse any single
    call, so a shape that will not come back is skipped rather than taking the
    slide down with it.
    """
    try:
        count = int(_retry(lambda: container.Shapes.Count))
    except Exception:
        log.debug("could not count the shapes on a slide", exc_info=True)
        return
    for i in range(1, count + 1):
        try:
            yield _retry(lambda: container.Shapes(i))
        except Exception:
            continue


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
