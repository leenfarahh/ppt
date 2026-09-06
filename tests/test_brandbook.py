"""Tests for brand book extraction.

None of these need an API key. The one end-to-end test stubs the client, which
is the whole reason `extract_from_pdf` takes one: the interesting behaviour is
in what the mapping keeps and what the inference fills, not in the HTTP call.
"""

from __future__ import annotations

import json
from pathlib import Path

from formatting_tool.ai.schema import to_gemini_schema
from formatting_tool.brandbook import (
    BRANDBOOK_SCHEMA,
    ExtractConfig,
    extract_from_deck,
    extract_from_pdf,
    extract_guidelines,
    guidelines_from_extraction,
    infer_gaps,
    write_guidelines_yaml,
)
from formatting_tool.guidelines import load_guidelines
from formatting_tool.models import (
    BrandGuidelines,
    DeckProfile,
    Geometry,
    ParagraphProfile,
    Provenance,
    RunProfile,
    ShapeProfile,
    SlideProfile,
    TextRole,
)

QUOTE = "Headlines are set in Inter Tight at 32 to 40 point."


# --------------------------------------------------------------------------- #
# Fixtures, built in memory
# --------------------------------------------------------------------------- #

def _extraction(**overrides) -> dict:
    """A plausible reading of a brand book, with everything evidenced."""
    data = {
        "brand_name": "Test Brand",
        "palette": [
            {
                "label": "Deep Navy",
                "hex": "#1F2A44",
                "spec_as_written": None,
                "evidence": "Deep Navy #1F2A44 is our primary colour.",
                "page": 4,
            }
        ],
        "latin_fonts": [
            {"name": "Inter Tight", "usage": "headlines", "evidence": QUOTE, "page": 9}
        ],
        "arabic_fonts": [],
        "roles": [
            {
                "role": "title",
                "fonts": ["Inter Tight"],
                "min_size": 32,
                "max_size": 40,
                "size_unit": "pt",
                "colors": ["#1F2A44"],
                "max_lines": 2,
                "required": True,
                "evidence": QUOTE,
                "page": 9,
            }
        ],
        "logo": {
            "min_width": 25,
            "clear_space": 6,
            "unit": "mm",
            "allowed_corners": ["tl"],
            "required_on_first_slide": True,
            "required_on_every_slide": None,
            "evidence": "The logo is never smaller than 25mm wide.",
            "page": 12,
        },
        "safe_margins": {
            "top": None,
            "right": None,
            "bottom": None,
            "left": None,
            "unit": None,
            "evidence": None,
            "page": None,
        },
        "typography": {
            "max_orphan_words": None,
            "min_widow_chars": None,
            "max_title_lines": 2,
            "allow_hyphenation": False,
            "evidence": "Never hyphenate. Titles run to at most two lines.",
            "page": 14,
        },
        "notes": [
            {
                "rule": "Section dividers reverse to white on Deep Navy.",
                "evidence": "Section dividers reverse out of the primary colour.",
                "page": 16,
            }
        ],
        "not_specified": ["safe_margins"],
    }
    data.update(overrides)
    return data


def _master() -> DeckProfile:
    """A master deck whose captions are consistently 11pt and logo top-left."""
    slides = []
    for number in range(1, 5):
        caption = ShapeProfile(
            shape_id=10 + number,
            name=f"Caption {number}",
            shape_type="PLACEHOLDER",
            geometry=Geometry(left_in=0.6, top_in=6.5, width_in=5.0, height_in=0.4),
            placeholder_type="BODY",
            role=TextRole.CAPTION,
            text="A caption",
            paragraphs=[
                ParagraphProfile(
                    text="A caption",
                    runs=[RunProfile(text="A caption", font_name="Inter", size_pt=11.0)],
                )
            ],
        )
        logo = ShapeProfile(
            shape_id=20 + number,
            name="Logo",
            shape_type="PICTURE",
            geometry=Geometry(left_in=0.6, top_in=0.5, width_in=1.2, height_in=0.4),
            is_picture=True,
            image_sha1="abc123",
        )
        slides.append(
            SlideProfile(number=number, layout_name="Content", shapes=[caption, logo])
        )
    return DeckProfile(
        path="master.pptx",
        width_in=13.333,
        height_in=7.5,
        slides=slides,
        theme_fonts={"major": "Inter Tight", "minor": "Inter"},
    )


# --------------------------------------------------------------------------- #
# Mapping: what survives the gate
# --------------------------------------------------------------------------- #

