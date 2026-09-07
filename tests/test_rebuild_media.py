"""Two ways the rebuild produced a file PowerPoint would not open.

Both were found the same way: the UI's after-preview showed "not rendered" for
every slide, the renderer's own error was "PowerPoint could not open the file"
with no part named, and bisecting the package down to one slide and then to one
shape pointed at a picture. Neither breaks OPC integrity, so no amount of zip
or relationship checking finds them: the package is well-formed and the content
is wrong.

Both tests work on the XML and the helpers directly. A fixture deck carrying a
think-cell OLE object and a saturation layer is what exposed them, and that is
a client file.
"""

from __future__ import annotations

import struct

from lxml import etree

from formatting_tool.rebuild.builder import (
    MediaTypeMismatch,
    _detach,
    _reimport_image,
)

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
A14 = "http://schemas.microsoft.com/office/drawing/2010/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# The real shape of it, from the deck that broke: the reference sits four
# levels down a chain that exists only to carry it.
PICTURE = f"""<p:blipFill xmlns:p="x" xmlns:a="{A}" xmlns:r="{R}">
  <a:blip r:embed="rId9">
    <a:extLst>
      <a:ext uri="{{BEBA8EAE-BF5A-486C-A8C5-ECC9F3942E4B}}">
        <a14:imgProps xmlns:a14="{A14}">
          <a14:imgLayer r:embed="rId10">
            <a14:imgEffect><a14:saturation sat="33000"/></a14:imgEffect>
          </a14:imgLayer>
        </a14:imgProps>
      </a:ext>
    </a:extLst>
  </a:blip>
</p:blipFill>"""


def _find(root, local):
    return next(e for e in root.iter() if etree.QName(e).localname == local)


# --------------------------------------------------------------------------- #
# An emptied extension
# --------------------------------------------------------------------------- #

def test_dropping_a_layered_reference_takes_its_extension_with_it() -> None:
    """Removing the imgLayer alone leaves `a14:imgProps` empty, which its
    schema forbids. PowerPoint rejects the whole file for it and names no
    part; on a real deck one of these cost all five slides."""
    root = etree.fromstring(PICTURE)

    _detach(_find(root, "imgLayer"))

    assert not [e for e in root.iter() if etree.QName(e).localname == "imgProps"]
    assert not [e for e in root.iter() if etree.QName(e).localname == "ext"]
    # The picture itself survives: only the enhancement was dropped.
    assert _find(root, "blip").get(f"{{{R}}}embed") == "rId9"


def test_an_empty_extension_list_is_left_in_place() -> None:
    """`a:extLst` with no `a:ext` is legal, so there is nothing to chase
    further and no reason to risk touching the blip."""
    root = etree.fromstring(PICTURE)

    _detach(_find(root, "imgLayer"))

    assert [e for e in root.iter() if etree.QName(e).localname == "extLst"]


def test_a_reference_outside_an_extension_loses_only_itself() -> None:
    """Walking up removing emptied parents outside an extension does more harm
    than good: it emptied a fill on another slide and moved the corruption
    instead of fixing it. Those parents are required content, not payload.
    """
    xml = f"""<p:blipFill xmlns:p="x" xmlns:a="{A}" xmlns:r="{R}">
      <a:blip r:embed="rId9"/>
    </p:blipFill>"""
    root = etree.fromstring(xml)

    _detach(_find(root, "blip"))

    assert len(root) == 0            # the blip went
    assert etree.QName(root).localname == "blipFill"   # its parent did not


# --------------------------------------------------------------------------- #
# A relabelled media part
# --------------------------------------------------------------------------- #

class _Part:
    def __init__(self, blob, content_type):
        self.blob = blob
        self.content_type = content_type


class _Rel:
    def __init__(self, part):
        self.target_part = part


class _Target:
    def __init__(self):
        self.added = []

    def get_or_add_image_part(self, stream):
        self.added.append(stream.read())
        return object(), "rId7"


def _emf() -> bytes:
    """A minimal EMF: header record, bounds, frame, and the " EMF" signature.

    Pillow's WMF reader accepts anything opening `01 00 00 00` and answers
    "WMF" for all of it, which is the whole bug. Built here rather than lifted
    out of the client deck that exposed it.
    """
    header = struct.pack("<II", 1, 108)                  # EMR_HEADER, size
    header += struct.pack("<iiii", 0, 0, 1000, 1000)     # rclBounds
    header += struct.pack("<iiii", 0, 0, 1000, 1000)     # rclFrame
    header += b" EMF"                                    # dSignature
    header += struct.pack("<I", 0x10000)                 # nVersion
    return header + b"\x00" * (108 - len(header))


EMF = _emf()


def test_a_blob_the_target_would_retype_is_refused() -> None:
    """python-pptx re-sniffs every blob through Pillow, and Pillow's WMF
    reader accepts EMF too and calls it WMF. An EMF picture came back typed
    image/x-wmf and was written as image8.wmf; PowerPoint read the extension,
    tried to parse EMF as WMF, and refused the file."""
    target = _Target()
    rel = _Rel(_Part(EMF, "application/vnd.openxmlformats-officedocument.oleObject"))

    try:
        _reimport_image(rel, target)
    except MediaTypeMismatch as exc:
        assert "oleObject" in str(exc)
    else:
        raise AssertionError("a relabelled blob was imported")

    # Checked before anything is added, because get_or_add_image_part creates
    # the relationship as well as the part: raising afterwards would leave the
    # mislabelled part in the package, and content types are global.
    assert target.added == []


def test_a_blob_that_keeps_its_type_is_imported() -> None:
    """The other side, so the guard cannot refuse everything."""
    png = (b"\x89PNG\r\n\x1a\n"
           b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
           b"\x1f\x15\xc4\x89"
           b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
           b"\x00\x00\x00\x00IEND\xaeB`\x82")
    target = _Target()

    rid = _reimport_image(_Rel(_Part(png, "image/png")), target)

    assert rid == "rId7"
    assert target.added == [png]


def test_a_source_with_no_declared_type_is_left_to_the_importer() -> None:
    """Nothing to compare against is not a mismatch. The re-import will raise
    on its own if the bytes are unreadable, and the caller already handles
    that by dropping the shape and reporting it."""
    target = _Target()

    rid = _reimport_image(_Rel(_Part(EMF, None)), target)

    assert rid == "rId7"
