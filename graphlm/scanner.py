"""Project scanner — walk directory tree, read file contents, skip noise."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from graphlm.exclusions import _ALWAYS_EXCLUDE, _should_exclude
from graphlm.redact import _is_sensitive_file, _redact_secrets
from graphlm.scan_guards import (
    _is_nested_checkout,
    _path_is_inside,
    _symlink_hides_sensitive,
    load_graphlmignore,
)
from graphlm.testpaths import is_test_path

logger = logging.getLogger(__name__)

# Hard cap on how many listed children any one directory contributes to the
# pass-1 tree. Bounds tree size independently of the exclude list so a huge or
# junk-heavy directory (e.g. thousands of generated files the excludes missed)
# cannot blow the LLM context window (#17). See scan_project for the rationale
# on capping per-directory rather than globally.
_MAX_TREE_ENTRIES_PER_DIR = 200
# The total tree-line ceiling is this multiple of the per-directory cap. The
# per-directory cap alone bounds the tree at per_dir × (number of directories);
# this multiplier turns that into an absolute ceiling (default 200 × 25 = 5000
# lines ≈ well under the context budget) that holds no matter how many
# directories a repo has.
_TREE_TOTAL_LINE_MULTIPLIER = 25

# Binary extensions that should never be read as text
_BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".svg",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".whl",
    ".so",
    ".dll",
    ".dylib",
    ".o",
    ".pyc",
    ".pyo",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
}

# Source-code extensions that should NOT be excluded by name patterns
# to avoid false positives (e.g. token.py, credentials.py).
_SOURCE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go", ".rs", ".java", ".cs", ".cpp", ".c", ".h", ".hpp", ".cc", ".cxx", ".hh", ".hxx", ".php"}


class FileFragment:
    """A single file's path and content for inclusion in LLM context.

    ``line_count`` is the *on-disk* physical line count (``newlines + 1``, the
    convention ``compute_sloc_map`` has always used), captured before any cut
    so cycle risk scores weigh a large file by its real size, not by the
    ``max_file_chars`` slice. Defaults to counting ``content`` for hand-built
    fragments.
    """

    __slots__ = ("rel_path", "content", "estimated_tokens", "line_count")

    def __init__(
        self,
        rel_path: str,
        content: str,
        estimated_tokens: int,
        line_count: int | None = None,
    ) -> None:
        self.rel_path = rel_path
        self.content = content
        self.estimated_tokens = estimated_tokens
        self.line_count = line_count if line_count is not None else content.count("\n") + 1


class ScanResult:
    """Result of scanning a project directory."""

    __slots__ = ("tree", "file_fragments", "skipped_count", "excluded_patterns")

    def __init__(
        self,
        tree: str,
        file_fragments: list[FileFragment],
        skipped_count: int,
        excluded_patterns: tuple[str, ...],
    ) -> None:
        self.tree = tree
        self.file_fragments = file_fragments
        self.skipped_count = skipped_count
        self.excluded_patterns = excluded_patterns


def estimate_tokens(text: str) -> int:
    """Fast heuristic token count, calibrated conservatively.

    Real measurement against the served model on graphlm's own content (dense
    directory trees + source code) put the ratio at ~2.83–2.87 bytes/token, so
    the old ~4-bytes/token assumption *under*-counted by ~28% — enough to pack a
    prompt the model then rejected or timed out on (issue #17). We deliberately
    assume 2.5 bytes/token (``* 2 // 5``) so the estimate sits ~13% *above* the
    real count: over-estimating trims a few extra files but keeps the budget a
    real guarantee, whereas under-estimating overflows the context. This is the
    single source of truth for the heuristic — ``context.py`` imports it.
    """
    return len(text.encode("utf-8")) * 2 // 5


def _is_binary(path: Path) -> bool:
    """Check if a file is likely binary by extension."""
    return path.suffix.lower() in _BINARY_EXTS


def scan_project(
    project_dir: Path,
    *,
    max_file_chars: int = 4000,
    max_files: int = 200,
    include_tests: bool = True,
    exclude_patterns: tuple[str, ...] = (),
    redact_secrets: bool = True,
    max_tree_entries_per_dir: int = _MAX_TREE_ENTRIES_PER_DIR,
    skeleton: bool = True,
    use_graphlmignore: bool = True,
) -> ScanResult:
    """Walk project directory, build annotated tree, read file contents.

    Args:
        project_dir: Root directory to scan.
        max_file_chars: Maximum characters to send per file. A longer file is
            sent as its signature skeleton when ``skeleton`` is on and its
            language has a renderer (Python today); only if the skeleton is
            *still* over the cap, or no renderer exists, is it cut to the cap.
        max_files: Maximum number of source files to include in context.
        include_tests: Whether to include test files.
        exclude_patterns: Additional glob patterns to exclude.
        use_graphlmignore: If True (default), merge patterns from a
            ``.graphlmignore`` at the project root into the exclude set.
            Pass False / ``--no-graphlmignore`` to ignore the file.
        redact_secrets: If True, redact secret-like patterns from file content
            (after skeletonisation — docstring lines/constants can hold secrets).
        skeleton: If True (default), replace an oversized file with its
            tree-sitter signature skeleton — imports, class/def headers,
            docstring first lines, short constants, bodies elided — i.e. the
            whole API surface instead of the first class and nothing else.
            Pass False / ``--no-skeleton`` to send the head of the file.
        max_tree_entries_per_dir: Cap on how many *listed* children (after
            excludes) any single directory contributes to the pass-1 tree.
            Beyond it, a "… N more entries not shown" marker is emitted and the
            rest of that directory's children are omitted from the tree. This
            is a hard bound on tree size that does not depend on the exclude
            list catching every junk dir — the tree (the whole pass-1 prompt)
            would otherwise grow unbounded with repo size and overflow the LLM
            context on large/polyglot repos (#17). Capping per-directory rather
            than globally keeps the budget spread across the tree so deeply
            nested source dirs stay visible instead of being crowded out by an
            early alphabetical cache directory. It bounds only the tree text,
            not which files are read (that is ``max_files``).

    Returns:
        ScanResult with tree string and file fragments.
    """
    project_dir = project_dir.resolve()
    if not project_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    ignore_pats = load_graphlmignore(project_dir) if use_graphlmignore else ()
    all_exclude = tuple(_ALWAYS_EXCLUDE | set(exclude_patterns) | set(ignore_pats))

    # One scandir walk builds the directory tree and the ranked file list.
    # The tree still stops at the per-directory cap and the total-line ceiling;
    # file collection keeps going through directories the tree did not list
    # (omitted tail, and everything past the line ceiling). That split is what
    # the old tree walk + os.walk pair did. skipped_count stays the sum of both
    # old walks: a directory the tree entered counts the tree's skips, and
    # sensitive files plus escaping symlink *files* are also counted by the
    # file walk (so a tree-entered sensitive file counts twice).
    tree_lines = [str(project_dir.name) + "/"]
    skipped_count = 0
    # Absolute ceiling on total tree lines — the per-directory cap alone only
    # bounds the tree at max_tree_entries_per_dir × (number of directories), so
    # a repo with thousands of directories could still overflow. This total
    # backstop makes "the pass-1 tree is bounded" a real guarantee regardless of
    # directory count (#17). Once hit, the tree stops growing and a final
    # marker is appended after the loop. File collection is not stopped.
    max_tree_lines = max_tree_entries_per_dir * _TREE_TOTAL_LINE_MULTIPLIER
    tree_stopped = False
    all_files: list[tuple[int, Path]] = []

    def _rank_file(rel_path: str) -> int:
        """Lower number = higher priority."""
        name = rel_path.lower()
        # Top-level config files get highest priority
        config_files = {
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "Gemfile",
            "Makefile",
            "Dockerfile",
            "docker-compose.yml",
            ".env.example",
            "README.md",
            "setup.py",
            "setup.cfg",
            "tsconfig.json",
            "next.config.js",
            "vite.config.js",
            "webpack.config.js",
            "tailwind.config.js",
            "postcss.config.js",
            "eslintrc.js",
            "prettier.config.js",
            "rust-toolchain",
            ".dockerignore",
            ".gitignore",
        }
        if name in config_files:
            return 0
        if name.endswith("/__init__.py") or name.endswith("/__init__.js"):
            return 1
        if name.endswith("/main.py") or name.endswith("/main.js"):
            return 2
        if is_test_path(rel_path):
            return 10
        # Source code outranks non-source text (docs, data, configs not already
        # prioritized above). Under a tight max_files cap, a doc-heavy repo (e.g.
        # argus: 91 .md vs 135 .py) would otherwise let markdown crowd out the
        # actual modules — starving the AST import graph, since edges only
        # resolve between *scanned* files (#19). Source gets 5, everything else 8.
        if Path(name).suffix in _SOURCE_EXTS:
            return 5
        return 8

    def _scan_dir(dir_path: Path, indent: str, *, tree_visit: bool) -> None:
        nonlocal skipped_count, tree_stopped
        # tree_visit: the old tree walk entered this directory, so its skips
        # and its tree lines apply. False: only the old file walk saw it.
        # Stat while the scandir iterator is still open. DirEntry results are
        # not guaranteed once it closes.
        rows: list[tuple[bool, str, Path, bool, bool]] = []
        try:
            with os.scandir(dir_path) as iterator:
                for dir_entry in iterator:
                    try:
                        is_dir = dir_entry.is_dir()
                    except OSError:
                        is_dir = False
                    try:
                        is_link = dir_entry.is_symlink()
                    except OSError:
                        is_link = False
                    rows.append(
                        (not is_dir, dir_entry.name, Path(dir_entry.path), is_dir, is_link)
                    )
        except PermissionError:
            if tree_visit:
                skipped_count += 1
            return
        except OSError:
            # os.walk swallows scandir errors. The tree walk only caught
            # PermissionError, so a tree-entered directory still raises.
            if tree_visit:
                raise
            return
        # Dirs first, then name — same key as Path.iterdir. Names are unique,
        # so the order does not depend on scandir's unordered yield.
        rows.sort(key=lambda row: (row[0], row[1]))

        listable: list[tuple[Path, bool]] = []
        for _files_after_dirs, _name, path, is_dir, is_link in rows:
            rel_str = str(path.relative_to(project_dir))

            if _should_exclude(rel_str, all_exclude):
                if tree_visit:
                    skipped_count += 1
                continue

            # Skip any symlink (file or directory) that points outside the
            # project — otherwise an external symlinked file is listed in the
            # tree and consumes a max_files slot. Checked before any read.
            if is_link and not _path_is_inside(project_dir, path):
                if tree_visit:
                    skipped_count += 1
                # The file walk counts escaping symlink files again. Escaping
                # directory symlinks are only counted by the tree walk.
                if not is_dir:
                    skipped_count += 1
                continue
            # Directory symlinks, even in-project, are not recursed
            # (followlinks=False). Listing them advertised paths pass 2 cannot
            # open, and a cycle (sub/loop → project root) exploded the tree
            # (#97).
            if is_link and is_dir:
                if tree_visit:
                    skipped_count += 1
                continue

            # A nested git checkout (worktree, submodule, vendored clone) is a
            # separate project: keep it out of the tree and the file list.
            if is_dir and _is_nested_checkout(path):
                if tree_visit:
                    skipped_count += 1
                continue

            if not is_dir:
                if _is_binary(path):
                    if tree_visit:
                        skipped_count += 1
                    continue
                if _is_sensitive_file(path) or _symlink_hides_sensitive(path, all_exclude):
                    # Both old walks counted a sensitive file the tree saw.
                    if tree_visit:
                        skipped_count += 1
                    skipped_count += 1
                    continue
                if not include_tests and is_test_path(rel_str):
                    if tree_visit:
                        skipped_count += 1
                    continue

            listable.append((path, is_dir))

        pending_dirs: list[Path] = []
        if tree_visit:
            shown = listable[:max_tree_entries_per_dir]
            omitted = listable[max_tree_entries_per_dir:]
            cursor = 0
            while cursor < len(shown):
                # Total-lines backstop: stop the tree once it reaches its
                # absolute ceiling. File collection below still runs.
                if len(tree_lines) >= max_tree_lines:
                    tree_stopped = True
                    break
                path, is_dir = shown[cursor]
                cursor += 1
                # Basename + indent, not the full relative path. Restating
                # `pkg/sub/deep.py` on every line (on top of indent that
                # already encodes ancestry) made the 5000-line cap still
                # overflow max_context on deep monorepos — n8n's tree was
                # ~151k tokens and zeroed the pass-2 edge budget (#69).
                if is_dir:
                    tree_lines.append(f"{indent}{path.name}/")
                    _scan_dir(path, indent + "  ", tree_visit=True)
                else:
                    tree_lines.append(f"{indent}{path.name}")
            # The omitted marker is emitted only when the line ceiling did not
            # return early — including when the ceiling tripped inside the last
            # shown child and this directory still had an omitted tail.
            if cursor == len(shown) and omitted:
                tree_lines.append(
                    f"{indent}… {len(omitted)} more entries not shown "
                    f"(tree capped at {max_tree_entries_per_dir} per directory)"
                )
                skipped_count += len(omitted)
            if cursor < len(shown):
                pending_dirs.extend(path for path, is_dir in shown[cursor:] if is_dir)
            pending_dirs.extend(path for path, is_dir in omitted if is_dir)
        else:
            pending_dirs.extend(path for path, is_dir in listable if is_dir)

        for path, is_dir in listable:
            if is_dir:
                continue
            rel = str(path.relative_to(project_dir))
            try:
                all_files.append((_rank_file(rel), path))
            except (OSError, PermissionError):
                skipped_count += 1

        for child in pending_dirs:
            _scan_dir(child, indent + "  ", tree_visit=False)

    _scan_dir(project_dir, "  ", tree_visit=True)
    if tree_stopped:
        tree_lines.append(
            f"… tree truncated at {max_tree_lines} total lines "
            "(too many directories to list in full)"
        )
    tree = "\n".join(tree_lines)

    # Priority ranking already stamped each path. Sort, then read.
    file_fragments: list[FileFragment] = []
    file_count = 0

    # Sort by rank, then by path for determinism
    all_files.sort(key=lambda x: (x[0], str(x[1])))

    # Function-local import: graphlm.parsers.base imports FileFragment from this
    # module at module scope, so a top-level import here would be a cycle (the
    # same reason parsers/base.py defers its own language-module import).
    from graphlm.parsers.base import skeleton_for

    # Read up to max_files
    for _rank, fpath in all_files[:max_files]:
        # Double-check the resolved path is inside project
        if not _path_is_inside(project_dir, fpath):
            skipped_count += 1
            continue

        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            # Captured before any cut so cycle scoring sees the real size.
            line_count = content.count("\n") + 1
            truncated = False
            if len(content) > max_file_chars:
                # Prefer the signature skeleton (every symbol) over the head of
                # the file (the first few). The cap applies only if the skeleton
                # is *still* over it (bodies already gone) or no renderer exists.
                skel = skeleton_for(fpath, content.encode("utf-8")) if skeleton else None
                content = skel if skel is not None else content
                if len(content) > max_file_chars:
                    content = content[:max_file_chars]
                truncated = True

            # Redact secret-like patterns — deliberately AFTER skeletonisation,
            # since docstring first lines / short constants survive into it.
            if redact_secrets:
                content = _redact_secrets(content)

            rel = str(fpath.relative_to(project_dir))
            tokens = estimate_tokens(content)
            file_fragments.append(FileFragment(rel, content, tokens, line_count))
            file_count += 1
            if truncated:
                logger.debug("Truncated %s to %d chars", rel, max_file_chars)
        except (OSError, PermissionError) as e:
            logger.warning("Could not read %s: %s", fpath, e)
            skipped_count += 1

    return ScanResult(
        tree=tree,
        file_fragments=file_fragments,
        skipped_count=skipped_count,
        excluded_patterns=all_exclude,
    )
