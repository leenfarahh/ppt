"""Stage 5: merge both layers and write the report."""

from .merge import merge_issues, summarize
from .writers import write_json, write_markdown, write_text

__all__ = [
    "merge_issues",
    "summarize",
    "write_json",
    "write_markdown",
    "write_text",
]
