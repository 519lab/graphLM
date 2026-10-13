"""Configuration — frozen Settings dataclass from environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

from dotenv import load_dotenv

from graphlm.limits import LLM_MAX_OUTPUT_TOKENS


def _user_config_path() -> Path:
    """User-level config path: ``$XDG_CONFIG_HOME/graphlm/.env`` when
    ``$XDG_CONFIG_HOME`` is set and non-empty, else ``~/.config/graphlm/.env``.

    Reads the env var at call time (not import time) so it stays testable.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "graphlm" / ".env"


def _load_env_files() -> None:
    """Populate ``os.environ`` from the user-level .env without clobbering
    real env vars.

    Resolution precedence (first non-empty wins), highest to lowest:

    1. the real shell environment (already in ``os.environ`` — never touched);
    2. the user-level ``~/.config/graphlm/.env`` (or
       ``$XDG_CONFIG_HOME/graphlm/.env``);
    3. the built-in defaults in :class:`Settings`.

    A ``.env`` in the working directory, any ancestor, or the scanned project
    is never loaded (ADR-009). ``load_dotenv(override=False)`` never overwrites
    a variable already present, so an exported ``GRAPHLM_*`` always wins.
    The user file is loaded only when it exists: ``load_dotenv("")`` would
    fall back to a frame-based search from *this module's* directory.
    """
    user_env = _user_config_path()
    if user_env.is_file():
        load_dotenv(user_env, override=False)


_load_env_files()


# Default maximum context window (tokens) — ~128k with room for output
_DEFAULT_MAX_CONTEXT = 120000
# Default LLM request timeout (seconds) — generous because pass 2 is streamed
# and a large project's generation can take minutes (#18). Mirrors
# llm._DEFAULT_TIMEOUT; overridable via GRAPHLM_TIMEOUT / --timeout.
_DEFAULT_TIMEOUT = 300.0
# Default max output tokens — sourced from limits.py (the same integer llm.py
# re-exports) so this module does not import the HTTP client. Sized from
# measurement (a real project's full graph needs ~18k output tokens — #18);
# overridable via GRAPHLM_MAX_OUTPUT_TOKENS / --max-output-tokens.
_DEFAULT_MAX_OUTPUT_TOKENS = LLM_MAX_OUTPUT_TOKENS


@dataclass(frozen=True, slots=True)
class Settings:
    """OpenAI-compatible LLM endpoint configuration from env vars."""

    base_url: str
    api_key: str
    model: str
    max_context: int = _DEFAULT_MAX_CONTEXT
    timeout: float = _DEFAULT_TIMEOUT
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings from GRAPHLM_* environment variables."""
        import os

        base_url = os.environ.get("GRAPHLM_BASE_URL", "")
        api_key = os.environ.get("GRAPHLM_API_KEY", "")
        model = os.environ.get("GRAPHLM_MODEL", "")
        max_context = int(os.environ.get("GRAPHLM_MAX_CONTEXT", _DEFAULT_MAX_CONTEXT))
        timeout = float(os.environ.get("GRAPHLM_TIMEOUT", _DEFAULT_TIMEOUT))
        max_output_tokens = int(
            os.environ.get("GRAPHLM_MAX_OUTPUT_TOKENS", _DEFAULT_MAX_OUTPUT_TOKENS)
        )

        if not base_url:
            raise ValueError(
                "GRAPHLM_BASE_URL not set. Export it, set it in "
                "~/.config/graphlm/.env, or pass --base-url."
            )
        if not api_key:
            raise ValueError(
                "GRAPHLM_API_KEY not set. Export it, set it in "
                "~/.config/graphlm/.env, or pass --api-key."
            )
        if not model:
            raise ValueError(
                "GRAPHLM_MODEL not set. Export it, set it in "
                "~/.config/graphlm/.env, or pass --model."
            )

        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_context=max_context,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )
