"""Carrying the master's values from the run to the apply.

Applying a fix happens minutes after the run and, in the page, in a different
request. The values a proposed fix is checked against have to come from
somewhere, and the obvious somewhere -- read the master again -- is wrong
twice over.

It is wrong in practice: an uploaded master lives in a temp directory, and on
a real run the file was gone by the time the apply asked for it, which turned
a working apply into a traceback. And it is wrong in principle even when the
file survives: a master read a second time may not be the master the report
describes, so a proposal would be checked against values the findings never
came from.

So the run leaves its spec on the report and the apply takes it from there.
"""

from __future__ import annotations

import json

from formatting_tool.models import ValidationReport


def _report() -> ValidationReport:
    return ValidationReport(master="m.pptx", decks=["d.pptx"], generated_at="now")


def test_the_spec_rides_on_the_report() -> None:
    report = _report()
    assert report.spec is None          # always present, never a missing attribute

    report.spec = "the master's values"
    assert report.spec == "the master's values"


def test_the_spec_is_not_serialised() -> None:
    """It is working state, not part of the report. A MasterSpec carries every
    layout and every shape on it, so serialising one would put the whole
    master inside the JSON a designer downloads."""
    report = _report()
    report.spec = "the master's values"

    data = report.to_dict()

    assert "spec" not in data
    json.dumps(data)        # still a plain JSON document


def test_two_reports_do_not_share_a_spec() -> None:
    """`spec` is a bare class attribute so the dataclass leaves it out of the
    fields. Assigning to it has to bind on the instance, not the class."""
    first, second = _report(), _report()

    first.spec = "first master"

    assert second.spec is None
