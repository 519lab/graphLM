"""Provenance capture — the git SHA, timestamp, and package version the graph
was generated against.

This is the only module in graphlm that runs git (``git rev-parse HEAD`` —
never ``status``/``diff``, which would run a hostile repo's ``core.fsmonitor``
or pager). The only other subprocesses are the installers started by
``--upgrade`` / ``--setup`` (``upgrade.py``, ``setup.py``). All of them get
:func:`graphlm.child_env.child_env`, without graphlm's keys. The feature
that consumes it (the self-refreshing directive in ``render.py``) treats a
missing SHA as normal, not an error, so every function here is failure-tolerant:
a non-git project, git not installed, or an empty/brand-new repo yields
``commit_sha=None`` and never raises.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from graphlm.child_env import child_env

# A git commit hash: 40 hex chars (SHA-1) or 64 (SHA-256, `git init
# --object-format=sha256`). We validate the SHA rather than trusting exit codes
# alone because an *empty* repo's `git rev-parse HEAD` prints the literal string
# "HEAD" to stdout (with a non-zero exit) — verified, exit 128 — and we must not
# stamp that as a commit. Requiring a hash shape rejects it cleanly.
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

# Timeout for the git call. A rev-parse is near-instant; the cap only guards
# against a pathological/hung git so provenance capture can never stall a run.
_GIT_TIMEOUT = 5.0


def now_utc_iso() -> str:
    """Current UTC time as an ISO 8601 string, e.g. ``2026-08-30T14:22:05Z``.

    Second precision, ``Z`` suffix — human context in the stamp, never the
    staleness trigger (that is the SHA).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def graphlm_version() -> str | None:
    """The installed graphlm package version, or ``None`` if it can't be read.

    Sourced from installed package metadata so it tracks ``pyproject.toml``
    without a hand-maintained ``__version__``. Best-effort: a source checkout
    that was never installed (no dist metadata) yields ``None`` rather than
    raising.
    """
    try:
        from importlib.metadata import version

        return version("graphlm")
    except Exception:
        return None


def git_commit_sha(project_dir: str | Path) -> str | None:
    """Return the HEAD commit SHA for ``project_dir``, or ``None``.

    ``None`` is the normal, silent result for every non-happy path — the
    project is not a git repo, git is not installed, the repo has no commits
    yet, or git otherwise fails. Never raises.

    If ``project_dir`` is a *subdirectory* of a git repo, this returns the
    containing repo's HEAD (git's own behavior). That is intentional: the
    containing repo's HEAD is the correct staleness anchor for a graph of that
    subtree — there is no separate SHA for a subdirectory.

    A result is accepted only when git exits 0 *and* stdout is a real commit
    hash (40 or 64 hex chars). This rejects the empty-repo case, where
    ``git rev-parse HEAD`` prints the literal ``HEAD`` with a non-zero exit.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        # git not on PATH, cwd vanished, timeout, etc. — all "no SHA".
        return None

    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    if _SHA_RE.match(sha):
        return sha
    return None
