"""Memoise python-pptx's tag-name resolver.

`pptx.oxml.ns.qn` turns "p:cSld" into "{...presentationml...}cSld". It is a
pure function of its argument and it builds a NamespacePrefixedTag object on
every call, and python-pptx calls it constantly: reading one 197KB master with
7,884 shapes measured 459,897 calls against 88 distinct tags. Caching it made
that read 43% faster, 7.2s to 4.1s, and it is on the path of everything that
touches a deck -- the reader, the rules, the rebuild.

Safe because the function is pure and the key space is closed: the 88 tags are
the OOXML vocabulary python-pptx knows, and a tag it has never seen is a cache
miss that computes the same answer it would have computed anyway.

Applied by hand rather than left to python-pptx because it is a third-party
library and this is a local decision. It is idempotent, and it is applied at
the point the library is first imported rather than at package import, so
`formatting-tool --help` still does not pay for loading python-pptx at all.
"""

from __future__ import annotations

import functools
import logging
import sys

log = logging.getLogger(__name__)

_APPLIED = False


def apply() -> None:
    """Patch `qn` in every python-pptx module that holds a reference to it.

    Every reference, because most of the library does `from ..ns import qn`
    and so holds its own name for the original function; patching only the
    defining module would leave nearly all of the calls uncached.
    """
    global _APPLIED
    if _APPLIED:
        return
    try:
        from pptx.oxml import ns
    except Exception:       # python-pptx absent; nothing to speed up
        return

    if getattr(ns.qn, "__wrapped__", None) is not None:
        _APPLIED = True     # already cached, by us or by a future release
        return

    original = ns.qn
    cached = functools.lru_cache(maxsize=1024)(original)
    patched = 0
    for name, module in list(sys.modules.items()):
        if name.startswith("pptx") and getattr(module, "qn", None) is original:
            module.qn = cached
            patched += 1
    ns.qn = cached
    _APPLIED = True
    log.debug("cached python-pptx's qn in %d module(s)", patched)
