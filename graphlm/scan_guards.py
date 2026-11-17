"""Filesystem safety guards for the scanner — symlink escape, nested checkouts,
and reading the project's ``.graphlmignore`` safely.

Split out of ``scanner.py`` (600-line limit). Groups the "is this path safe to
follow / read?" concerns: a symlink that hides a sensitive target, a child dir
that is really another git checkout, project containment, and the ignore-file
reader (which is a safe file read — it rejects an escaping symlink and never
raises — not a pattern matcher, so it lives here beside ``_path_is_inside``).

Imports `exclusions` (`_ALWAYS_EXCLUDE`) and `sensitive_files` (`_is_sensitive_file`);
`scanner → scan_guards → exclusions → freshness → models` has no cycle.
"""

from __future__ import annotations

import logging
from pathlib import Path

from graphlm.exclusions import _ALWAYS_EXCLUDE, _should_exclude
from graphlm.sensitive_files import _is_sensitive_file

logger = logging.getLogger(__name__)


def _symlink_hides_sensitive(
    path: Path, exclude: tuple[str, ...] | None, project_dir: Path
) -> bool:
    """True when ``path`` is a symlink to a never-read file or excluded path.

    Guards inspect the *link name*; ``read_text`` follows the target, so
    ``crypto.py → .ssh/id_rsa`` used to send the key body to the LLM, and
    ``config.yaml → secrets/prod.yaml`` still would when ``secrets`` is
    only in ``.graphlmignore``.

    The target is matched exactly as the walk matches a real file: its path
    *relative to the project* through ``_should_exclude``. So a path-style
    pattern (``deploy/prod.yaml``, ``secrets/*.yaml``) blocks a link to that
    file, and the directories *above* the project (a checkout under
    ``~/build/``) never match an exclude like ``build``.
    """
    if not path.is_symlink():
        return False
    try:
        # strict: a broken link or a loop raises (OSError) on every Python
        # version — 3.13 would otherwise return a looped path unresolved.
        target = path.resolve(strict=True)
        root = project_dir.resolve()
    except (OSError, RuntimeError):  # RuntimeError: symlink loop on 3.11/3.12
        return True
    if _is_sensitive_file(target):
        return True
    try:
        rel_target = target.relative_to(root)
    except ValueError:
        # Outside the project. The walk already skips escaping links before
        # this check; refuse here too rather than match an absolute path.
        return True
    pats = exclude if exclude is not None else tuple(_ALWAYS_EXCLUDE)
    return _should_exclude(str(rel_target), pats)


# Characters that end a line for a Markdown renderer, a tokenizer or a text
# editor. A file or directory name holding one would split its tree line and
# let the name write its own lines into the pass-1 prompt and GRAPH.md.
_LINE_BREAKING = frozenset("\x85\u2028\u2029")


def _unsafe_name(name: str) -> bool:
    """True for a name with a control character (newline, CR, ESC, …) or a
    Unicode line/paragraph separator. Such entries are skipped by the walk:
    no real project needs them, and they are the way a hostile tree breaks
    out of the directory listing."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _LINE_BREAKING for ch in name)


def _is_nested_checkout(dir_path: Path) -> bool:
    """True if ``dir_path`` is the root of another git checkout.

    A git worktree or submodule marks its root with a ``.git`` *file* (a
    pointer into the parent's gitdir), a vendored clone with a ``.git``
    directory; ``exists()`` covers both. Such a subtree is a different project
    — merging it into the parent's map duplicates every module and edge under a
    second prefix (observed with agent worktrees under ``.claude/worktrees/``
    and would equally hit submodules). The scan root itself is never tested
    here (only children are), so scanning a repo is unaffected.
    """
    try:
        return (dir_path / ".git").exists()
    except OSError:
        return False


def _path_is_inside(project_dir: Path, target: Path) -> bool:
    """Check if target path is inside (or equal to) project_dir.

    Handles symlinks by resolving the parent of each path component,
    which prevents symlink traversal attacks.
    """
    try:
        # Resolve both paths
        project_resolved = project_dir.resolve()
        target_resolved = target.resolve()
        # Check containment via commonpath
        common = str(project_resolved)
        return str(target_resolved).startswith(common + "/") or str(target_resolved) == common
    except (ValueError, OSError, RuntimeError):
        # RuntimeError: Python 3.11/3.12 report a symlink loop that way
        # (3.13 returns the unresolved path). A loop is not "inside".
        return False


def load_graphlmignore(project_dir: Path) -> tuple[str, ...]:
    """Patterns from ``.graphlmignore`` (gitignore-lite). Missing → ``()``.

    One glob per line; ``#`` comments and blanks skipped; trailing ``/``
    stripped so ``.godot/`` matches the ``.godot`` path component. Never
    raises — unreadable / non-UTF-8 / escaping-symlink files are skipped.
    """
    path = project_dir / ".graphlmignore"
    try:
        if path.is_symlink() and not _path_is_inside(project_dir, path):
            return ()
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError):
        logger.warning("Could not read %s; ignoring it", path)
        return ()
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.rstrip("/")
        if line:
            out.append(line)
    return tuple(out)
