"""LLM client — OpenAI-compatible HTTP call with response parsing and recovery."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from graphlm.limits import LLM_MAX_OUTPUT_TOKENS
from graphlm.models import CodebaseGraph

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

# Default request timeout (seconds). Generous because pass 2 is streamed: a
# large project's full CodebaseGraph JSON legitimately takes ~200s to generate
# on a mid-size model. Overridable via GRAPHLM_TIMEOUT / --timeout (#18).
_DEFAULT_TIMEOUT = 300.0
_MAX_RETRIES = 2
_RETRY_DELAY = 2.0


def _load_httpx() -> Any:
    """Import httpx on first use and cache it as ``graphlm.llm.httpx``.

    Importing this module must not import httpx: ``import graphlm``,
    ``--help``, ``--version``, ``--dry-run``, and ``--serve`` never make an
    LLM call. The cached module object is the patch target
    ``graphlm.llm.httpx.Client`` (tests bind a spy there before ``call_llm``).
    """
    cached = globals().get("httpx")
    if cached is not None:
        return cached
    import httpx

    globals()["httpx"] = httpx
    return httpx


def __getattr__(name: str) -> Any:
    if name == "httpx":
        return _load_httpx()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class GraphLLError(Exception):
    """Base error for graphLM failures."""


class GraphLLErrorUnconfigured(GraphLLError):
    """Required configuration is missing."""


class GraphLLErrorTransport(GraphLLError):
    """Network or HTTP transport failure."""


class GraphLLErrorResponse(GraphLLError):
    """LLM returned a non-2xx response."""


class GraphLLErrorParse(GraphLLError):
    """Response was not valid JSON or didn't match expected schema."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Normalized LLM response."""

    content: str
    model: str


def _extract_json(text: str) -> str | None:
    """Extract JSON from LLM response, stripping markdown code fences if present.

    Tries multiple recovery strategies to handle common LLM output patterns.
    """
    text = text.strip()

    # Try the text as-is first
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove the first fence line
        lines = lines[1:]
        # Remove trailing fence if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines)
        try:
            json.loads(cleaned)
            return cleaned
        except (json.JSONDecodeError, ValueError):
            pass

    # Find first { and last } in the text
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, ValueError):
            pass

    return None


class GraphLLErrorTruncated(GraphLLErrorParse):
    """The model hit its max_tokens ceiling and returned truncated output."""


@dataclass(frozen=True, slots=True)
class StreamResult:
    """What ``_read_streamed_completion`` pulls out of one response.

    ``content`` is the reassembled message text. ``usage`` is the server's
    token accounting (``prompt_tokens`` / ``completion_tokens`` / ...) when the
    response carried one, else ``None`` — an endpoint that ignores
    ``stream_options`` simply never sends it, and that is a normal state, not
    an error (innovation #6).
    """

    content: str
    usage: dict[str, object] | None = None


def _usage_from_chunk(chunk: object) -> dict[str, object] | None:
    """Return the ``usage`` object of a chat-completions chunk/body, or None.

    Tolerant by design: usage is telemetry, never load-bearing, so a missing
    or malformed value (a string, a list, ``null``) must not raise — it just
    reads as "no usage seen". A non-dict ``chunk`` yields None too, so this is
    safe to call on any decoded JSON.
    """
    if not isinstance(chunk, dict):
        return None
    usage = chunk.get("usage")
    return usage if isinstance(usage, dict) else None


