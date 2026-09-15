"""A model call that never comes back must not take the run with it.

On a real 22-slide deck the review layer sent seven batches and six came back.
The seventh never did, and the run stopped there: no error, no report, no way
to tell a stalled connection from a model still thinking. Two unbounded waits
were stacked to make that possible, and both are closed here.
"""

from __future__ import annotations

import os

import pytest

from formatting_tool.ai.gemini import REQUEST_TIMEOUT_MS, build_client
from formatting_tool.pipeline import _AI_LAYER_DEADLINE_S


def test_a_request_cannot_run_for_ever() -> None:
    """The SDK's own default is None, which means forever."""
    from google.genai import types

    assert types.HttpOptions.model_fields["timeout"].default is None
    assert REQUEST_TIMEOUT_MS > 0


def test_the_client_is_built_with_that_timeout(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")

    client = build_client()

    assert client._api_client._http_options.timeout == REQUEST_TIMEOUT_MS


def test_the_layer_waits_longer_than_one_request() -> None:
    """A batch that times out is retried, so the layer's deadline has to sit
    above the request's or a retry could never finish."""
    assert _AI_LAYER_DEADLINE_S > REQUEST_TIMEOUT_MS / 1000


def test_the_deadline_is_well_above_a_real_run() -> None:
    """The slowest measured here is a 22-slide deck at 153s. The deadline is
    for a run that has stopped, not for one that is merely long."""
    assert _AI_LAYER_DEADLINE_S >= 10 * 153


# --------------------------------------------------------------------------- #
# A busy service is not a failed call
# --------------------------------------------------------------------------- #

def test_a_busy_server_is_retried_not_raised() -> None:
    """The 503 this API returns says so in words: "this model is currently
    experiencing high demand. Spikes in demand are usually temporary. Please
    try again later." It was being treated as permanent, and on a real deck
    one batch of seven took one and two slides silently lost their findings."""
    from formatting_tool.ai.gemini import _RETRY_CODES

    assert 503 in _RETRY_CODES
    assert {500, 502, 504} <= _RETRY_CODES
    # A client error is the caller's fault and asking again cannot help.
    assert not ({400, 401, 403, 404} & _RETRY_CODES)


def test_there_are_several_attempts_over_a_useful_span() -> None:
    from formatting_tool.ai.gemini import _RETRY_WAITS

    assert len(_RETRY_WAITS) >= 3
    assert sum(_RETRY_WAITS) >= 60        # long enough to outlast a spike
