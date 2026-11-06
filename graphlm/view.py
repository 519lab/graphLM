"""View model for codebase graphs — the nodes/links any renderer draws.

One node per file path (or data-flow label), one link per relationship, with
parser-proven (``ast``) structure kept distinct from LLM-inferred (``import``)
edges. ``html_render`` embeds this in GRAPH.html; host applications (an agent
desktop, an IDE panel) call :func:`graph_view` to draw the same picture with
their own UI toolkit instead of re-deriving it from ``CodebaseGraph``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from graphlm.models import CodebaseGraph
from graphlm.testpaths import is_test_path

# Deterministic color palette for directory hashing
PALETTE: list[str] = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
    "#911eb4", "#42d4f4", "#f032e6", "#bfef45", "#fabed4",
    "#469990", "#dcbeff", "#9A6324", "#fffac8", "#800000",
    "#aaffc3", "#808000", "#ffd8b1", "#000075", "#a9a9a9",
]


def directory_color(dir_name: str) -> str:
    """Return a deterministic color from the palette based on a directory name."""
    idx = int(hashlib.md5(dir_name.encode()).hexdigest(), 16) % len(PALETTE)
    return PALETTE[idx]


def norm_path(path: str) -> str:
    """Canonical path: forward slashes, no leading ``./``.

    ``detect_cycles`` now emits normalised cycle nodes (#84); LLM edges often
    still carry ``./``. Comparison and node ids must use the same form or
    ``in_cycle`` is always false and AST/LLM links duplicate (#96).
    """
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


_TYPE_RANK = {
    "entry_point": 3,
    "module": 2,
    "file_summary": 1,
    "file": 0,
    "component": 0,
}


def _upsert_node(
    by_id: dict[str, dict[str, Any]],
    key: str,
    *,
    name: str,
    type: str,
    path: str,
    description: str,
    r: int,
) -> None:
    """Insert or merge a node. One node per id so D3 forceLink can resolve links."""
    existing = by_id.get(key)
    if existing is None:
        by_id[key] = {
            "id": key,
            "name": name,
            "type": type,
            "path": path,
            "description": description,
            "r": r,
            "color": directory_color(path),
        }
        return
    if _TYPE_RANK.get(type, 0) > _TYPE_RANK.get(existing["type"], 0):
        existing["type"] = type
        existing["name"] = name or existing["name"]
    if r > existing["r"]:
        existing["r"] = r
    if description and (
        not existing["description"] or len(description) > len(existing["description"])
    ):
        existing["description"] = description


def build_nodes(graph: CodebaseGraph) -> list[dict[str, Any]]:
    """Build D3 node data from the graph.

    One node per file path (or data-flow label). Import-edge (LLM *and* AST)
    and data-flow endpoints that are not already modules/entry points/summaries
    are added so every link can resolve — D3 forceLink throws on missing node
    ids. Every node carries ``in_cycle``: True when its path is a member of
    any ``import_cycles`` SCC, so the template can ring it red.
    """
    by_id: dict[str, dict[str, Any]] = {}

    for mod in graph.modules:
        key = norm_path(mod.path)
        _upsert_node(
            by_id,
            key,
            name=mod.name,
            type="module",
            path=key,
            description=mod.description,
            r=8,
        )
    for ep in graph.entry_points:
        key = norm_path(ep.path)
        _upsert_node(
            by_id,
            key,
            name=ep.name,
            type="entry_point",
            path=key,
            description=ep.description,
            r=12,
        )
    for fs in graph.file_summaries:
        key = norm_path(fs.path)
        _upsert_node(
            by_id,
            key,
            name=key,
            type="file_summary",
            path=key,
            description=fs.summary,
            r=5,
        )
    for edge in [*graph.import_edges, *(graph.deterministic_edges or [])]:
        for p in (edge.from_path, edge.to_path):
            key = norm_path(p)
            if key not in by_id:
                _upsert_node(
                    by_id, key, name=key, type="file", path=key, description="", r=6
                )
    for flow in graph.data_flow:
        for p in (flow.source, flow.destination):
            key = norm_path(p)
            if key not in by_id:
                _upsert_node(
                    by_id,
                    key,
                    name=key,
                    type="component",
                    path=key,
                    description=flow.description,
                    r=7,
                )

    cycle_paths = {
        norm_path(node) for cycle in graph.import_cycles for node in cycle.nodes
    }
    for node in by_id.values():
        node["in_cycle"] = norm_path(node["path"]) in cycle_paths
        # Test files are marked so the template can render them muted — they are
        # part of the map but usually not what an agent is orienting toward.
        node["is_test"] = is_test_path(node["path"])

    return list(by_id.values())


def build_links(graph: CodebaseGraph) -> list[dict[str, Any]]:
    """Build D3 link data from the graph.

    Three link types, so the picture distinguishes parser-proven structure
    from LLM inference: ``ast`` (``deterministic_edges``, solid accent
    stroke), ``import`` (LLM ``import_edges``, grey), ``data_flow`` (dashed).
    An LLM edge with the same ``(from, to)`` as an AST edge is **dropped** and
    the AST link marked ``corroborated`` — drawing two lines between one pair
    would read as two relationships. Only the LLM-only imports survive as
    ``import`` links.
    """
    links: list[dict[str, Any]] = []

    ast_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in graph.deterministic_edges or []:
        pair = (norm_path(edge.from_path), norm_path(edge.to_path))
        if pair in ast_by_pair:
            continue  # the parser can emit one edge per import statement
        link = {
            "source": pair[0],
            "target": pair[1],
            "type": "ast",
            "stroke": "#7aa2f7",
            "dash": None,
            "corroborated": False,
        }
        ast_by_pair[pair] = link
        links.append(link)

    for edge in graph.import_edges:
        pair = (norm_path(edge.from_path), norm_path(edge.to_path))
        if pair in ast_by_pair:
            ast_by_pair[pair]["corroborated"] = True
            continue
        links.append({
            "source": pair[0],
            "target": pair[1],
            "type": "import",
            "stroke": "#888",
            "dash": None,
        })

    for flow in graph.data_flow:
        links.append({
            "source": norm_path(flow.source),
            "target": norm_path(flow.destination),
            "type": "data_flow",
            "stroke": "#c69",
            "dash": "5,5",
        })

    return links


def graph_view(graph: CodebaseGraph) -> dict[str, Any]:
    """The full drawable view: ``{nodes, links, cycles, provenance}``.

    ``cycles`` is the SCC count (not derivable from per-node ``in_cycle``
    flags: two 3-cycles and one 6-cycle flag the same six nodes).
    ``provenance`` mirrors GRAPH.json's ``meta`` (version / commit / date) so a
    consumer can tell which map it is drawing and whether it is stale.
    """
    provenance = None
    if graph.meta is not None:
        provenance = {
            "graphlm_version": graph.meta.graphlm_version,
            "commit_sha": graph.meta.commit_sha,
            "generated_at": graph.meta.created_at,
        }
    return {
        "nodes": build_nodes(graph),
        "links": build_links(graph),
        "cycles": len(graph.import_cycles),
        "provenance": provenance,
    }