def test_evidenced_values_are_kept_and_marked_authored() -> None:
    result = guidelines_from_extraction(_extraction(), source="book.pdf")
    g = result.guidelines

    assert g.name == "Test Brand"
    assert g.palette == {"Deep Navy": "1F2A44"}
    assert g.allowed_fonts == ["Inter Tight"]
    assert g.roles["title"].min_size_pt == 32.0
    assert g.provenance_of("palette") is Provenance.AUTHORED
    assert g.provenance_of("roles.title.min_size_pt") is Provenance.AUTHORED
    assert result.evidence["roles.title.min_size_pt"] == QUOTE
    assert result.pages["roles.title.min_size_pt"] == 9


def test_unevidenced_values_are_discarded() -> None:
    """The anti-invention gate: a value with no quote is not a rule."""
    raw = _extraction(
        palette=[
            {
                "label": "Invented Teal",
                "hex": "#00A0A0",
                "spec_as_written": None,
                "evidence": None,
                "page": None,
            }
        ]
    )
    result = guidelines_from_extraction(raw, source="book.pdf")

    assert result.guidelines.palette == {}
    assert result.guidelines.provenance_of("palette") is Provenance.MISSING
    assert any("no supporting quote" in r.reason for r in result.rejections)


def test_millimetres_convert_to_inches() -> None:
    result = guidelines_from_extraction(_extraction(), source="book.pdf")
    # 25mm and 6mm, to four decimal places.
    assert result.guidelines.logo.min_width_in == round(25 / 25.4, 4)
    assert result.guidelines.logo.clear_space_in == round(6 / 25.4, 4)


def test_pixels_and_unitless_measurements_are_rejected() -> None:
    raw = _extraction()
    raw["logo"] = {**raw["logo"], "unit": "px", "min_width": 96}
    result = guidelines_from_extraction(raw, source="book.pdf")

    assert result.guidelines.logo.min_width_in is None
    assert any("px has no physical size" in r.reason for r in result.rejections)

    raw = _extraction()
    raw["logo"] = {**raw["logo"], "unit": None}
    result = guidelines_from_extraction(raw, source="book.pdf")
    assert any("no unit" in r.reason for r in result.rejections)


def test_inverted_size_range_drops_both_ends() -> None:
    raw = _extraction()
    raw["roles"][0] = {**raw["roles"][0], "min_size": 40, "max_size": 32}
    result = guidelines_from_extraction(raw, source="book.pdf")

    spec = result.guidelines.roles["title"]
    assert spec.min_size_pt is None and spec.max_size_pt is None
    assert any("exceeds maximum" in r.reason for r in result.rejections)


def test_pantone_only_colour_is_surfaced_not_guessed() -> None:
    raw = _extraction()
    raw["palette"][0] = {
        **raw["palette"][0],
        "hex": None,
        "spec_as_written": "PANTONE 2965 C",
    }
    result = guidelines_from_extraction(raw, source="book.pdf")

    assert result.guidelines.palette == {}
    assert any("PANTONE 2965 C" in str(r) for r in result.rejections)


def test_unspecified_margins_are_missing_not_defaulted() -> None:
    """A default margin would have the safe-margin rule test an invented frame."""
    result = guidelines_from_extraction(_extraction(), source="book.pdf")
    for side in ("top_in", "right_in", "bottom_in", "left_in"):
        assert (
            result.guidelines.provenance_of(f"safe_margins.{side}")
            is Provenance.MISSING
        )


# --------------------------------------------------------------------------- #
# Inference: filling the gaps from the master deck
# --------------------------------------------------------------------------- #

def test_inference_fills_gaps_and_marks_them() -> None:
    guidelines = guidelines_from_extraction(_extraction(), source="book.pdf").guidelines
    inference = infer_gaps(guidelines, _master())

    assert guidelines.roles["caption"].min_size_pt == 11.0
    assert guidelines.provenance_of("roles.caption.min_size_pt") is Provenance.INFERRED
    assert "roles.caption.min_size_pt" in inference.inferred
    assert "11.0pt on 4 of 4 runs" in inference.inferred["roles.caption.min_size_pt"]

    assert guidelines.logo.required_on_every_slide is True
    assert guidelines.provenance_of("logo.required_on_every_slide") is Provenance.INFERRED


def test_inference_never_overwrites_an_authored_value() -> None:
    guidelines = guidelines_from_extraction(_extraction(), source="book.pdf").guidelines
    before = guidelines.roles["title"].min_size_pt

    infer_gaps(guidelines, _master())

    assert guidelines.roles["title"].min_size_pt == before
    assert guidelines.provenance_of("roles.title.min_size_pt") is Provenance.AUTHORED
    # The master's logo is 1.2in wide, but the book states 25mm; the book wins.
    assert guidelines.logo.min_width_in == round(25 / 25.4, 4)


def test_inference_needs_consistency_not_just_presence() -> None:
    """One observation is not a convention."""
    master = _master()
    master.slides = master.slides[:1]        # a single 11pt caption
    guidelines = guidelines_from_extraction(_extraction(), source="book.pdf").guidelines
    infer_gaps(guidelines, master)

    assert "caption" not in guidelines.roles


# --------------------------------------------------------------------------- #
# Writer: the reviewable artefact
# --------------------------------------------------------------------------- #

def test_yaml_round_trips_through_the_loader(tmp_path: Path) -> None:
    """The writer's output must be loadable, or the review step is a dead end."""
    result = guidelines_from_extraction(_extraction(), source="book.pdf")
    inference = infer_gaps(result.guidelines, _master())
    document = write_guidelines_yaml(
        result.guidelines,
        evidence=result.evidence,
        pages=result.pages,
        inference=inference,
        rejections=result.rejections,
        master="master.pptx",
        unspecified=result.unspecified,
    )

    path = tmp_path / "brand.yaml"
    path.write_text(document, encoding="utf-8")
    reloaded = load_guidelines(path)

    original = result.guidelines
    assert reloaded.name == original.name
    assert reloaded.palette == original.palette
    assert reloaded.allowed_fonts == original.allowed_fonts
    assert reloaded.roles["title"].min_size_pt == original.roles["title"].min_size_pt
    assert reloaded.logo.min_width_in == original.logo.min_width_in
    assert reloaded.typography.allow_hyphenation is False
    assert reloaded.tuning.grid_support == original.tuning.grid_support
    # Provenance survives, so a validate run can still hedge inferred findings.
    assert reloaded.provenance == original.provenance
    assert reloaded.paths_with(Provenance.INFERRED)


def test_yaml_annotates_every_provenance_kind(tmp_path: Path) -> None:
    # A second palette entry with no quote, so there is a discarded reading for
    # the appendix to report. A real brand book always yields a few.
    raw = _extraction()
    raw["palette"].append(
        {
            "label": "Unsourced Teal",
            "hex": "#00A0A0",
            "spec_as_written": None,
            "evidence": None,
            "page": None,
        }
    )
    result = guidelines_from_extraction(raw, source="book.pdf")
    inference = infer_gaps(result.guidelines, _master())
    document = write_guidelines_yaml(
        result.guidelines,
        evidence=result.evidence,
        pages=result.pages,
        inference=inference,
        rejections=result.rejections,
    )

    assert "authored p9:" in document
    assert "inferred:" in document
    assert "MISSING - not specified" in document
    assert "REVIEW BEFORE USE" in document
    # Discarded readings are stated, not silently dropped.
    assert "Readings discarded during extraction" in document


def test_writer_quotes_hex_so_yaml_does_not_read_a_comment(tmp_path: Path) -> None:
    result = guidelines_from_extraction(_extraction(), source="book.pdf")
    document = write_guidelines_yaml(result.guidelines)
    path = tmp_path / "brand.yaml"
    path.write_text(document, encoding="utf-8")

    assert load_guidelines(path).palette == {"Deep Navy": "1F2A44"}


# --------------------------------------------------------------------------- #
# Schema and the end-to-end path
# --------------------------------------------------------------------------- #

def test_schema_has_no_optional_keys() -> None:
    def check(node: dict, where: str = "root") -> None:
        if node.get("type") == "object":
            assert set(node["required"]) == set(node["properties"]), where
            assert node["additionalProperties"] is False, where
            for name, child in node["properties"].items():
                check(child, f"{where}.{name}")
        if "items" in node:
            check(node["items"], f"{where}[]")

    check(BRANDBOOK_SCHEMA)


def test_schema_survives_translation_to_geminis_subset() -> None:
    translated = to_gemini_schema(BRANDBOOK_SCHEMA)
    assert translated["type"] == "OBJECT"
    assert "additionalProperties" not in json.dumps(translated)

    palette_item = translated["properties"]["palette"]["items"]
    assert palette_item["properties"]["hex"]["nullable"] is True
    assert palette_item["properties"]["label"]["type"] == "STRING"


def test_extract_from_pdf_end_to_end(tmp_path: Path) -> None:
    """The whole command path with the network stubbed out."""
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    class _Usage:
        prompt_token_count = 4321
        candidates_token_count = 210
        thoughts_token_count = 90
        cached_content_token_count = 0

    class _Part:
        text = json.dumps(_extraction())
        thought = False

    class _Response:
        candidates = [type("C", (), {"content": type("N", (), {"parts": [_Part()]})()})()]
        prompt_feedback = None
        usage_metadata = _Usage()

    class _Models:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return _Response()

    class _Client:
        def __init__(self) -> None:
            self.models = _Models()

    client = _Client()
    result = extract_from_pdf(pdf, ExtractConfig(effort="low"), client=client)

    assert result.guidelines.palette == {"Deep Navy": "1F2A44"}
    assert result.input_tokens == 4321
    assert result.output_tokens == 300
    assert result.unspecified == ["safe_margins"]

    # The PDF went as a file part alongside the instruction, and the schema was
    # translated for Gemini before the call.
    sent = client.models.calls[0]
    assert len(sent["contents"]) == 2
    assert sent["config"].response_mime_type == "application/json"
    assert sent["config"].response_schema["type"] == "OBJECT"


