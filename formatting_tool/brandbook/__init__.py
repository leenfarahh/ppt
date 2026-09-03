"""Extract brand rules from a reference PDF into a reviewable guidelines file.

The flow, and the reason it is a separate command rather than part of validate:

    brand book PDF
        |
        v
    extractor.py    one Gemini call, whole PDF as a file part
        |  raw reading, every value carrying its evidence quote
        v
    mapping.py      discard anything unquoted, unitless or malformed
        |  BrandGuidelines with AUTHORED / MISSING provenance
        v
    inference.py    fill the gaps from the master deck, mark them INFERRED
        |
        v
    writer.py       annotated YAML, for a designer to review and correct

Extraction from a brand book is lossy, so the output is a file a person signs
off on once, not something recomputed on every validate run.
"""

from .extractor import (
    BrandBookError,
    ExtractConfig,
    ExtractionResult,
    extract_from_pdf,
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
    "extract_from_pdf",
    "guidelines_from_extraction",
    "infer_gaps",
    "write_guidelines_yaml",
]
