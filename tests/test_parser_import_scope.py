"""Python imports at every depth, tagged with when they run (scope)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graphlm import generate_graph
from graphlm.cycles import detect_cycles
from graphlm.models import ImportEdge
from graphlm.parsers import detect_import_cycles

FILES = {
    "pkg/__init__.py": "",
    # module-level a -> b; lazy a -> c (inside a function)
    "pkg/a.py": "import pkg.b\n\n\ndef f():\n    from pkg import c\n    return c\n",
    # c -> a only lazily: a <-> c is NOT an import-time cycle
    "pkg/c.py": "def g():\n    from pkg import a\n    return a\n",
    # typing-only b -> a; the else branch (b -> d) runs at import time
    "pkg/b.py": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from pkg import a\nelse:\n    from pkg import d\n"
    ),
    # class body runs at import time: d -> e is module scope
    "pkg/d.py": "class K:\n    from pkg import e\n",
    # top-level try runs at import time; e imports a both lazily and at top
    "pkg/e.py": (
        "def h():\n    import pkg.a\n\n"
        "try:\n    import pkg.a\nexcept ImportError:\n    pass\n"
    ),
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def _edges(project: Path) -> dict[tuple[str, str], str | None]:
    graph = generate_graph(project, structure_only=True).graph
    return {(e.from_path, e.to_path): e.scope for e in graph.deterministic_edges or []}


def test_imports_at_every_depth_are_edges_with_scope(project):
    edges = _edges(project)
    assert edges[("pkg/a.py", "pkg/b.py")] is None           # module level
    assert edges[("pkg/a.py", "pkg/c.py")] == "local"         # inside def f
    assert edges[("pkg/c.py", "pkg/a.py")] == "local"
    assert edges[("pkg/b.py", "pkg/a.py")] == "typing"        # TYPE_CHECKING body
    assert edges[("pkg/b.py", "pkg/d.py")] is None            # its else branch
    assert edges[("pkg/d.py", "pkg/e.py")] is None            # class body
    assert edges[("pkg/e.py", "pkg/a.py")] is None            # top-level try wins dedupe


def test_only_import_time_edges_form_cycles(project):
    graph = generate_graph(project, structure_only=True).graph
    # a -> b -> d -> e -> a is a real import-time cycle; a <-> c (lazy) is not,
    # and b -> a (typing) does not add a second one.
    members = [set(c.nodes) for c in graph.import_cycles]
    assert members == [{"pkg/a.py", "pkg/b.py", "pkg/d.py", "pkg/e.py"}]


def test_lazy_import_alone_never_makes_a_cycle():
    lazy = [ImportEdge(from_path="x.py", to_path="y.py", kind="from", scope="local"),
            ImportEdge(from_path="y.py", to_path="x.py", kind="from")]
    assert detect_cycles(lazy) == []
    assert detect_import_cycles(lazy) == []
    eager = [ImportEdge(from_path="x.py", to_path="y.py", kind="from"),
             ImportEdge(from_path="y.py", to_path="x.py", kind="from")]
    assert len(detect_cycles(eager)) == 1
    assert detect_import_cycles(eager)


def test_scope_is_optional_for_old_maps_and_llm_edges():
    edge = ImportEdge.model_validate({"from_path": "a", "to_path": "b", "kind": "import"})
    assert edge.scope is None
    assert "scope" not in edge.model_dump(exclude_none=True)
