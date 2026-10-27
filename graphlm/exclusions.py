"""Path-exclusion patterns and matching — what the scanner skips by name.

The always-excluded set (`_ALWAYS_EXCLUDE`) and the matcher (`_should_exclude`),
split out of `scanner.py` (600-line limit). A leaf module: imports only stdlib
+ `freshness.STATE_FILENAME` (the one working-copy name), so
`scanner → scan_guards → exclusions → freshness → models` has no cycle.

Exact names (no glob character) are a set lookup. ``fnmatch`` runs only for
patterns that contain ``*``, ``?``, or ``[``.
"""

from __future__ import annotations

import fnmatch
import functools
import os
from pathlib import Path

from graphlm.freshness import STATE_FILENAME

# Patterns that are always excluded (in addition to user-specified ones)
_ALWAYS_EXCLUDE = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",  # Hypothesis example DB — thousands of cache files
    ".tox",
    ".eggs",
    "*.egg-info",
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".env",
    ".gitignore",
    ".gitkeep",
    ".graphlmignore",  # the ignore file itself — never sent to the LLM (#38)
    # Build / dist / cache output. These match on any path component, so a
    # legitimately-named source dir called "build"/"dist"/"target" anywhere in
    # the tree is also excluded — an accepted trade-off (precedent: bare "env"
    # above already excludes any component named env). They only remove files
    # from analysis; no security invariant depends on them. Together with the
    # per-directory tree cap, this keeps huge polyglot repos (Rust target/,
    # JS build/, hypothesis caches) from overflowing the LLM context (#17).
    "target",  # Rust/Java build output (crates/*/target/... is thousands of files)
    "build",
    "dist",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".terraform",
    ".gradle",
    "coverage",
    "htmlcov",
    ".idea",
    ".vscode",
    # graphlm's own output directory. The CLI writes GRAPH.* / GRAPH_DIFF.* into
    # a `.graphlm/` subdir of the scanned project by default, so excluding the
    # whole directory keeps graphlm from ingesting its own map (and diff, #28) as
    # source on a re-run. `_should_exclude` matches any path component, so this
    # drops `.graphlm/` and everything under it in one entry.
    ".graphlm",
    # graphlm's own output artifacts *by filename*, for the case where output is
    # redirected into the scanned tree with `-o` (or a library caller writes to
    # the project root) rather than the default `.graphlm/` dir. Named explicitly
    # (not a broad GRAPH*) so a user's GRAPHICS.md etc. is untouched.
    # NOTE: these are the *default* suffix ("GRAPH") only. write_outputs accepts
    # custom *_suffix / diff_suffix params, so a library caller writing e.g.
    # `map.json` / `map_DIFF.json` and then re-scanning that dir would re-ingest
    # them. Not reachable today (the CLI exposes no suffix flag), so it's not
    # live — but if a `--json-suffix` / `--diff-suffix` flag is ever added, make
    # this exclusion suffix-aware (or the self-ingestion bug reopens for it).
    "GRAPH.md",
    "GRAPH.json",
    "GRAPH.html",
    "GRAPH_DIFF.md",
    "GRAPH_DIFF.json",
    # graphlm's internal JSON working copy — always written for the diff baseline
    # / --serve, so exclude it from a re-scan the same way as the deliverables.
    # Sourced from the single definition in freshness.py (not a hardcoded copy).
    STATE_FILENAME,
}


# Characters that make ``fnmatch`` treat a pattern as a glob. A ``[`` with no
# closing ``]`` is a literal, but it still takes the fnmatch path so the result
# stays identical. Anything else is an exact name.
_GLOB_CHARS = frozenset("*?[")


@functools.lru_cache(maxsize=128)
def _compile_exclude(
    patterns: tuple[str, ...],
) -> tuple[frozenset[str], frozenset[str], tuple[str, ...]]:
    """Split patterns into exact component names, exact full paths, and globs.

    Slash-free exact names match any path component (and a single-component
    path). A pattern that contains a separator but no glob character matches
    only the full relative path — ``fnmatch`` does not treat ``/`` as special,
    so ``src/main.py`` does not exclude the component ``main.py``.
    """
    components: list[str] = []
    paths: list[str] = []
    globs: list[str] = []
    for pat in patterns:
        if any(ch in pat for ch in _GLOB_CHARS):
            globs.append(pat)
        elif "/" in pat or "\\" in pat:
            paths.append(os.path.normcase(pat))
        else:
            components.append(os.path.normcase(pat))
    return frozenset(components), frozenset(paths), tuple(globs)


def _should_exclude(rel_path: str, exclude_patterns: tuple[str, ...]) -> bool:
    """Check if a relative path matches any exclusion pattern."""
    # ``tuple()`` of an existing tuple is the same object, so the cache key is
    # stable across the thousands of calls in one scan. A list still works.
    patterns = tuple(exclude_patterns)
    exact_parts, exact_paths, globs = _compile_exclude(patterns)
    folded = os.path.normcase(rel_path)
    if folded in exact_paths or folded in exact_parts:
        return True
    parts = Path(rel_path).parts
    for part in parts:
        if os.path.normcase(part) in exact_parts:
            return True
    for pattern in globs:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False
