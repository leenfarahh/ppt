#used by: every AI call in the tool -- the deck validation layer (`ai/client.py`),
#the design check (`ai/designqa.py`), layout and role reading (`ai/layout.py`,
#`ai/roles.py`) and the brand book extractor (`brandbook/extractor.py`).
#
#The same contract `ai/gemini.py` offered, so the callers did not change shape:
#`generate_json` is the whole of it, and everything above it works in dicts.
#`gemini.py` is kept on disk and imported by nothing.


from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Optional, Sequence

log = logging.getLogger(__name__)

PROVIDER = "claude"
DEFAULT_MODEL = "claude-opus-5"
API_KEY_ENV = "ANTHROPIC_API_KEY"

# The longest answer asked for anywhere is the deck check's, at 65,536 with
# thinking included. Claude's own ceiling is higher; this is the cap on a
# runaway, not a target.
MAX_TOKENS_CEILING = 128_000

# Server-side refusal fallback. On a policy decline the API re-runs the same
# request on a fallback model inside the same call, chosen by the category of
# the refusal, so a slide that trips a classifier still gets reviewed.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIValidationError(RuntimeError):
    """Raised when an AI call cannot complete, with a reason a user can act on."""


class RateLimited(AIValidationError):
    """The API asked us to slow down. Distinct because it is the one failure
    that is worth simply waiting out."""


class Truncated(AIValidationError):
    """The answer ran past `max_tokens` and stopped mid-token.

    Separate from the other failures because the text it carries is not
    worthless. For a caller whose answer is a LIST -- a verdict per shape --
    the entries before the cut are as good as they were ever going to be.
    `salvage_list` reads them.
    """

    def __init__(self, message: str, text: str = "") -> None:
        super().__init__(message)
        self.text = text


class Exhausted(AIValidationError):
    """The account is out: credit spent, or a spend limit reached.

    The opposite kind of problem from a rate limit. A rate limit clears in
    seconds; this clears when somebody tops up or raises a limit, so it fails
    fast instead of every slide waiting out the backoff first.
    """


# Error bodies no amount of waiting will change.
_NOT_WORTH_WAITING = (
    "credit balance",
    "usage limit",
    "spend limit",
    "spending limit",
    "billing",
)

# How long to wait out a rate limit or an overloaded API, and how many times.
# The SDK already retries twice with a short backoff; these are the longer
# waits a per-minute quota needs. Under two minutes in total.
_RATE_LIMIT_WAITS = (5.0, 15.0, 30.0, 60.0)


def build_client(api_key_env: str = API_KEY_ENV) -> Any:
    import anthropic  # noqa: PLC0415

    key = os.environ.get(api_key_env)
    if key:
        return anthropic.Anthropic(api_key=key)
    # A bare constructor also resolves ANTHROPIC_AUTH_TOKEN and an `ant auth
    # login` profile, so an unset key is not proof of no credentials.
    log.debug("%s is unset; falling back to the SDK credential chain", api_key_env)
    return anthropic.Anthropic()


def effort_for(thinking_budget: int) -> str:
    """The effort level a caller's thinking budget stood for.

    Callers still size their answers as "answer + thinking budget", which is
    also right for Claude: `max_tokens` covers both. What Claude takes as the
    depth control is an effort level, so the budget is read back into one.
    """
    if thinking_budget <= 2048:
        return "low"
    if thinking_budget <= 8192:
        return "medium"
    if thinking_budget <= 16384:
        return "high"
    if thinking_budget <= 24576:
        return "xhigh"
    return "max"