def test_non_pdf_reference_file_is_refused(tmp_path: Path) -> None:
    docx = tmp_path / "book.docx"
    docx.write_bytes(b"not a pdf")
    try:
        extract_from_pdf(docx)
    except Exception as exc:
        assert "reads PDFs only" in str(exc)
    else:
        raise AssertionError("a .docx reference file should be refused")


def test_extract_guidelines_refuses_an_unsupported_reference(tmp_path: Path) -> None:
    """The dispatcher names both accepted forms, not just the PDF."""
    docx = tmp_path / "book.docx"
    docx.write_bytes(b"not a pdf")
    try:
        extract_guidelines(docx)
    except Exception as exc:
        assert ".pdf brand book" in str(exc)
        assert ".pptx approved deck" in str(exc)
    else:
        raise AssertionError("a .docx reference file should be refused")


def test_extract_from_deck_starts_everything_missing(tmp_path: Path) -> None:
    """A deck states nothing, so nothing arrives authored.

    The value of the deck path is entirely in what inference makes of it, and
    that is only sound if extraction hands it a blank slate: an AUTHORED value
    here would be a rule nobody wrote.
    """
    import pytest

    pptx = pytest.importorskip("pptx")
    deck = tmp_path / "approved.pptx"
    pptx.Presentation().save(str(deck))

    result = extract_from_deck(deck)

    assert result.guidelines.paths_with(Provenance.AUTHORED) == []
    assert result.guidelines.provenance
    assert set(result.guidelines.provenance.values()) == {Provenance.MISSING.value}
    assert result.input_tokens == 0 and result.output_tokens == 0


def test_palette_is_inferred_from_the_theme() -> None:
    """The colour scheme is the one brand value a deck states unambiguously."""
    master = DeckProfile(
        path="master.pptx",
        width_in=13.333,
        height_in=7.5,
        theme_colors={"accent1": "1F2A44", "accent2": "C8A951"},
    )
    guidelines = BrandGuidelines(
        provenance={"palette": Provenance.MISSING.value}
    )

    result = infer_gaps(guidelines, master)

    assert guidelines.palette == {"accent1": "1F2A44", "accent2": "C8A951"}
    assert guidelines.provenance_of("palette") is Provenance.INFERRED
    assert "palette" in result.inferred


def test_authored_palette_survives_inference() -> None:
    """An authored palette is never overwritten by the theme behind it."""
    master = DeckProfile(
        path="master.pptx",
        width_in=13.333,
        height_in=7.5,
        theme_colors={"accent1": "FF0000"},
    )
    guidelines = BrandGuidelines(
        palette={"navy": "1F2A44"},
        provenance={"palette": Provenance.AUTHORED.value},
    )

    infer_gaps(guidelines, master)

    assert guidelines.palette == {"navy": "1F2A44"}
    assert guidelines.provenance_of("palette") is Provenance.AUTHORED


# --------------------------------------------------------------------------- #
# Tuning reaches the rules
# --------------------------------------------------------------------------- #

def test_tuning_changes_rule_behaviour() -> None:
    """The constants that used to be hardcoded are now configurable."""
    from formatting_tool.extract.master_spec import derive_master_spec
    from formatting_tool.rules import RuleContext, run_rules
    from formatting_tool.rules.space import OverlapRule

    def deck_with_overlap(gap: float) -> DeckProfile:
        def box(name: str, left: float) -> ShapeProfile:
            return ShapeProfile(
                shape_id=hash(name) % 1000,
                name=name,
                shape_type="TEXT_BOX",
                geometry=Geometry(left_in=left, top_in=1.0, width_in=2.0, height_in=1.0),
                text="text",
                paragraphs=[ParagraphProfile(text="text")],
            )

        return DeckProfile(
            path="d.pptx",
            width_in=13.333,
            height_in=7.5,
            slides=[
                SlideProfile(
                    number=1,
                    shapes=[box("A", 1.0), box("B", 1.0 + 2.0 - gap)],
                )
            ],
        )

    deck = deck_with_overlap(gap=0.05)   # 0.05in x 1in = 0.05 sq in of overlap

    strict = BrandGuidelines(name="strict")
    strict.tuning.min_overlap_in2 = 0.01
    loose = BrandGuidelines(name="loose")
    loose.tuning.min_overlap_in2 = 0.5

    def findings(guidelines: BrandGuidelines) -> int:
        spec = derive_master_spec(deck, guidelines)
        return len(run_rules(RuleContext(deck=deck, spec=spec), [OverlapRule()]))

    assert findings(strict) == 1
    assert findings(loose) == 0
