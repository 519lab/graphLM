"""Scanner robustness against hostile trees.

Each test builds the tree a hostile repository (or a tarball, or a build step)
can produce and checks the scan stays bounded and correct: no hang on special
files, no unbounded read, no recursion limit, no line-breaking names in the
tree, and no excluded file read through a symlink.
"""

from __future__ import annotations

import os
import socket
import threading
import tracemalloc
from pathlib import Path

import pytest

from graphlm import bounded_read
from graphlm.bounded_read import read_text_bounded
from graphlm.context import assemble_pass1_prompt
from graphlm.parsers import base as parsers_base
from graphlm.scanner import FileFragment, scan_project


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True)
    (proj / "pkg" / "__init__.py").write_text("")
    (proj / "pkg" / "core.py").write_text("import os\n")
    return proj


def _scan_with_timeout(proj: Path, seconds: float = 20.0, **kwargs):
    """Run scan_project in a daemon thread; fail (not hang) if it blocks."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["result"] = scan_project(proj, **kwargs)
        except BaseException as e:  # surfaced below
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "scan_project blocked (did it open a special file?)"
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]


# --------------------------------------------------------------------------- #
# Special files (FIFO, socket) are never opened
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_fifo_is_skipped_not_opened(tmp_path):
    proj = _project(tmp_path)
    os.mkfifo(proj / "notes.txt")
    result = _scan_with_timeout(proj)
    assert "notes.txt" not in result.tree
    assert all(f.rel_path != "notes.txt" for f in result.file_fragments)
    assert any(f.rel_path == "pkg/core.py" for f in result.file_fragments)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no unix sockets")
def test_unix_socket_is_skipped(tmp_path):
    proj = _project(tmp_path)
    sock_path = proj / "s.sock"
    if len(str(sock_path)) > 100:  # AF_UNIX path limit
        pytest.skip("tmp path too long for a unix socket")
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(sock_path))
        result = _scan_with_timeout(proj)
    finally:
        s.close()
    assert "s.sock" not in result.tree


def test_broken_symlink_is_not_listed(tmp_path):
    proj = _project(tmp_path)
    (proj / "dangling.py").symlink_to(proj / "missing.py")
    result = scan_project(proj)
    assert "dangling.py" not in result.tree
    assert all(f.rel_path != "dangling.py" for f in result.file_fragments)


# --------------------------------------------------------------------------- #
# Names that would break the tree's line structure are skipped
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sep", ["\n", "\r", " ", "\x1b"])
def test_line_breaking_name_is_skipped(tmp_path, sep):
    proj = _project(tmp_path)
    marker = "INERT_MARKER_LINE"
    name = f"a.txt{sep}```{sep}{marker}"
    try:
        (proj / name).write_text("hello")
    except OSError:
        pytest.skip("filesystem rejects this name")
    sub = proj / f"dir{sep}{marker}2"
    sub.mkdir()
    (sub / "x.py").write_text("x = 1\n")
    result = scan_project(proj)
    assert marker not in result.tree
    assert marker not in assemble_pass1_prompt(result.tree)
    assert all(marker not in f.rel_path for f in result.file_fragments)
    # Ordinary entries are unaffected.
    assert "core.py" in result.tree


def test_ordinary_unicode_and_punctuation_names_are_kept(tmp_path):
    proj = _project(tmp_path)
    for name in ("héllo.py", "page [id].tsx", "a b.md", "tab\x7fless.txt"):
        try:
            (proj / name).write_text("x")
        except OSError:
            pass
    tree = scan_project(proj).tree
    assert "héllo.py" in tree and "page [id].tsx" in tree and "a b.md" in tree
    assert "tab\x7fless.txt" not in tree  # DEL is a control character


# --------------------------------------------------------------------------- #
# Reads are bounded; the line count stays exact
# --------------------------------------------------------------------------- #
def test_huge_file_is_not_read_whole(tmp_path):
    proj = _project(tmp_path)
    big = proj / "data.csv"
    row = "a,b,c,d,e,f,g,h,i,j\n"  # 20 bytes
    n_rows = 2_000_000  # 40 MB, 10x the 4 MiB read bound
    with open(big, "w") as fh:
        for _ in range(n_rows // 1000):
            fh.write(row * 1000)
    tracemalloc.start()
    try:
        result = scan_project(proj)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    frag = next(f for f in result.file_fragments if f.rel_path == "data.csv")
    assert len(frag.content) <= 4000
    assert frag.line_count == n_rows + 1  # every newline counted, + 1
    # The whole file read as one str is ~40 MB (plus a copy); bounded ≈ 4 MiB.
    assert peak < 20 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB"


def test_read_text_bounded_matches_read_text_when_within_bound(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"a\r\nb\rc\nd\xff\xfee\n")
    text, lines, complete = read_text_bounded(p, 1000)
    assert complete
    assert text == p.read_text(encoding="utf-8", errors="replace")
    assert lines == text.count("\n") + 1


def test_read_text_bounded_counts_crlf_across_chunk_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(bounded_read, "_STREAM_CHUNK", 3)
    p = tmp_path / "f.txt"
    payload = ("xy\r\n" * 50) + "tail\rz\n"
    p.write_bytes(payload.encode())
    expected = p.read_text(encoding="utf-8", errors="replace")
    text, lines, complete = read_text_bounded(p, 5)
    assert not complete
    assert text == expected[:5]
    assert lines == expected.count("\n") + 1


def test_ast_pass_reads_a_bounded_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers_base, "MAX_SOURCE_READ", 64, raising=False)
    (tmp_path / "big.py").write_text("import os\n" + "x = 1\n" * 1000)
    frag = FileFragment("big.py", "", 0, None)
    code = parsers_base._source_bytes(frag, tmp_path)
    assert code is not None and len(code) == 64
    assert code.startswith(b"import os\n")


# --------------------------------------------------------------------------- #
# Deep nesting does not hit the recursion limit
# --------------------------------------------------------------------------- #
def test_deeply_nested_tree_scans_without_recursion_error(tmp_path):
    proj = _project(tmp_path)
    depth = 1100  # past CPython's default recursion limit of 1000
    # One level at a time: mkdir(parents=True) itself recurses per level, and
    # so does shutil.rmtree on 3.11 — which is what pytest's tmp_path cleanup
    # uses — so the test removes its own tree bottom-up afterwards.
    made: list[Path] = []
    deep = proj
    try:
        try:
            for _ in range(depth):
                deep = deep / "d"
                deep.mkdir()
                made.append(deep)
        except OSError:
            pytest.skip("path too long for this filesystem")
        (deep / "leaf.py").write_text("x = 1\n")
        result = scan_project(proj, max_files=5000)
        leaf = "/".join(["d"] * depth) + "/leaf.py"
        assert any(f.rel_path == leaf for f in result.file_fragments)
    finally:
        (deep / "leaf.py").unlink(missing_ok=True)
        for d in reversed(made):
            d.rmdir()


# --------------------------------------------------------------------------- #
# Symlinks cannot read a path the user excluded, and in-project links survive
# a project that lives under a directory named like an exclude
# --------------------------------------------------------------------------- #
def test_symlink_to_path_style_exclude_is_not_read(tmp_path):
    proj = _project(tmp_path)
    (proj / "deploy").mkdir()
    (proj / "deploy" / "prod.yaml").write_text("INERT_EXCLUDED_CONTENT\n")
    (proj / ".graphlmignore").write_text("deploy/prod.yaml\n")
    (proj / "readme_link.yaml").symlink_to(proj / "deploy" / "prod.yaml")
    result = scan_project(proj)
    assert all("INERT_EXCLUDED_CONTENT" not in f.content for f in result.file_fragments)
    assert "readme_link.yaml" not in result.tree


def test_glob_path_exclude_blocks_symlink_too(tmp_path):
    proj = _project(tmp_path)
    (proj / "secrets").mkdir()
    (proj / "secrets" / "db.yaml").write_text("INERT_EXCLUDED_CONTENT\n")
    (proj / "conf.yaml").symlink_to(proj / "secrets" / "db.yaml")
    result = scan_project(proj, exclude_patterns=("secrets/*.yaml",))
    assert all("INERT_EXCLUDED_CONTENT" not in f.content for f in result.file_fragments)


def test_in_project_symlink_kept_when_project_is_under_build_dir(tmp_path):
    proj = _project(tmp_path / "build")
    (proj / "pkg" / "alias.py").symlink_to(proj / "pkg" / "core.py")
    result = scan_project(proj)
    assert any(f.rel_path == "pkg/alias.py" for f in result.file_fragments)


def test_symlink_loop_does_not_crash_the_scan(tmp_path):
    # Python 3.11/3.12 raise RuntimeError from Path.resolve() on a loop.
    proj = _project(tmp_path)
    (proj / "a.py").symlink_to("b.py")
    (proj / "b.py").symlink_to("a.py")
    result = scan_project(proj)
    assert "a.py" not in result.tree and "b.py" not in result.tree
    assert any(f.rel_path == "pkg/core.py" for f in result.file_fragments)


def test_symlink_guard_refuses_unresolvable_and_escaping_targets(tmp_path):
    from graphlm.scan_guards import _symlink_hides_sensitive

    proj = _project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    (proj / "out.txt").symlink_to(outside)
    (proj / "loop1").symlink_to("loop2")
    (proj / "loop2").symlink_to("loop1")
    assert _symlink_hides_sensitive(proj / "out.txt", (), proj)
    assert _symlink_hides_sensitive(proj / "loop1", (), proj)
    assert not _symlink_hides_sensitive(proj / "pkg" / "core.py", (), proj)