def _read_streamed_completion(
    response: httpx.Response, *, max_output_tokens: int = LLM_MAX_OUTPUT_TOKENS
) -> StreamResult:
    """Read a streamed (SSE) chat-completions response into a ``StreamResult``.

    The request is sent with ``stream: true`` so the response arrives as a
    sequence of ``data: {json}`` lines (OpenAI SSE), terminated by ``data:
    [DONE]``. Streaming is what keeps a long generation alive past a proxy's
    read timeout (Cloudflare 524 — #18): each delta resets the edge timer,
    whereas a single buffered response can exceed it before the first byte.

    Tolerates a **non-SSE** body too: if no ``data:`` line is ever seen, the
    accumulated text is returned as-is and parsed as a plain chat-completions
    JSON object by the caller. This keeps non-streaming test mocks (and any
    server that ignores ``stream``) working unchanged.

    Token usage: the request asks for ``stream_options.include_usage``, so a
    compliant server appends one last chunk *before* ``[DONE]`` whose
    ``choices`` is ``[]`` and whose ``usage`` carries the real prompt /
    completion token counts. That chunk must be inspected **before** the
    empty-``choices`` skip below, or it is silently dropped. A plain (non-SSE)
    completion object carries ``usage`` at top level and is read the same way.
    Usage is optional end to end — absent or malformed, ``usage`` is ``None``.

    Raises GraphLLErrorTruncated if the model stopped on ``finish_reason ==
    "length"`` (output hit max_tokens — the JSON is truncated and unparseable),
    so that surfaces as a clear "graph too large" error rather than a confusing
    parse failure.
    """
    parts: list[str] = []
    raw: list[str] = []
    saw_sse = False
    finish_reason: str | None = None
    usage: dict[str, object] | None = None

    for line in response.iter_lines():
        raw.append(line)
        stripped = line.strip()
        if not stripped or not stripped.startswith("data:"):
            continue
        saw_sse = True
        data = stripped[len("data:") :].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(chunk, dict):
            continue
        # The usage chunk has empty `choices` — read it before the skip below.
        seen_usage = _usage_from_chunk(chunk)
        if seen_usage is not None:
            usage = seen_usage
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        piece = delta.get("content")
        if piece:
            parts.append(piece)
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

    if finish_reason == "length":
        raise GraphLLErrorTruncated(
            "LLM output hit the max_tokens ceiling and was truncated "
            f"({max_output_tokens} tokens) — the project is too large to graph "
            "in one response. Raise --max-output-tokens (GRAPHLM_MAX_OUTPUT_TOKENS) "
            "or reduce scope (e.g. --max-pass2-files)."
        )

    if not saw_sse:
        # Non-SSE body (e.g. a test mock or a server ignoring stream): parse the
        # whole body as a plain chat-completions JSON object and pull content out
        # of it, so this path yields the same content string as the SSE path.
        body = "".join(raw)
        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"] or ""
        except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError):
            # Not a recognizable completion object — return raw for the caller's
            # JSON-recovery to attempt (preserves prior lenient behavior).
            return StreamResult(content=body)
        return StreamResult(content=content, usage=_usage_from_chunk(data))
    return StreamResult(content="".join(parts), usage=usage)


