#used by: the deck validation layer (`ai/client.py`) and the brand book extractor (`brandbook/extractor.py`). 
#`generate_json` is the whole contract. Everything above it works in dicts.


from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional, Sequence

from .schema import to_gemini_schema

log = logging.getLogger(__name__)


class AIValidationError(RuntimeError):
    """Raised when an AI call cannot complete, with a reason a user can act on."""


class RateLimited(AIValidationError):
    """The API asked us to slow down. Distinct because it is the one failure
    that is worth simply waiting out."""


class Exhausted(AIValidationError):
    """The account is out: a spend cap reached, or a daily quota spent.

    Arrives as HTTP 429 exactly like a rate limit, and is the opposite kind of
    problem. A rate limit clears in seconds; this clears when somebody raises
    a cap or the month turns over. Telling them apart matters more than it
    sounds: treating this one as transient meant every slide of a 17-slide
    deck waited 5+15+30+60 seconds before failing anyway -- half an hour of
    sleeping to produce the report that a fast failure would have produced
    immediately, with the same findings missing.
    """


# 429 bodies that no amount of waiting will change. Matched on the message
# because the status is RESOURCE_EXHAUSTED for both kinds.
_NOT_WORTH_WAITING = (
    "spending cap",
    "spend cap",
    "billing",
    "per day",
    "daily limit",
    "exceeded your current quota",
)


# How long to wait out a rate limit, and how many times. Generous on purpose:
# a per-minute quota needs seconds, not milliseconds, and the alternative is
# dropping a slide's findings from the report. The total ceiling is under two
# minutes, so a genuinely exhausted quota still fails rather than hanging.
_RATE_LIMIT_WAITS = (5.0, 15.0, 30.0, 60.0)


