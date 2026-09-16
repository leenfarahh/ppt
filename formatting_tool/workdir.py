"""Where the tool puts the files it is working on.

The default is the system temporary directory, which is right until something
else is managing it. On this machine something is: Windows Storage Sense and
the remote-management agents an IT department installs both delete from there
on a schedule, and neither asks whether a process is using the files. Every
session directory went mid-run once -- an uploaded deck, its renders, and the
deck that had been written from it.

So it can be pointed somewhere nobody sweeps: set FORMATTING_TOOL_WORKDIR to a
directory of your own.

THIS LIVES IN A MODULE OF ITS OWN because it was in `web.server`, and the
render directory therefore did not get it. That is the directory that actually
went: a run lost `Slide20.PNG` out of `formatting-tool-render-y0nikrcx` after
sixteen batches had come back, and setting the variable would not have helped,
because `render_deck` called `mkdtemp` with no `dir` at all. An escape hatch
that misses the file that vanishes is not an escape hatch. Both callers now
read the same answer from here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

WORKDIR_ENV = "FORMATTING_TOOL_WORKDIR"


def workroot() -> Optional[str]:
    """The directory to make working directories inside, or None for default.

    None means `tempfile` picks, which is the system temporary directory and
    the right answer where nothing is sweeping it.

    A path that cannot be made is a warning and the default, because a run
    that goes in the wrong directory is better than one that will not start.
    """
    root = os.environ.get(WORKDIR_ENV, "").strip()
    if not root:
        return None
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "%s is set to %r, which cannot be used (%s); working files will go "
            "in the system temporary directory", WORKDIR_ENV, root, exc,
        )
        return None
    return root
