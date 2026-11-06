"""Pluggable LLM backends — let a host application supply the model call.

graphlm's default transport is :func:`graphlm.llm.call_llm`, an OpenAI-compatible
HTTP client configured from ``GRAPHLM_*``. A host that already owns model access
(an agent framework, an IDE, a gateway with its own credentials and billing)
passes an :data:`LLMBackend` to ``generate_graph(llm=...)`` instead. graphlm
then never resolves ``Settings``, never reads ``~/.config/graphlm/.env``, and
never opens a socket of its own — every request goes through the host.

The backend only moves text. Parsing and schema validation stay in graphlm
(:func:`graphlm.llm.parse_reply`), the same path the HTTP client uses, so a
backend cannot change what counts as a valid graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from graphlm.llm import CodebaseGraph, GraphLLErrorParse, parse_reply


@dataclass(frozen=True, slots=True)
class LLMReply:
    """What a backend returns for one request.

    ``usage`` uses the OpenAI field names (``prompt_tokens``,
    ``completion_tokens``) so it lands in ``meta.usage`` exactly like the HTTP
    client's streamed usage chunk. ``None`` means "the host did not report
    usage" and leaves the stamp's real-count fields unset — never a fake zero.
    """

    text: str
    usage: Optional[dict[str, object]] = None


class LLMBackend(Protocol):
    """One bounded, non-streaming chat completion.

    ``json_schema`` is set for the structured pass (pass 2: the
    ``CodebaseGraph`` JSON schema) and ``None`` for the free-form pass 1. A
    backend should forward it as a structured-output hint when its transport
    supports one; graphlm validates the reply itself either way.
    """

    def __call__(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        json_schema: Optional[dict[str, object]],
        schema_name: Optional[str],
        timeout: float,
        max_output_tokens: int,
    ) -> LLMReply: ...


def complete(
    backend: LLMBackend,
    system_prompt: str,
    user_prompt: str,
    *,
    response_format: type[CodebaseGraph] | None = None,
    timeout: float,
    max_output_tokens: int,
    on_usage: Callable[[dict[str, object]], None] | None = None,
) -> CodebaseGraph | str:
    """Run one request through ``backend`` with ``call_llm``'s return contract.

    Returns the validated ``response_format`` model, or the extracted JSON text
    when no format is requested — exactly what :func:`graphlm.llm.call_llm`
    returns, so ``generate_graph`` treats both transports identically.
    """
    schema = response_format.model_json_schema() if response_format else None
    reply = backend(
        system_prompt,
        user_prompt,
        json_schema=schema,
        schema_name=response_format.__name__ if response_format else None,
        timeout=timeout,
        max_output_tokens=max_output_tokens,
    )
    if not isinstance(reply, LLMReply):
        raise GraphLLErrorParse(
            f"LLM backend returned {type(reply).__name__}, expected LLMReply"
        )
    if on_usage is not None and reply.usage is not None:
        on_usage(reply.usage)
    return parse_reply(reply.text, response_format)