def build_client(api_key_env: str = "GEMINI_API_KEY") -> Any:
    from google import genai  # noqa: PLC0415

    # A bare constructor also resolves GOOGLE_API_KEY and an application
    # default credential, so an unset GEMINI_API_KEY is not proof of no
    # credentials.
    if not os.environ.get(api_key_env):
        log.debug("%s is unset; falling back to the SDK credential chain", api_key_env)
    return genai.Client()


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
    api_key_env: str = "GEMINI_API_KEY",
    translate_schema: bool = True,
) -> tuple[dict[str, Any], Any]:

    # httpx is a hard dependency of the SDK, which raises it bare for anything
    # below the API layer.
    import httpx  # noqa: PLC0415
    from google.genai import errors, types  # noqa: PLC0415

    response_schema = to_gemini_schema(schema) if translate_schema else schema

    # Rate limits are waited out here rather than raised. The calls run
    # several at a time now, and a limit hit by one of six in flight used to
    # cost that slide its findings entirely: the report came back quietly
    # short. Waiting is the honest answer, since the work is not impossible,
    # only unwelcome this second.
    import time  # noqa: PLC0415

    last: Exception | None = None
    for attempt, wait in enumerate((0.0,) + _RATE_LIMIT_WAITS):
        if wait:
            log.info("rate limited; waiting %.0fs before retrying", wait)
            time.sleep(wait)
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    cached_content=cached_content,
                    max_output_tokens=max_output_tokens,
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=thinking_budget
                    ),
                    response_mime_type="application/json",
                    response_schema=response_schema,
                ),
            )
            break
        except errors.ClientError as exc:
            translated = _translate_client_error(exc, model, api_key_env)
            if not isinstance(translated, RateLimited):
                raise translated from exc
            last = translated
        except errors.ServerError as exc:
            raise AIValidationError(
                f"API error {getattr(exc, 'code', None)}: "
                f"{getattr(exc, 'message', exc)}"
            ) from exc
        except errors.APIError as exc:
            raise AIValidationError(f"API call failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AIValidationError(f"could not reach the API: {exc}") from exc
    else:
        raise last or AIValidationError("rate limited")

    check_refusal(response)
    return parse_json(response), response


def _message_of(exc: Any) -> str:
    """The human-readable half of an API error, however the SDK wrapped it."""
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        inner = details.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"]).strip()
    return str(getattr(exc, "message", "") or exc).strip()


def _translate_client_error(exc: Any, model: str, api_key_env: str) -> AIValidationError:
    code = getattr(exc, "code", None)
    if code == 404:
        return AIValidationError(f"model {model!r} is not available to this key")
    if code in (401, 403):
        return AIValidationError(f"authentication failed; check ${api_key_env}")
    if code == 429:
        detail = _message_of(exc)
        if any(phrase in detail.lower() for phrase in _NOT_WORTH_WAITING):
            # The API's own wording, because it names the thing to go and fix
            # and a paraphrase would lose the link.
            return Exhausted(detail or "the API quota or spend cap is exhausted")
        return RateLimited("rate limited")
    return AIValidationError(f"API error {code}: {getattr(exc, 'message', exc)}")


# response handling

def finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    finish = getattr(candidates[0], "finish_reason", None)
    return getattr(finish, "name", None) or str(finish or "")


def check_refusal(response: Any) -> None:
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(feedback, "block_reason", None)
    if block_reason:
        detail = getattr(feedback, "block_reason_message", None) or block_reason
        raise AIValidationError(f"request declined by the model: {detail}")

    if not getattr(response, "candidates", None):
        raise AIValidationError("response contained no candidates")

    reason = finish_reason(response)
    if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
        raise AIValidationError(f"request declined by the model: {reason}")


def response_text(response: Any) -> Optional[str]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    content = getattr(candidates[0], "content", None)
    chunks = [
        part.text
        for part in (getattr(content, "parts", None) or [])
        if getattr(part, "text", None) and not getattr(part, "thought", False)
    ]
    return "".join(chunks) or None


def parse_json(response: Any) -> dict[str, Any]:
    reason = finish_reason(response)
    text = response_text(response)
    if not text:
        raise AIValidationError(
            f"response contained no text part (finish_reason={reason})"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # response_schema makes this near-impossible; if it happens, the
        # response was truncated by max_output_tokens, most likely because
        # thinking ate the budget. Lower the effort or raise max_tokens.
        raise AIValidationError(
            f"response was not valid JSON (finish_reason={reason}): {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise AIValidationError(f"response JSON was {type(data).__name__}, not an object")
    return data


def count(usage: Any, name: str) -> int:
    """Usage fields come back as None rather than 0 when they do not apply."""
    return getattr(usage, name, 0) or 0


# file input

INLINE_LIMIT_BYTES = 15 * 1024 * 1024


def file_part(client: Any, path: Any, mime_type: str) -> Any:
    """A content part for a local file, inline or uploaded by size.

    Uploaded files are held for 48 hours, which is irrelevant here: one
    extraction run uses the reference file once.
    """
    from google.genai import types  # noqa: PLC0415

    size = path.stat().st_size
    if size <= INLINE_LIMIT_BYTES:
        log.debug("attaching %s inline (%d bytes)", path.name, size)
        return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type)

    log.info("%s is %.1f MB; uploading via the Files API", path.name, size / 1e6)
    uploaded = client.files.upload(
        file=str(path),
        config=types.UploadFileConfig(mime_type=mime_type, display_name=path.name),
    )
    uploaded = _await_active(client, uploaded)
    return types.Part.from_uri(
        file_uri=uploaded.uri,
        mime_type=uploaded.mime_type or mime_type,
    )


def _await_active(client: Any, uploaded: Any, attempts: int = 20) -> Any:

    import time  # noqa: PLC0415

    for _ in range(attempts):
        state = getattr(getattr(uploaded, "state", None), "name", None) or str(
            getattr(uploaded, "state", "")
        )
        if state == "ACTIVE":
            return uploaded
        if state == "FAILED":
            detail = getattr(uploaded, "error", None)
            raise AIValidationError(f"upload failed: {detail}")
        time.sleep(1.0)
        uploaded = client.files.get(name=uploaded.name)
    raise AIValidationError(
        f"uploaded file {getattr(uploaded, 'name', '?')} did not become active"
    )


def as_sequence(*parts: Any) -> Sequence[Any]:
    """Contents list with empty parts dropped."""
    return [p for p in parts if p is not None]
