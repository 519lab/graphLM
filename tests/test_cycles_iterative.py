"""Cycle detection is iterative: same output as the recursive formulation it
replaced, no recursion limit, and a failure never discards a paid graph."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

import graphlm
from graphlm import generate_graph
from graphlm.backend import LLMReply
from graphlm.cycles import detect_cycles, tarjan_scc
from graphlm.models import ImportEdge
from graphlm.parsers.base import detect_import_cycles

FIXTURES = Path(__file__).parent / "fixtures"


def _recursive_tarjan(nodes, adj):
    """The pre-change algorithm, verbatim, as the oracle."""
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index_map: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v):
        index_map[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, set()):
            if w not in index_map:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index_map[w])
        if lowlink[v] == index_map[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    for node in nodes:
        if node not in index_map:
            strongconnect(node)
    return sccs


@pytest.mark.parametrize("seed", range(300))
def test_iterative_matches_recursive_exactly(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 40)
    names = [f"m{i}.py" for i in range(n)]
    adj: dict[str, set[str]] = {}
    for _ in range(rng.randint(0, n * 3)):
        a, b = rng.choice(names), rng.choice(names)
        adj.setdefault(a, set()).add(b)
    nodes = set(names)
    # Same dict/set objects → same iteration order in this process, so the
    # two must agree on components AND their order (diff identity uses it).
    assert tarjan_scc(nodes, adj) == _recursive_tarjan(nodes, adj)


def _chain(n: int, *, close: bool = False) -> list[ImportEdge]:
    edges = [ImportEdge(from_path=f"m{i}.py", to_path=f"m{i + 1}.py", kind="import") for i in range(n)]
    if close:
        edges.append(ImportEdge(from_path=f"m{n}.py", to_path="m0.py", kind="import"))
    return edges


def test_long_chain_does_not_hit_recursion_limit():
    assert detect_cycles(_chain(5000)) == []
    assert detect_import_cycles(_chain(5000)) == []


def test_long_cycle_is_one_component():
    cycles = detect_cycles(_chain(3000, close=True))
    assert len(cycles) == 1 and len(cycles[0].nodes) == 3001
    assert len(detect_import_cycles(_chain(3000, close=True))[0]) == 3001


class _Backend:
    def __init__(self, replies):
        self.replies = list(replies)

    def __call__(self, system_prompt, user_prompt, *, json_schema, schema_name,
                 timeout, max_output_tokens):
        return self.replies.pop(0)


def _graph_with_edges(edges: list[ImportEdge]) -> str:
    return json.dumps({
        "directory_tree": "",
        "import_edges": [{"from_path": e.from_path, "to_path": e.to_path, "kind": "import"} for e in edges],
        "modules": [{"name": "main", "path": "main.py", "description": "entry"}],
        "data_flow": [],
        "database_schema": None,
        "test_organization": [],
        "architecture_notes": [],
        "quick_reference": [],
    })


def test_no_ast_run_survives_a_long_llm_edge_chain():
    # With --no-ast, cycles come from the model's edges; a reply with a long
    # chain used to raise RecursionError after both paid passes.
    backend = _Backend([
        LLMReply('{"requested_files": ["main.py"]}'),
        LLMReply(_graph_with_edges(_chain(3000))),
    ])
    result = generate_graph(FIXTURES / "small_project", llm=backend, ast=False,
                            include_evidence=False, include_importance=False)
    assert len(result.graph.import_edges) == 3000
    assert result.graph.import_cycles == []


def test_cycle_failure_keeps_the_graph_and_says_so(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(graphlm, "detect_cycles", boom)
    backend = _Backend([
        LLMReply('{"requested_files": ["main.py"]}'),
        LLMReply(_graph_with_edges([])),
    ])
    result = generate_graph(FIXTURES / "small_project", llm=backend,
                            include_evidence=False, include_importance=False)
    assert result.graph.modules[0].path == "main.py"
    assert result.graph.import_cycles == []
    assert any("could not compute import cycles" in n.note
               for n in result.graph.architecture_notes)
