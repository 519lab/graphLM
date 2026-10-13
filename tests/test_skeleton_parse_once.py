"""One Tree-sitter parse for a skeletonised Python file and its import edges.

The model still sees the redacted skeleton of the text read. Import edges still
come from the raw file. Those two buffers are the same parse only when they
are the same bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from graphlm.parsers.base import (
    PYTHON,
    _backend,
    build_dependency_graph,
    skeleton_for,
)
from graphlm.parsers.python import skeleton
from graphlm.redact import _redact_secrets
from graphlm.scanner import scan_project

FIXTURES = Path(__file__).parent / "fixtures"
SKELETON_FIXTURE = FIXTURES / "skeleton_project" / "big_module.py"


def _fixture_dirs() -> list[Path]:
    return sorted(path for path in FIXTURES.iterdir() if path.is_dir())


def _legacy_text(
    path: Path, *, max_file_chars: int, skeleton_on: bool, redact: bool
) -> tuple[str, int]:
    """The pre-change read: text, then skeleton-or-head, then redaction."""
    content = path.read_text(encoding="utf-8", errors="replace")
    line_count = content.count("\n") + 1
    if len(content) > max_file_chars:
        skel = skeleton_for(path, content.encode("utf-8")) if skeleton_on else None
        content = skel if skel is not None else content
        if len(content) > max_file_chars:
            content = content[:max_file_chars]
    if redact:
        content = _redact_secrets(content)
    return content, line_count


def _edge_keys(edges) -> list[tuple[str, str, str]]:
    return [(edge.from_path, edge.to_path, edge.kind) for edge in edges]


@pytest.mark.parametrize("fixture", _fixture_dirs(), ids=lambda path: path.name)
@pytest.mark.parametrize("max_file_chars", [4000, 80])
def test_fixtures_match_unshared_skeleton_and_edges(fixture: Path, max_file_chars: int) -> None:
    scan = scan_project(fixture, max_files=500, max_file_chars=max_file_chars)
    root = fixture.resolve()
    for frag in scan.file_fragments:
        expected, line_count = _legacy_text(
            root / frag.rel_path,
            max_file_chars=max_file_chars,
            skeleton_on=True,
            redact=True,
        )
        assert frag.content == expected, frag.rel_path
        assert frag.line_count == line_count
        cached = frag._import_cache
        if cached is not None:
            resolved, digest, carriers = cached
            raw = (root / frag.rel_path).read_bytes()
            assert resolved == str((root / frag.rel_path).resolve())
            assert digest == hashlib.sha256(raw).hexdigest()
            assert isinstance(carriers, list)
            assert all(type(item).__name__ == "_ParsedImport" for item in carriers)

    cap = max(len(scan.file_fragments), 1)
    shared = build_dependency_graph(
        scan.file_fragments, project_dir=root, max_files=cap
    )
    assert all(frag._import_cache is None for frag in scan.file_fragments)
    plain = build_dependency_graph(
        scan.file_fragments, project_dir=root, max_files=cap
    )
    assert _edge_keys(shared) == _edge_keys(plain)


def test_skeleton_tree_argument_matches_direct_parse() -> None:
    code = SKELETON_FIXTURE.read_bytes()
    direct = skeleton(code)
    rendered = skeleton(code, tree=_backend.parse_source(code, PYTHON))
    assert rendered == direct


def _count_parses(monkeypatch) -> dict[tuple[str, bytes], int]:
    counts: dict[tuple[str, bytes], int] = {}
    real = _backend.parse_source

    def wrapped(code: bytes, language: str, suffix: str = ""):
        key = (language, hashlib.sha256(code).digest())
        counts[key] = counts.get(key, 0) + 1
        return real(code, language, suffix)

    monkeypatch.setattr(_backend, "parse_source", wrapped)
    return counts


def _oversized_module() -> str:
    return (
        '"""module"""\n'
        'password = "hunter2"\n'
        "import small\n"
        "def f():\n"
        '    password = "bodysecret"\n'
        "    return 1\n"
        + "x = 1\n" * 700
    )


def test_skeletonized_file_is_parsed_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "big.py").write_text(_oversized_module())
    (tmp_path / "small.py").write_text("x = 1\n")
    expected, line_count = _legacy_text(
        tmp_path / "big.py", max_file_chars=4000, skeleton_on=True, redact=True
    )
    counts = _count_parses(monkeypatch)

    scan = scan_project(tmp_path)
    big = next(frag for frag in scan.file_fragments if frag.rel_path == "big.py")
    assert big.content == expected
    assert big.line_count == line_count
    assert big.content.startswith("# [graphlm skeleton:")
    assert "hunter2" not in big.content
    assert "bodysecret" not in big.content
    assert big._import_cache is not None

    cap = len(scan.file_fragments)
    big_key = ("python", hashlib.sha256((tmp_path / "big.py").read_bytes()).digest())
    small_key = ("python", hashlib.sha256((tmp_path / "small.py").read_bytes()).digest())
    shared = build_dependency_graph(
        scan.file_fragments, project_dir=tmp_path, max_files=cap
    )
    assert big._import_cache is None
    # Scan rendered the skeleton and the graph reused that parse.
    assert counts[big_key] == 1
    assert counts[small_key] == 1
    assert sum(counts.values()) == 2
    plain = build_dependency_graph(
        scan.file_fragments, project_dir=tmp_path, max_files=cap
    )
    assert _edge_keys(shared) == _edge_keys(plain)
    assert any(edge.to_path == "small.py" for edge in shared)


def test_no_skeleton_does_not_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "big.py").write_text(_oversized_module())
    counts = _count_parses(monkeypatch)
    scan = scan_project(tmp_path, skeleton=False)
    big = next(frag for frag in scan.file_fragments if frag.rel_path == "big.py")
    expected, _ = _legacy_text(
        tmp_path / "big.py", max_file_chars=4000, skeleton_on=False, redact=True
    )
    assert big.content == expected
    assert not big.content.startswith("# [graphlm skeleton:")
    assert big._import_cache is None
    build_dependency_graph(
        scan.file_fragments, project_dir=tmp_path, max_files=len(scan.file_fragments)
    )
    key = ("python", hashlib.sha256((tmp_path / "big.py").read_bytes()).digest())
    assert counts[key] == 1


def test_fragment_text_is_not_parsed_as_disk(
    tmp_path: Path,
) -> None:
    """A sliced skeleton is what project_dir=None parses; disk bytes are not."""
    source = "x = 1\n" * 900 + "import tailmod\n"
    (tmp_path / "big.py").write_text(source)
    (tmp_path / "tailmod.py").write_text("z = 1\n")

    scan = scan_project(tmp_path)
    big = next(frag for frag in scan.file_fragments if frag.rel_path == "big.py")
    assert "import tailmod" not in big.content
    assert big._import_cache is not None
    from_fragment = build_dependency_graph(
        scan.file_fragments, project_dir=None, max_files=len(scan.file_fragments)
    )
    assert big._import_cache is None
    assert not any(edge.to_path == "tailmod.py" for edge in from_fragment)

    scan = scan_project(tmp_path)
    from_disk = build_dependency_graph(
        scan.file_fragments, project_dir=tmp_path, max_files=len(scan.file_fragments)
    )
    assert any(
        edge.from_path == "big.py" and edge.to_path == "tailmod.py" for edge in from_disk
    )


def test_byte_mismatch_keeps_text_skeleton_and_disk_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crlf = "import os\r\n" + "x = 1\r\n" * 700
    (tmp_path / "crlf.py").write_bytes(crlf.encode("utf-8"))
    (tmp_path / "bad.py").write_bytes(b"import os\n\xff\n" + b"y = 2\n" * 700)
    expected = {
        name: _legacy_text(tmp_path / name, max_file_chars=4000, skeleton_on=True, redact=True)
        for name in ("crlf.py", "bad.py")
    }
    counts = _count_parses(monkeypatch)

    scan = scan_project(tmp_path)
    for name in ("crlf.py", "bad.py"):
        path = tmp_path / name
        frag = next(item for item in scan.file_fragments if item.rel_path == name)
        text = path.read_text(encoding="utf-8", errors="replace")
        encoded = text.encode("utf-8")
        assert path.read_bytes() != encoded
        content, line_count = expected[name]
        assert frag.content == content
        assert frag.line_count == line_count
        assert frag._import_cache is None
        text_key = ("python", hashlib.sha256(encoded).digest())
        disk_key = ("python", hashlib.sha256(path.read_bytes()).digest())
        assert counts[text_key] == 1
        assert counts.get(disk_key, 0) == 0

    cap = len(scan.file_fragments)
    shared = build_dependency_graph(
        scan.file_fragments, project_dir=tmp_path, max_files=cap
    )
    plain = build_dependency_graph(
        scan.file_fragments, project_dir=tmp_path, max_files=cap
    )
    assert _edge_keys(shared) == _edge_keys(plain)
    for name in ("crlf.py", "bad.py"):
        path = tmp_path / name
        text = path.read_text(encoding="utf-8", errors="replace").encode("utf-8")
        text_key = ("python", hashlib.sha256(text).digest())
        disk_key = ("python", hashlib.sha256(path.read_bytes()).digest())
        assert counts[text_key] == 1
        assert counts[disk_key] == 2
