"""The environment graphlm hands to the processes it starts.

graphlm loads its endpoint key (``GRAPHLM_API_KEY``) and the optional
``TYPESAFE_API_KEY`` into ``os.environ``. Child processes — ``git`` for the
provenance stamp, and pip / pipx / uv for ``--upgrade`` and ``--setup`` — need
neither, and a child that runs repository-influenced code (a git hook-like
config, a build backend) would otherwise inherit both keys. A denylist, not an
allowlist: proxies, ``PIP_*`` / ``UV_*`` settings and ``PATH`` must still reach
the installers.
"""

from __future__ import annotations

import os

_SECRET_PREFIXES = ("GRAPHLM_", "TYPESAFE_")


def child_env() -> dict[str, str]:
    """A copy of ``os.environ`` without graphlm's own credentials and settings."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(_SECRET_PREFIXES)
    }
