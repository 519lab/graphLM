"""Old-vs-new equivalence for exclusion and the fused directory walk.

The oracles below are the pre-change algorithms: ``fnmatch`` on every pattern,
then a pathlib tree walk plus a separate ``os.walk`` for the file list. They
must stay frozen — the production scan is only allowed to change how the work
is done, not which paths are excluded, which files are selected, or the tree
text.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

import pytest

from graphlm.exclusions import _ALWAYS_EXCLUDE, _should_exclude
from graphlm.scanner import (
    FileFragment,
    ScanResult,
    _BINARY_EXTS,
    _MAX_TREE_ENTRIES_PER_DIR,
    _SOURCE_EXTS,
    _TREE_TOTAL_LINE_MULTIPLIER,
    _is_binary,
    estimate_tokens,
    scan_project,
)
from graphlm.scan_guards import (
    _is_nested_checkout,
    _path_is_inside,
    _symlink_hides_sensitive,
    load_graphlmignore,
)
from graphlm.redact import _is_sensitive_file, _redact_secrets
from graphlm.testpaths import is_test_path

FIXTURES = Path(__file__).parent / "fixtures"

# Paths that exercise globs, trailing slashes, separators, and components.
_SYNTHETIC_PATHS = (
    "",
    "foo",
    "Foo",
    "foo/bar",
    "foo/bar/",
    "src/main.py",
    "src/main.py/extra",
    "src\\main.py",
    "skipdir",
    "skipdir/",
    "skipdir/a.py",
    "notes/a.py",
    "pkg/secret/f.py",
    "pkg/secretary.py",
    "file[1",
    "file[1.py",
    "file1.py",
    "file[123].py",
    "file4.py",
    "a.egg-info",
    "pkg/foo.egg-info/PKG-INFO",
    "build/output.js",
    "build/a/b.js",
    "build",
    "GRAPHICS.md",
    "GRAPH.md",
    ".graphlm/GRAPH.md",
    "a/b",
    "a/bc",
    "?",
    "*",
    "gen/out.js",
    "gen/out.ts",
    "debug.log",
    "kept/skip_me.py",
    "kept/stay.py",
)


def _legacy_should_exclude(rel_path: str, exclude_patterns: tuple[str, ...]) -> bool:
    """The pre-change matcher: fnmatch on the full path and on every component."""
    parts = Path(rel_path).parts
    for pattern in exclude_patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


def _legacy_rank(rel_path: str) -> int:
    name = rel_path.lower()
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
    if Path(name).suffix in _SOURCE_EXTS:
        return 5
    return 8


def _legacy_scan_project(
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
    """Two-pass scan: pathlib tree walk, then os.walk for the file list."""
    project_dir = project_dir.resolve()
    if not project_dir.is_dir():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    ignore_pats = load_graphlmignore(project_dir) if use_graphlmignore else ()
    all_exclude = tuple(_ALWAYS_EXCLUDE | set(exclude_patterns) | set(ignore_pats))

    tree_lines = [str(project_dir.name) + "/"]
    skipped_count = 0
    max_tree_lines = max_tree_entries_per_dir * _TREE_TOTAL_LINE_MULTIPLIER
    tree_stopped = False

    def _walk_dir(dir_path: Path, indent: str = "  ") -> None:
        nonlocal skipped_count, tree_stopped
        if tree_stopped:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except PermissionError:
            skipped_count += 1
            return

        listable: list[Path] = []
        for entry in entries:
            rel_str = str(entry.relative_to(project_dir))
            if _legacy_should_exclude(rel_str, all_exclude):
                skipped_count += 1
                continue
            if entry.is_symlink() and not _path_is_inside(project_dir, entry):
                skipped_count += 1
                continue
            if entry.is_symlink() and entry.is_dir():
                skipped_count += 1
                continue
            if entry.is_dir() and _is_nested_checkout(entry):
                skipped_count += 1
                continue
            if not entry.is_dir():
                if _is_binary(entry):
                    skipped_count += 1
                    continue
                if _is_sensitive_file(entry) or _symlink_hides_sensitive(entry, all_exclude):
                    skipped_count += 1
                    continue
                if not include_tests and is_test_path(rel_str):
                    skipped_count += 1
                    continue
            listable.append(entry)

        shown = listable[:max_tree_entries_per_dir]
        omitted = len(listable) - len(shown)
        for entry in shown:
            if len(tree_lines) >= max_tree_lines:
                tree_stopped = True
                return
            if entry.is_dir():
                tree_lines.append(f"{indent}{entry.name}/")
                _walk_dir(entry, indent + "  ")
            else:
                tree_lines.append(f"{indent}{entry.name}")
        if omitted:
            tree_lines.append(
                f"{indent}… {omitted} more entries not shown "
                f"(tree capped at {max_tree_entries_per_dir} per directory)"
            )
            skipped_count += omitted

    _walk_dir(project_dir)
    if tree_stopped:
        tree_lines.append(
            f"… tree truncated at {max_tree_lines} total lines "
            "(too many directories to list in full)"
        )
    tree = "\n".join(tree_lines)

    all_files: list[tuple[int, Path]] = []
    for root, dirs, files in os.walk(project_dir):
        current_root = Path(root)
        if not _path_is_inside(project_dir, current_root):
            dirs.clear()
            continue
        dirs[:] = [
            d
            for d in dirs
            if not _legacy_should_exclude(
                str(Path(root, d).relative_to(project_dir)), all_exclude
            )
            and (not Path(root, d).is_symlink() or _path_is_inside(project_dir, Path(root, d)))
            and not _is_nested_checkout(Path(root, d))
        ]
        for fname in files:
            fpath = Path(root, fname)
            rel = str(fpath.relative_to(project_dir))
            if _legacy_should_exclude(rel, all_exclude):
                continue
            if fpath.is_symlink() and not _path_is_inside(project_dir, fpath):
                skipped_count += 1
                continue
            if _is_binary(fpath):
                continue
            if _is_sensitive_file(fpath) or _symlink_hides_sensitive(fpath, all_exclude):
                skipped_count += 1
                continue
            if not include_tests and is_test_path(rel):
                continue
            try:
                all_files.append((_legacy_rank(rel), fpath))
            except (OSError, PermissionError):
                skipped_count += 1

    all_files.sort(key=lambda x: (x[0], str(x[1])))
    from graphlm.parsers.base import skeleton_for

    file_fragments: list[FileFragment] = []
    for _rank, fpath in all_files[:max_files]:
        if not _path_is_inside(project_dir, fpath):
            skipped_count += 1
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            line_count = content.count("\n") + 1
            if len(content) > max_file_chars:
                skel = skeleton_for(fpath, content.encode("utf-8")) if skeleton else None
                content = skel if skel is not None else content
                if len(content) > max_file_chars:
                    content = content[:max_file_chars]
            if redact_secrets:
                content = _redact_secrets(content)
            rel = str(fpath.relative_to(project_dir))
            file_fragments.append(
                FileFragment(rel, content, estimate_tokens(content), line_count)
            )
        except (OSError, PermissionError):
            skipped_count += 1

    return ScanResult(
        tree=tree,
        file_fragments=file_fragments,
        skipped_count=skipped_count,
        excluded_patterns=all_exclude,
    )


def _assert_equivalent(legacy: ScanResult, new: ScanResult, label: str) -> None:
    if legacy.tree != new.tree:
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                legacy.tree.splitlines(),
                new.tree.splitlines(),
                fromfile="legacy",
                tofile="new",
                lineterm="",
            )
        )
        raise AssertionError(f"{label}: tree mismatch\n{diff}")
    assert legacy.skipped_count == new.skipped_count, (
        f"{label}: skipped_count {legacy.skipped_count} != {new.skipped_count}"
    )
    assert legacy.excluded_patterns == new.excluded_patterns, label
    legacy_files = [
        (f.rel_path, f.content, f.estimated_tokens, f.line_count) for f in legacy.file_fragments
    ]
    new_files = [
        (f.rel_path, f.content, f.estimated_tokens, f.line_count) for f in new.file_fragments
    ]
    legacy_paths = [item[0] for item in legacy_files]
    new_paths = [item[0] for item in new_files]
    assert legacy_paths == new_paths, f"{label}: paths\nlegacy={legacy_paths}\nnew={new_paths}"
    for old, cur in zip(legacy_files, new_files, strict=True):
        assert old == cur, f"{label}: fragment {old[0]!r} differs"


def _compare(project: Path, label: str, **kwargs: object) -> ScanResult:
    legacy = _legacy_scan_project(project, **kwargs)  # type: ignore[arg-type]
    new = scan_project(project, **kwargs)  # type: ignore[arg-type]
    _assert_equivalent(legacy, new, label)
    return new


def _fixture_dirs() -> list[Path]:
    return sorted(path for path in FIXTURES.iterdir() if path.is_dir())


_SCAN_VARIANTS: list[tuple[str, dict[str, object]]] = [
    ("default", {}),
    ("no-tests", {"include_tests": False}),
    ("max-files-5", {"max_files": 5}),
    ("max-files-0", {"max_files": 0}),
    ("tree-cap-2", {"max_tree_entries_per_dir": 2}),
    ("no-skeleton", {"skeleton": False}),
    ("tiny-chars", {"max_file_chars": 80}),
    ("no-ignore", {"use_graphlmignore": False}),
    ("glob-exclude", {"exclude_patterns": ("*.md",)}),
    ("no-redact", {"redact_secrets": False}),
]


@pytest.mark.parametrize("fixture", _fixture_dirs(), ids=lambda path: path.name)
@pytest.mark.parametrize("variant", _SCAN_VARIANTS, ids=lambda item: item[0])
def test_fixtures_match_legacy_scan(fixture: Path, variant: tuple[str, dict[str, object]]) -> None:
    name, kwargs = variant
    _compare(fixture, f"{fixture.name}/{name}", **kwargs)


def test_exclude_matches_legacy_on_fixtures_and_synthetics() -> None:
    paths = set(_SYNTHETIC_PATHS)
    for fixture in _fixture_dirs():
        for dirpath, dirnames, filenames in os.walk(fixture):
            rel_root = Path(dirpath).relative_to(fixture)
            for name in [*dirnames, *filenames]:
                rel = name if str(rel_root) == "." else str(rel_root / name)
                paths.add(rel)
                paths.add(str(rel_root / name / "nested") if str(rel_root) != "." else f"{name}/nested")
    pattern_sets = [
        (),
        ("",),
        (".git",),
        ("node_modules",),
        ("*.egg-info",),
        ("build/*",),
        ("src/main.py",),
        ("skipdir/",),
        ("notes/",),
        ("secret",),
        ("file[1",),
        ("file[1.py",),
        ("file[123].py",),
        ("Foo",),
        ("*.py",),
        ("*.log",),
        ("a/b*",),
        ("foo*",),
        ("?",),
        ("*",),
        ("src\\main.py",),
        ("kept/skip_me.py",),
        ("gen/*.js",),
        tuple(_ALWAYS_EXCLUDE),
        (".git", "node_modules", "*.egg-info", "src/main.py", "build/*", "secret", "notes/"),
        ("Foo", "foo", "*.md", "pkg/secret"),
    ]
    for patterns in pattern_sets:
        for rel in paths:
            old = _legacy_should_exclude(rel, patterns)
            new = _should_exclude(rel, patterns)
            assert old is new, f"patterns={patterns!r} path={rel!r} legacy={old} new={new}"


def test_trailing_slash_pattern_does_not_exclude_the_directory(tmp_path: Path) -> None:
    """``notes/`` is exact and is not the path ``notes/a.py``. ``notes`` is."""
    project = tmp_path / "proj"
    (project / "notes").mkdir(parents=True)
    (project / "notes" / "a.py").write_text("kept = True\n")
    (project / "other.py").write_text("x = 1\n")

    slash = _compare(project, "trailing-slash", exclude_patterns=("notes/",))
    assert "notes/a.py" in {frag.rel_path for frag in slash.file_fragments}

    bare = _compare(project, "component-name", exclude_patterns=("notes",))
    assert "notes/a.py" not in {frag.rel_path for frag in bare.file_fragments}
    assert "other.py" in {frag.rel_path for frag in bare.file_fragments}


def test_glob_and_exact_full_path_exclude(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "kept").mkdir(parents=True)
    (project / "gen").mkdir()
    (project / "debug.log").write_text("noise\n")
    (project / "kept" / "skip_me.py").write_text("nope\n")
    (project / "kept" / "stay.py").write_text("yes\n")
    (project / "gen" / "out.js").write_text("bundled\n")
    (project / "gen" / "out.ts").write_text("typed\n")

    result = _compare(
        project,
        "glob-and-exact",
        exclude_patterns=("*.log", "kept/skip_me.py", "gen/*.js"),
    )
    paths = {frag.rel_path for frag in result.file_fragments}
    assert paths == {"kept/stay.py", "gen/out.ts"}


def test_graphlmignore_globs_components_and_trailing_slash(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "notes").mkdir(parents=True)
    (project / "kept").mkdir()
    (project / "notes" / "a.py").write_text("hidden\n")
    (project / "kept" / "skip_me.py").write_text("hidden\n")
    (project / "kept" / "stay.py").write_text("shown\n")
    (project / "debug.log").write_text("hidden\n")
    # The loader strips the trailing slash, so ``notes/`` becomes the component.
    (project / ".graphlmignore").write_text("# comment\n\nnotes/\n*.log\nkept/skip_me.py\n")

    result = _compare(project, "graphlmignore")
    assert {frag.rel_path for frag in result.file_fragments} == {"kept/stay.py"}

    opted_out = _compare(project, "graphlmignore-off", use_graphlmignore=False)
    assert "notes/a.py" in {frag.rel_path for frag in opted_out.file_fragments}


def test_omitted_directory_still_contributes_files(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    for i in range(5):
        directory = project / f"d{i}"
        directory.mkdir()
        (directory / "mod.py").write_text(f"n = {i}\n")
    (project / "late.py").write_text("late = True\n")

    result = _compare(project, "per-dir-cap", max_tree_entries_per_dir=2)
    paths = {frag.rel_path for frag in result.file_fragments}
    assert paths == {f"d{i}/mod.py" for i in range(5)} | {"late.py"}
    assert "  d0/" in result.tree
    assert "  d1/" in result.tree
    assert "d2/" not in result.tree
    assert "2 more entries not shown" not in result.tree  # 4 omitted: d2 d3 d4 late.py
    assert "4 more entries not shown" in result.tree


def test_total_line_ceiling_still_reads_unlisted_files(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    # A single spine. Cap 1 shows that one child per directory, so the per-dir
    # marker never fires; 30 levels blow the 25-line ceiling instead.
    cursor = project
    for i in range(30):
        cursor = cursor / f"n{i}"
        cursor.mkdir()
    (cursor / "leaf.py").write_text("leaf = 1\n")

    result = _compare(project, "line-ceiling", max_tree_entries_per_dir=1)
    leaf = "/".join(f"n{i}" for i in range(30)) + "/leaf.py"
    assert [frag.rel_path for frag in result.file_fragments] == [leaf]
    assert "tree truncated at 25 total lines" in result.tree
    assert "n29/" not in result.tree


def test_symlinks_nested_git_and_sensitive_files_match(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "a.py").write_text("public = 1\n")
    (project / "pkg" / "loop").symlink_to(project, target_is_directory=True)

    outside = tmp_path / "outside.txt"
    outside.write_text("SENTINEL_OUTSIDE\n")
    (project / "escaped.py").symlink_to(outside)
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "hidden.py").write_text("SENTINEL_OUTSIDE_DIR\n")
    (project / "escaped_dir").symlink_to(outside_dir, target_is_directory=True)

    (project / ".env").write_text("SENTINEL_SECRET=1\n")
    (project / "id_rsa").write_text("SENTINEL_KEY\n")
    (project / "via_env.py").symlink_to(project / ".env")
    (project / "alias.py").symlink_to(project / "pkg" / "a.py")
    (project / "pic.png").write_bytes(b"\x89PNG\r\n")

    worktree = project / "vendor" / "lib"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /somewhere\n")
    (worktree / "secret.py").write_text("SENTINEL_NESTED\n")

    clone = project / "third_party" / "dep"
    (clone / ".git").mkdir(parents=True)
    (clone / "dep.py").write_text("SENTINEL_CLONE\n")

    # In-project alias of a real package: listed once, under its real path.
    (project / "pkg_alias").symlink_to(project / "pkg", target_is_directory=True)

    result = _compare(project, "symlinks-and-nested", max_files=2)
    blob = "\n".join(frag.content for frag in result.file_fragments)
    for sentinel in (
        "SENTINEL_OUTSIDE",
        "SENTINEL_OUTSIDE_DIR",
        "SENTINEL_SECRET",
        "SENTINEL_KEY",
        "SENTINEL_NESTED",
        "SENTINEL_CLONE",
    ):
        assert sentinel not in blob
        assert sentinel not in result.tree
    assert "loop" not in result.tree
    assert "escaped_dir" not in result.tree
    assert "pkg_alias" not in result.tree
    assert "vendor/" in result.tree
    assert "lib/" not in result.tree
    # The escaping README-ranked names must not consume the two file slots.
    assert {frag.rel_path for frag in result.file_fragments} <= {
        "pkg/a.py",
        "alias.py",
        "vendor",
    }
    assert "pkg/a.py" in {frag.rel_path for frag in result.file_fragments}


def test_escaping_symlink_does_not_consume_max_files_slot(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("def main():\n    return 1\n")
    (project / "src" / "util.py").write_text("x = 1\n")
    outside = tmp_path / "README.md"
    outside.write_text("SENTINEL_README\n")
    # Dropped before ranking, so it cannot take the one max_files slot from
    # src/main.py (rank 2, ahead of src/util.py).
    (project / "README.md").symlink_to(outside)

    result = _compare(project, "symlink-slot", max_files=1)
    assert [frag.rel_path for frag in result.file_fragments] == ["src/main.py"]
    assert "SENTINEL_README" not in result.file_fragments[0].content


def test_rank_order_matches_under_max_files(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "docs").mkdir()
    (project / "tests").mkdir()
    (project / "README.md").write_text("readme\n")
    (project / "src" / "__init__.py").write_text("")
    (project / "src" / "main.py").write_text("main\n")
    (project / "src" / "util.py").write_text("util\n")
    (project / "docs" / "guide.md").write_text("guide\n")
    (project / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")

    result = _compare(project, "rank-order", max_files=3)
    # Config rank is a lowercase path looked up in a mixed-case name set, so
    # README.md stays with the other markdown. Init, main, then other source.
    assert [frag.rel_path for frag in result.file_fragments] == [
        "src/__init__.py",
        "src/main.py",
        "src/util.py",
    ]

    without_tests = _compare(project, "no-tests", include_tests=False)
    assert "tests/test_a.py" not in {frag.rel_path for frag in without_tests.file_fragments}
    assert "src/util.py" in {frag.rel_path for frag in without_tests.file_fragments}


def test_character_class_and_unclosed_bracket(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "file1.py").write_text("one = 1\n")
    (project / "file4.py").write_text("four = 4\n")
    (project / "file[1.py").write_text("literal = 1\n")

    classed = _compare(project, "char-class", exclude_patterns=("file[123].py",))
    assert {frag.rel_path for frag in classed.file_fragments} == {"file4.py", "file[1.py"}

    literal = _compare(project, "unclosed-bracket", exclude_patterns=("file[1.py",))
    assert {frag.rel_path for frag in literal.file_fragments} == {"file1.py", "file4.py"}


def test_binary_extension_constant_is_still_the_skip_list() -> None:
    """Guard the oracle against drifting off the production binary set."""
    assert ".png" in _BINARY_EXTS
