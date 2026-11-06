"""Structure-only graphs — the map graphlm can draw without any LLM call.

Everything here is deterministic and local: the directory tree from the scan,
tree-sitter import edges, and the import cycles those edges form. It is what
``--dry-run`` has always returned, promoted to a first-class mode
(``generate_graph(structure_only=True)``) for hosts that want an instant, free
map first and LLM enrichment later (or never).
"""

from __future__ import annotations

from collections.abc import Sequence

from graphlm.cycles import detect_cycles
from graphlm.llm import CodebaseGraph
from graphlm.models import ArchitectureNote, ImportEdge


def structure_graph(
    tree: str,
    deterministic_edges: list[ImportEdge] | None,
    sloc_map: dict[str, int],
    *,
    show_cycles: bool = True,
    cycle_threshold: float = 0.0,
    notes: Sequence[str] = (),
) -> CodebaseGraph:
    """Build a graph from the scan tree and AST edges alone.

    ``deterministic_edges`` is ``None`` when AST parsing was off or failed;
    the graph then carries the tree and no edges (never fabricated ones).
    Provenance (``meta``) is stamped by the caller, like every other graph.
    """
    graph = CodebaseGraph(
        directory_tree=tree,
        architecture_notes=[ArchitectureNote(note=n) for n in notes],
        deterministic_edges=deterministic_edges,
    )
    if show_cycles:
        graph.import_cycles = [
            c
            for c in detect_cycles(deterministic_edges or [], sloc_map=sloc_map)
            if c.risk_score >= cycle_threshold
        ]
    return graph