def generate_json(
    client: Any,
    *,
    model: str,
    contents: Any,
    system_instruction: Optional[str],
    schema: dict[str, Any],
    cached_content: Optional[str] = None,
    thinking_budget: int,
    max_output_tokens: int,
    api_key_env: str = API_KEY_ENV,
    translate_schema: bool = True,
) -> tuple[dict[str, Any], Any]:
    """One structured answer: the parsed JSON, and the message it came in.

    `cached_content` is accepted and ignored. It named a Gemini cache; Claude
    caches the system prompt by marking it, which happens here on every call.
    """
    import anthropic  # noqa: PLC0415

    del cached_content
    output_schema = to_claude_schema(schema) if translate_schema else schema
    # A schema past the grammar's limits is stated in the instructions instead
    # of enforced, and the reply parsed as JSON. See `_fits_the_grammar`.
    enforced = _fits_the_grammar(output_schema)
    output_config: dict[str, Any] = {"effort": effort_for(thinking_budget)}
    if enforced:
        output_config["format"] = {"type": "json_schema", "schema": output_schema}
    else:
        log.info(
            "the answer's schema has more optional fields than structured "
            "output compiles; stating it in the instructions instead"
        )
        system_instruction = (
            (system_instruction + "\n\n" if system_instruction else "")
            + _SCHEMA_IN_PROSE + json.dumps(output_schema, indent=1)
        )
    request = dict(
        model=model,
        max_tokens=max(1024, min(int(max_output_tokens), MAX_TOKENS_CEILING)),
        messages=[{"role": "user", "content": _content_blocks(contents)}],
        thinking={"type": "adaptive"},
        output_config=output_config,
        betas=[_FALLBACK_BETA],
        fallbacks="default",
    )
    if system_instruction:
        # Marked for caching: the instructions and brand reference are the
        # same bytes on every batch of a run.
        request["system"] = [{
            "type": "text", "text": system_instruction,
            "cache_control": {"type": "ephemeral"},
        }]

    last: Exception | None = None
    for wait in (0.0,) + _RATE_LIMIT_WAITS:
        if wait:
            log.info("rate limited; waiting %.0fs before retrying", wait)
            time.sleep(wait)
        try:
            # Streamed: answers with room for thinking run long, and a
            # non-streamed request that large risks the HTTP timeout.
            with client.beta.messages.stream(**request) as stream:
                response = stream.get_final_message()
            break
        except anthropic.RateLimitError as exc:
            translated = _translate(exc, model, api_key_env)
            if not isinstance(translated, RateLimited):
                raise translated from exc
            last = translated
        except anthropic.APIStatusError as exc:
            translated = _translate(exc, model, api_key_env)
            if not isinstance(translated, RateLimited):
                raise translated from exc
            last = translated
        except anthropic.APIConnectionError as exc:
            raise AIValidationError(f"could not reach the API: {exc}") from exc
        except anthropic.AnthropicError as exc:
            raise AIValidationError(f"API call failed: {exc}") from exc
    else:
        raise last or AIValidationError("rate limited")

    check_refusal(response)
    return parse_json(response), response


def _message_of(exc: Any) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"]).strip()
    return str(getattr(exc, "message", "") or exc).strip()


def _translate(exc: Any, model: str, api_key_env: str) -> AIValidationError:
    code = getattr(exc, "status_code", None)
    detail = _message_of(exc)
    if any(phrase in detail.lower() for phrase in _NOT_WORTH_WAITING):
        # The API's own wording, because it names the thing to go and fix.
        return Exhausted(detail or "the account's credit or spend limit is exhausted")
    if code == 404:
        return AIValidationError(f"model {model!r} is not available to this key")
    if code in (401, 403):
        return AIValidationError(f"authentication failed; check ${api_key_env}")
    if code in (429, 529) or (code or 0) >= 500:
        return RateLimited("rate limited" if code == 429 else f"API busy ({code})")
    return AIValidationError(f"API error {code}: {detail}")


# response handling

def finish_reason(response: Any) -> str:
    return str(getattr(response, "stop_reason", "") or "")


def check_refusal(response: Any) -> None:
    if finish_reason(response) == "refusal":
        details = getattr(response, "stop_details", None)
        why = getattr(details, "explanation", None) or getattr(details, "category", None)
        raise AIValidationError(
            "request declined by the model" + (f": {why}" if why else "")
        )


_SCHEMA_IN_PROSE = (
    "ANSWER FORMAT. Reply with one JSON object and nothing else -- no prose "
    "before or after it, no code fence. It must match this JSON Schema. Use "
    "null wherever the schema allows it and the source does not say:\n"
)

# Structured output compiles the schema into a grammar and caps how many
# fields may be a union (a type list or `anyOf`, which is how "or null" is
# written). The brand book's schema has forty: every rule it asks for may be
# unstated in a given book, and null is how it says so.
_MAX_UNION_FIELDS = 16


def _fits_the_grammar(schema: Any) -> bool:
    return _unions_in(schema) <= _MAX_UNION_FIELDS


def _unions_in(node: Any) -> int:
    if isinstance(node, list):
        return sum(_unions_in(n) for n in node)
    if not isinstance(node, dict):
        return 0
    own = int(isinstance(node.get("type"), list) or "anyOf" in node)
    return own + sum(_unions_in(v) for k, v in node.items() if k != "enum")


def response_text(response: Any) -> Optional[str]:
    chunks = [
        block.text
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    return "".join(chunks) or None


def parse_json(response: Any) -> dict[str, Any]:
    reason = finish_reason(response)
    text = response_text(response)
    if not text:
        if reason == "max_tokens":
            raise Truncated(
                "the answer was cut off before any of it was written "
                "(stop_reason=max_tokens)", text="",
            )
        raise AIValidationError(f"response contained no text (stop_reason={reason})")
    try:
        data = json.loads(_unfenced(text))
    except json.JSONDecodeError as exc:
        # Structured output makes this near-impossible for any reason other
        # than the answer being cut off at max_tokens. The text comes along so
        # a caller that can use a partial answer has something to use.
        raise Truncated(
            f"response was not valid JSON (stop_reason={reason}): {exc}",
            text=text,
        ) from exc
    if not isinstance(data, dict):
        raise AIValidationError(f"response JSON was {type(data).__name__}, not an object")
    return data


def _unfenced(text: str) -> str:
    """The JSON object in a reply, without a code fence or stray prose round
    it. Only the unenforced path needs this; an enforced answer is bare."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    start, end = stripped.find("{"), stripped.rfind("}")
    return stripped[start:end + 1] if 0 <= start < end else stripped


def usage_counts(response: Any) -> dict[str, int]:
    """Token counts off a response, under names that mean the same thing for
    every caller: uncached input, output (thinking included), cache reads and
    cache writes -- four disjoint numbers."""
    usage = getattr(response, "usage", None)
    return {
        "input": count(usage, "input_tokens"),
        "output": count(usage, "output_tokens"),
        "cache_read": count(usage, "cache_read_input_tokens"),
        "cache_write": count(usage, "cache_creation_input_tokens"),
    }


# Whitespace and the commas between entries: what to step over on the way
# from one decoded entry of an array to the next.
_JSON_GAP = " \t\r\n,"


def salvage_list(text: str, key: str) -> list[Any]:
    """The complete entries of one top-level array in a truncated answer.

    Entries are decoded one at a time from the start of the array and the
    first one that will not decode ends it. Nothing is repaired: what comes
    back is a prefix of what the model actually said and never an invention.
    An empty list for anything unrecognisable.
    """
    marker = f'"{key}"'
    at = text.find(marker)
    if at < 0:
        return []
    # The bracket has to be THIS key's, so only a colon and whitespace may sit
    # between them.
    at += len(marker)
    while at < len(text) and text[at] in _JSON_GAP + ":":
        at += 1
    if at >= len(text) or text[at] != "[":
        return []

    decoder = json.JSONDecoder()
    out: list[Any] = []
    at += 1
    while at < len(text):
        while at < len(text) and text[at] in _JSON_GAP:
            at += 1
        if at >= len(text) or text[at] == "]":
            break
        try:
            entry, at = decoder.raw_decode(text, at)
        except ValueError:
            break
        out.append(entry)
    return out


def count(usage: Any, name: str) -> int:
    """Usage fields come back as None rather than 0 when they do not apply."""
    return getattr(usage, name, 0) or 0


# schema

# What Claude's structured output does not take. Dropped rather than sent,
# because one of them in a schema is a 400 for the whole request.
_UNSUPPORTED = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minLength", "maxLength", "pattern", "maxItems",
    "propertyOrdering", "nullable",
})


def to_claude_schema(schema: Any) -> Any:
    """A JSON Schema as Claude's structured output takes it.

    Every object closed with `additionalProperties: false`, which it requires,
    and the keywords it rejects removed. Everything else passes through.
    """
    if isinstance(schema, list):
        return [to_claude_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED:
            continue
        if key == "minItems" and value not in (0, 1):
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: to_claude_schema(v) for k, v in value.items()}
        elif key in ("items", "anyOf", "allOf", "$defs", "definitions"):
            out[key] = (
                {k: to_claude_schema(v) for k, v in value.items()}
                if isinstance(value, dict) and key in ("$defs", "definitions")
                else to_claude_schema(value)
            )
        else:
            out[key] = value
    types = out.get("type")
    if types == "object" or (isinstance(types, list) and "object" in types):
        out["additionalProperties"] = False
    return out


# file input

# Past these the file goes through the Files API instead of inline: base64
# grows a file by a third, and an image over 5 MB or a request over 32 MB is
# refused.
_INLINE_IMAGE_BYTES = 3_500_000
_INLINE_DOCUMENT_BYTES = 20_000_000


def file_part(client: Any, path: Any, mime_type: str) -> dict[str, Any]:
    """A content block for a local file: an image, or a PDF as a document."""
    kind = "document" if mime_type == "application/pdf" else "image"
    size = path.stat().st_size
    limit = _INLINE_DOCUMENT_BYTES if kind == "document" else _INLINE_IMAGE_BYTES
    if size <= limit:
        log.debug("attaching %s inline (%d bytes)", path.name, size)
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        return {
            "type": kind,
            "source": {"type": "base64", "media_type": mime_type, "data": data},
        }

    log.info("%s is %.1f MB; uploading via the Files API", path.name, size / 1e6)
    try:
        uploaded = client.files.upload(file=(path.name, path.read_bytes(), mime_type))
    except Exception as exc:
        raise AIValidationError(f"upload of {path.name} failed: {exc}") from exc
    return {"type": kind, "source": {"type": "file", "file_id": uploaded.id}}


def _content_blocks(contents: Any) -> list[dict[str, Any]]:
    """The callers' contents -- a string, or a list of strings and blocks --
    as the list of content blocks one user turn carries."""
    items = contents if isinstance(contents, (list, tuple)) else [contents]
    blocks: list[dict[str, Any]] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, str):
            if item.strip():
                blocks.append({"type": "text", "text": item})
        else:
            blocks.append(item)
    return blocks


def as_sequence(*parts: Any) -> Sequence[Any]:
    """Contents list with empty parts dropped."""
    return [p for p in parts if p is not None]
