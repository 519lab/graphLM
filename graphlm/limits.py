"""Shared numeric ceilings used by configuration and the LLM client.

This module is a leaf on purpose. ``config.py`` needs the output-token default
at import, and importing it from ``llm.py`` used to pull in ``httpx`` (and
Rich, via ``httpx._main``) for every ``import graphlm``, ``--help``, and
``--dry-run``. The integer lives here so neither side has to import the other.
"""

# Default max output tokens (the ``max_tokens`` the client requests). This is a
# *ceiling*, not a reservation — the model stops when its response is complete,
# so a high value costs nothing (no extra latency, tokens, or budget) but stops
# a large project's graph from truncating mid-JSON. Truncation defeats the whole
# tool, so the default is the model's practical output max rather than a tight
# guess: 128000 is what the Claude/GPT entries on the target endpoint advertise
# as max_output_tokens, and the Qwen endpoint accepts >=200k. Overridable via
# GRAPHLM_MAX_OUTPUT_TOKENS / --max-output-tokens (#25).
#
# NOTE: on the target OpenAI-compatible endpoint, input and output ceilings are
# INDEPENDENT (measured: 180k input + 200k max_tokens = 380k combined is
# accepted), so this does NOT come out of the input budget (max_context) — that
# was a false assumption in #17/#18, now unwound (#25). Some other servers
# (vLLM max_model_len, Anthropic) do bound prompt+generation together; anyone on
# such an endpoint lowers this via the flag.
LLM_MAX_OUTPUT_TOKENS = 128000