def call_llm(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    response_format: type[CodebaseGraph] | None = None,
    timeout: float | None = None,
    max_output_tokens: int = LLM_MAX_OUTPUT_TOKENS,
    on_usage: Callable[[dict[str, object]], None] | None = None,
) -> CodebaseGraph | str:
    """Call an OpenAI-compatible LLM endpoint and parse the response.

    Args:
        base_url: The API base URL (e.g. https://your-endpoint/v1).
        api_key: The API key for authentication.
        model: The model name to use.
        system_prompt: The system message.
        user_prompt: The user message.
        response_format: If provided, validate the JSON response against
            this Pydantic model.
        timeout: Request timeout in seconds (default 300; streamed responses can
            legitimately take several minutes on a large project — #18).
        on_usage: Optional callback invoked once with the server's ``usage``
            dict (``prompt_tokens`` / ``completion_tokens`` / ...) when the
            response carried one. Never invoked when the endpoint sent no
            usage. A callback rather than a changed return type so the
            ``CodebaseGraph | str`` contract and every existing caller stay
            untouched (innovation #6).

    The response is streamed (``stream: true``) and reassembled; a server that
    ignores the flag and returns a normal body is handled transparently. Both
    passes stream for a single response-reading path.

    Returns:
        Parsed CodebaseGraph (or the response_format model).

    Raises:
        GraphLLErrorUnconfigured: If configuration is missing.
        GraphLLErrorTransport: If the HTTP call fails.
        GraphLLErrorResponse: If the LLM returns an error response.
        GraphLLErrorParse: If the response cannot be parsed.
    """
    httpx = _load_httpx()

    if not base_url or not api_key:
        raise GraphLLErrorUnconfigured("base_url and api_key are required")

    if timeout is None:
        timeout = _DEFAULT_TIMEOUT

    url = base_url.rstrip("/") + "/chat/completions"

    payload: dict[str, object] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,  # Low temperature for structured output
        "max_tokens": max_output_tokens,
        # Stream so a long generation stays alive past proxy read timeouts —
        # a non-streamed pass-2 for a large repo hit Cloudflare's 524 at ~125s
        # while the origin was still generating (#18). A server that ignores
        # this flag returns a normal body, which _read_streamed_completion
        # handles transparently.
        "stream": True,
        # Ask for the real token accounting as a final SSE chunk (OpenAI
        # `stream_options`). Streaming otherwise loses `usage` entirely — a
        # streamed response has no top-level usage object — and the real
        # prompt count is what lets the estimate_tokens heuristic be checked
        # against the served model instead of guessed at (#17). An endpoint
        # that ignores the option just never sends the chunk; usage stays
        # None and nothing else changes (innovation #6).
        "stream_options": {"include_usage": True},
    }

    # Constrain the output to the JSON schema when a structured response is
    # requested (pass 2 only — pass 1 passes response_format=None and wants a
    # free-form {"requested_files": [...]} object). Prompt-only instruction is
    # not enough for every served model: Qwen3.6-35B, told in-prompt to emit a
    # CodebaseGraph, returned a near-empty `{"database_schema": null}` and the
    # run produced no graph at all — with response_format attached it emits the
    # full graph (#31). The endpoint treats the schema as a guided-JSON hint,
    # not strict all-required enforcement, so the prompt's "return an empty
    # directory_tree" (and the locally-filled meta / import_cycles /
    # deterministic_edges) still come back empty — #18 is not reopened. On an
    # endpoint that rejects response_format, the call degrades: see the
    # non-200 handling below, which retries once without the constraint.
    if response_format is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": response_format.__name__,
                "schema": response_format.model_json_schema(),
            },
        }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=1),
            ) as client:
                with client.stream(
                    "POST", url, json=payload, headers=headers
                ) as response:
                    if response.status_code != 200:
                        # Status is known before any body bytes. A non-200 here
                        # (e.g. the 524 that motivated streaming) is NOT retried
                        # — GraphLLError propagates via the except clause below,
                        # so we don't burn minutes re-waiting on a hard failure.
                        response.read()
                        detail = response.text[:500]
                        # Portability fallback: an endpoint that doesn't support
                        # response_format (or rejects the schema's $defs/anyOf)
                        # rejects the *parameter* with a 400 Bad Request. Rather
                        # than turn a previously-working prompt-only setup into a
                        # hard failure, drop the constraint and re-issue via the
                        # retry loop. Scoped to 400 specifically — a 404 (missing
                        # endpoint/model), 401/403 (auth), or 429 (rate limit) is
                        # not a schema problem and must still surface as an error,
                        # not be masked by a constraint-less retry. `continue`
                        # costs one connection-retry attempt, which is fine: a 400
                        # is deterministic, so the freed attempt would not have
                        # helped, and popping the constraint makes this one-shot
                        # (the next 400 cannot loop back here).
                        #
                        # stream_options is dropped FIRST (innovation #6): it is
                        # optional telemetry, whereas response_format is
                        # load-bearing (#31) — so a strict endpoint that rejects
                        # the newer stream_options parameter keeps the schema
                        # constraint, and one that rejects response_format loses
                        # only the usage stamp on the way to the prompt-only
                        # retry. Two pops + the final request fit the three
                        # attempts (_MAX_RETRIES + 1).
                        if response.status_code == 400:
                            if "stream_options" in payload:
                                payload.pop("stream_options")
                                continue
                            if "response_format" in payload:
                                payload.pop("response_format")
                                continue
                        raise GraphLLErrorResponse(
                            f"LLM returned HTTP {response.status_code}: {detail}"
                        )

                    streamed = _read_streamed_completion(
                        response, max_output_tokens=max_output_tokens
                    )

                content = streamed.content
                if on_usage is not None and streamed.usage is not None:
                    on_usage(streamed.usage)

                if not content:
                    raise GraphLLErrorParse("LLM returned an empty response")

                # Try to parse JSON from the response
                json_text = _extract_json(content)
                if json_text is None:
                    raise GraphLLErrorParse(
                        f"Could not extract JSON from LLM response. "
                        f"Response preview: {content[:200]}"
                    )

                # Validate against schema if requested
                if response_format is not None:
                    try:
                        return response_format.model_validate_json(json_text)
                    except Exception as e:
                        raise GraphLLErrorParse(
                            f"JSON response did not match expected schema: {e}\n"
                            f"Response preview: {json_text[:500]}"
                        )

                # Return raw JSON string if no schema validation requested
                return json_text  # type: ignore[return-value]

        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            # Mid-stream transport failures (a dropped connection after partial
            # deltas). Retriable — the whole generation re-runs and any partial
            # content is discarded; a partial stream is never parsed (#18).
            httpx.ReadError,
            httpx.RemoteProtocolError,
            OSError,
        ) as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAY * (2**attempt)
                logger.warning(
                    "LLM connection failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    delay,
                    e,
                )
                time.sleep(delay)
            else:
                raise GraphLLErrorTransport(
                    f"LLM connection failed after {_MAX_RETRIES + 1} attempts: {e}"
                ) from e
        except httpx.TimeoutException as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAY * (2**attempt)
                logger.warning(
                    "LLM request timed out (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    delay,
                )
                time.sleep(delay)
            else:
                raise GraphLLErrorTransport(
                    f"LLM request timed out after {_MAX_RETRIES + 1} attempts"
                ) from e
        except GraphLLError:
            # These are not retriable — propagate immediately
            raise

    # Should not reach here, but just in case
    assert last_error is not None
    raise GraphLLErrorTransport(
        f"LLM call failed: {last_error}"
    ) from last_error
