"""The Claude client every AI call goes through, with the network stubbed out.

What is under test is what a request is built from and how an answer or a
failure is read back, since that is the part the rest of the tool depends on:
the answer is JSON matching a schema, thinking depth follows the effort the
caller asked for, the system prompt is cached, and a spent account fails fast
while a busy one is waited out.
"""

from __future__ import annotations

import json

import pytest

from formatting_tool.ai import claude
from formatting_tool.ai.claude import (
    AIValidationError,
    Exhausted,
    RateLimited,
    Truncated,
    effort_for,
    generate_json,
    to_claude_schema,
)


class _Text:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Message:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [_Text(text)]
        self.stop_reason = stop_reason
        self.stop_details = None
        self.usage = None


class _Client:
    """`client.beta.messages.stream(**request)` as a context manager."""

    def __init__(self, *answers) -> None:
        self.calls: list[dict] = []
        self._answers = list(answers)
        client = self

        class _Stream:
            def __enter__(self):
                answer = client._answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return self.message

        class _Messages:
            def stream(self, **kwargs):
                client.calls.append(kwargs)
                stream = _Stream()
                if client._answers and not isinstance(client._answers[0], Exception):
                    stream.message = client._answers[0]
                return stream

        self.beta = type("B", (), {"messages": _Messages()})()


SMALL = {
    "type": "object",
    "properties": {"n": {"type": "integer", "minimum": 0}},
    "required": ["n"],
}


def _ask(client, **kwargs):
    base = dict(model="claude-opus-5", contents="go", system_instruction="be brief",
                schema=SMALL, thinking_budget=16384, max_output_tokens=20000)
    base.update(kwargs)
    return generate_json(client, **base)


def test_the_answer_is_constrained_to_the_schema() -> None:
    client = _Client(_Message('{"n": 3}'))

    data, _ = _ask(client)

    assert data == {"n": 3}
    sent = client.calls[0]
    schema = sent["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert "minimum" not in schema["properties"]["n"]
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"]["effort"] == "high"
    assert sent["fallbacks"] == "default"


def test_the_system_prompt_is_cached() -> None:
    client = _Client(_Message('{"n": 1}'))
    _ask(client)
    [block] = client.calls[0]["system"]
    assert block["text"] == "be brief"
    assert block["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("budget, effort", [
    (2048, "low"), (8192, "medium"), (16384, "high"), (24576, "xhigh"), (32768, "max"),
])
def test_the_thinking_budget_reads_back_as_an_effort(budget, effort) -> None:
    assert effort_for(budget) == effort


def test_a_schema_past_the_grammar_limit_is_stated_instead() -> None:
    """Forty nullable fields: more unions than structured output compiles."""
    wide = {
        "type": "object",
        "properties": {f"f{i}": {"type": ["string", "null"]} for i in range(40)},
        "required": [f"f{i}" for i in range(40)],
    }
    client = _Client(_Message('Here it is:\n```json\n{"f0": null}\n```'))

    data, _ = _ask(client, schema=wide)

    assert data == {"f0": None}
    sent = client.calls[0]
    assert "format" not in sent["output_config"]
    assert "ANSWER FORMAT" in sent["system"][0]["text"]


def test_a_cut_off_answer_keeps_its_text() -> None:
    client = _Client(_Message('{"shapes": [{"a": 1}, {"b"', stop_reason="max_tokens"))
    with pytest.raises(Truncated) as caught:
        _ask(client)
    assert caught.value.text.startswith('{"shapes"')


def test_a_refusal_is_reported_as_one() -> None:
    client = _Client(_Message("", stop_reason="refusal"))
    with pytest.raises(AIValidationError, match="declined"):
        _ask(client)


def _status_error(cls_name: str, status: int, message: str):
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    body = {"error": {"message": message}}
    return getattr(anthropic, cls_name)(message, response=response, body=body)


def test_a_spent_account_fails_fast(monkeypatch) -> None:
    monkeypatch.setattr(claude.time, "sleep", lambda s: pytest.fail("waited"))
    client = _Client(_status_error(
        "BadRequestError", 400,
        "Your credit balance is too low to access the Anthropic API.",
    ))
    with pytest.raises(Exhausted, match="credit balance"):
        _ask(client)


def test_a_rate_limit_is_waited_out(monkeypatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(claude.time, "sleep", waits.append)
    client = _Client(
        _status_error("RateLimitError", 429, "rate limited"),
        _Message('{"n": 2}'),
    )

    data, _ = _ask(client)

    assert data == {"n": 2}
    assert waits == [5.0]
    assert issubclass(RateLimited, AIValidationError)


def test_an_unknown_model_says_so() -> None:
    client = _Client(_status_error("NotFoundError", 404, "model not found"))
    with pytest.raises(AIValidationError, match="not available"):
        _ask(client, model="claude-nope")


def test_every_object_is_closed() -> None:
    nested = {"type": "object", "properties": {
        "rows": {"type": "array", "items": {"type": "object", "properties": {}}},
        "maybe": {"type": ["object", "null"], "properties": {}},
    }}
    out = to_claude_schema(nested)
    assert out["additionalProperties"] is False
    assert out["properties"]["rows"]["items"]["additionalProperties"] is False
    assert out["properties"]["maybe"]["additionalProperties"] is False
    json.dumps(out)


def test_nothing_imports_gemini() -> None:
    """Claude only: `ai/gemini.py` is kept on disk and used by nothing."""
    import pathlib

    root = pathlib.Path(claude.__file__).resolve().parents[1]
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path.name != "gemini.py"
        and ("from .gemini" in (text := path.read_text(encoding="utf-8"))
             or "ai.gemini import" in text or "import genai" in text)
    ]
    assert offenders == []
