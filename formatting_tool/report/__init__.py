"""Stage 5: merge both layers and write the report."""

from .merge import merge_issues, summarize, summarize_report
from .reader import ReportError, load_report
from .writers import write_json, write_markdown, write_text

__all__ = [
    "ReportError",
    "load_report",
    "merge_issues",
    "summarize",
    "summarize_report",
    "write_json",
    "write_markdown",
    "write_text",
]
