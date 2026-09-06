"""Extract brand rules from a reference into a reviewable guidelines file.

The flow, and the reason it is a separate command rather than part of validate:

    brand book PDF                    approved deck (.pptx)
        |                                 |
        v                                 v
    extractor.py                      extractor.py
    one Gemini call, whole PDF        no model call; the theme and the
    as a file part                    slides are already structured
        |  raw reading, every             |  an empty shell, everything
        |  value carrying its quote       |  marked MISSING
        v                                 |
    mapping.py                            |
    discard anything unquoted,            |
    unitless or malformed                 |
        |  AUTHORED / MISSING             |
        v                                 v
    inference.py    fill what is still missing from the deck, mark it INFERRED
        |
        v
    writer.py       annotated YAML, for a designer to review and correct

The two sources are not equivalent. A brand book states rules; a deck only
demonstrates them, and a master drifts. Everything read from a deck is
INFERRED, and the report says so rather than calling it a brand violation.

Extraction is lossy either way, so the output is a file a person signs off on
once, not something recomputed on every validate run.
"""

from .extractor import (
    BrandBookError,
    ExtractConfig,
    ExtractionResult,
    extract_from_deck,
    extract_from_pdf,
    extract_guidelines,
)
from .inference import InferenceResult, infer_gaps
from .mapping import MappingResult, Rejection, guidelines_from_extraction
from .schema import BRANDBOOK_SCHEMA
from .writer import write_guidelines_yaml

__all__ = [
    "BRANDBOOK_SCHEMA",
    "BrandBookError",
    "ExtractConfig",
    "ExtractionResult",
    "InferenceResult",
    "MappingResult",
    "Rejection",
    "extract_from_deck",
    "extract_from_pdf",
    "extract_guidelines",
    "guidelines_from_extraction",
    "infer_gaps",
    "write_guidelines_yaml",
]
