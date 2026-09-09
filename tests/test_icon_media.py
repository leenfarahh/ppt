"""Media the rebuild cannot identify, and must carry anyway.

An icon out of PowerPoint's own library is not a picture in the sense
python-pptx understands. It is an SVG, with an EMF raster fallback beside it
in `a:blip` and the vector in an `asvg:svgBlip` extension. Pillow identifies
NEITHER of those blobs, so `get_or_add_image_part` raised on both halves of
every library icon a rebuild touched, and the two failures were handled
differently:

- the `svgBlip` matched `_ALTERNATE_TAGS`, so it was detached as an
  "invisible enhancement" -- true of a hi-def duplicate of a photograph, and
  false of the only copy of a vector;
- the `a:blip` did not match, so it was left holding the SOURCE part's
  relationship id, which on the rebuilt slide means another part or nothing.

The result was a broken-image box on the slide -- "The picture can't be
displayed" -- and only on library icons, which is why some icons came through
a rebuild and others did not.

None of that needed identifying. The bytes are a valid part already; what
they need is a partname in the target package and the content type the source
declared, which is what `_carry_media` does.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# A one-path SVG, and the smallest thing that is unmistakably an EMF header.
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    b'<path d="M1 1h14v14H1z" fill="#C00000"/></svg>'
)
EMF = b"\x01\x00\x00\x00" + b"\x00" * 36 + b" EMF" + b"\x00" * 64


def _rel(blob: bytes, content_type: str, partname: str):
    """A relationship shaped like the ones the rebuild walks."""
    return SimpleNamespace(
        is_external=False,
        reltype=(
            "http://schemas.openxmlformats.org/officeDocument/2006/"
            "relationships/image"
        ),
        target_part=SimpleNamespace(
            blob=blob, content_type=content_type, partname=partname
        ),
    )


# --------------------------------------------------------------------------- #
# Why the carry path has to exist
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("blob,label", [(SVG, "svg"), (EMF, "emf")])
def test_python_pptx_cannot_identify_icon_media(blob: bytes, label: str) -> None:
    """The premise, asserted rather than assumed. If a future Pillow learns
    these, the carry path stops being reached and this test says so.
    """
    from formatting_tool.rebuild.builder import _sniffed_type

    assert _sniffed_type(blob) is None, (
        f"Pillow now identifies {label}; the carry path may be redundant"
    )


def test_an_ordinary_png_still_goes_the_normal_way() -> None:
    """The carry path is for what cannot be sniffed, and nothing else."""
    from formatting_tool.rebuild.builder import _sniffed_type

    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    assert _sniffed_type(png) == "image/png"


# --------------------------------------------------------------------------- #
# Carrying it
# --------------------------------------------------------------------------- #

def _target():
    pytest.importorskip("pptx")
    from pptx import Presentation

    prs = Presentation()
    return prs, prs.slides.add_slide(prs.slide_layouts[6]).part


def test_the_svg_is_carried_with_the_type_the_source_declared() -> None:
    from formatting_tool.rebuild.builder import _reimport_image

    _prs, part = _target()

    rid = _reimport_image(
        _rel(SVG, "image/svg+xml", "/ppt/media/image24.svg"), part
    )

    carried = part.rels[rid].target_part
    assert carried.blob == SVG
    assert str(carried.content_type) == "image/svg+xml"
    # A partname the TARGET allocated, not the source's, or two packages both
    # write ppt/media/image24.svg and one silently replaces the other.
    assert str(carried.partname).endswith(".svg")


def test_the_emf_fallback_is_carried_too() -> None:
    """The half that used to be left holding a stale relationship id, which is
    what actually put the broken-image box on the slide."""
    from formatting_tool.rebuild.builder import _reimport_image

    _prs, part = _target()

    rid = _reimport_image(
        _rel(EMF, "image/x-emf", "/ppt/media/image23.emf"), part
    )

    assert part.rels[rid].target_part.blob == EMF
    assert str(part.rels[rid].target_part.content_type) == "image/x-emf"


def test_the_same_icon_twice_is_stored_once() -> None:
    """An icon on twenty slides is one part, as it was on the image path this
    stands in for."""
    from formatting_tool.rebuild.builder import _reimport_image

    _prs, part = _target()
    rel = _rel(SVG, "image/svg+xml", "/ppt/media/image24.svg")

    first = _reimport_image(rel, part)
    second = _reimport_image(rel, part)

    assert part.rels[first].target_part is part.rels[second].target_part


def test_bytes_of_no_declared_type_are_refused() -> None:
    """Copying a declaration is not a decision; inventing one is. With nothing
    to copy this raises and the caller reports the shape, as it does for
    everything else it cannot rebuild faithfully."""
    from formatting_tool.rebuild.builder import MediaTypeMismatch, _reimport_image

    _prs, part = _target()

    with pytest.raises(MediaTypeMismatch):
        _reimport_image(_rel(SVG, "", "/ppt/media/image24.svg"), part)


def test_a_relabelled_blob_is_still_refused() -> None:
    """The guard this sits beside, unchanged: Pillow reads EMF as WMF, and a
    part written as .wmf holding EMF bytes made PowerPoint refuse the whole
    file. A blob the target would label differently is not carried."""
    from formatting_tool.rebuild.builder import MediaTypeMismatch, _reimport_image

    _prs, part = _target()
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    with pytest.raises(MediaTypeMismatch):
        _reimport_image(_rel(png, "image/jpeg", "/ppt/media/image1.jpeg"), part)


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #

def test_a_rebuilt_deck_keeps_its_svg_and_leaves_no_dangling_reference(
    tmp_path: Path,
) -> None:
    """The defect as it was seen: icons arriving as broken-image boxes.

    A broken box is a reference the slide's own rels do not define, so that is
    what is asserted -- on every slide, not just the one with the icon.
    """
    pytest.importorskip("pptx")
    source = Path("strategy&mini.pptx")
    if not source.exists():
        pytest.skip("needs the sample deck with a library icon in it")

    from formatting_tool.rebuild.builder import rebuild

    out = tmp_path / "rebuilt.pptx"
    rebuild("test_master1.pptx", source, out)

    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        assert any(n.lower().endswith(".svg") for n in names), (
            "the icon's vector was dropped"
        )
        types = archive.read("[Content_Types].xml").decode("utf-8", "ignore")
        assert "image/svg+xml" in types, "the SVG part has no declared type"

        for name in names:
            if not re.fullmatch(r"ppt/slides/slide\d+\.xml", name):
                continue
            xml = archive.read(name).decode("utf-8", "ignore")
            rels = archive.read(
                name.replace("slides/", "slides/_rels/") + ".rels"
            ).decode("utf-8", "ignore")
            defined = set(re.findall(r'Id="([^"]+)"', rels))
            used = set(re.findall(r'r:(?:embed|link)="([^"]+)"', xml))
            assert not (used - defined), (
                f"{name} points at {sorted(used - defined)}, which its rels do "
                "not define -- that is the broken-image box"
            )
