"""Mermaid module graph for ``GRAPH.md`` — a directory-level picture of the import edges.

Why a Mermaid block at all: GitHub (and most Markdown viewers) render Mermaid
fences natively, so the map gets a *picture* with no CDN, no build step, and no
separate file — unlike ``GRAPH.html``, which loads a pinned D3 build from a CDN and is blank
offline. Why directory-level: a file-level graph of a real project is hundreds
of nodes and unreadable in a Markdown page; collapsing each file to its parent
directory gives a package-dependency view that fits on one screen, and the
``max_nodes`` cap bounds it further for very large trees.

Ground truth wins: when the parser's ``deterministic_edges`` are present they
are the edge source (and the note says so); the LLM's ``import_edges`` are only
the fallback for ``--no-ast`` runs, and are labelled as inferred.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable

from graphlm.md_safe import md_fence, no_controls
from graphlm.models import CodebaseGraph, ImportEdge

# Red used for cycle members — the same hue as the ``in_cycle`` ring in
# ``GRAPH.html`` so the two pictures agree.
CYCLE_COLOR = "#e11"
# Muted amber for directories whose only cycle is test-only (usually intentional
# test scaffolding) — distinct from the alarming red of a production cycle.
TEST_CYCLE_COLOR = "#b8860b"

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_]")


def _norm_path(path: str) -> str:
    """Canonical path: forward slashes, no leading ``./``.

    LLM edges often carry ``./a.py``; ``rpartition("/")`` then yields ``.``
    and every root-level cycle collapses to one bogus node (#96).
    """
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _collapse_dir(path: str) -> str:
    """Map a file path to its directory-level node label.

    ``graphlm/parsers/python.py`` -> ``graphlm/parsers``; a root-level file
    (``setup.py``) has no directory and stays its own node — collapsing every
    root file into a single ``.`` node would hide the one place (top-level
    scripts) where per-file structure still matters.
    """
    path = _norm_path(path)
    head, sep, _tail = path.rpartition("/")
    return head if sep else path


def _mermaid_label(label: str) -> str:
    """Escape a label for use inside a quoted Mermaid node label.

    ``"`` would end the quoted label (Mermaid's ``#quot;`` entity restores
    it), and a control character could end the line — and with it the
    diagram — so it is shown as a visible escape instead. Labels are
    repository paths (or model-written ones), so both are reachable.
    """
    # A label starting with a backtick switches Mermaid into "markdown
    # string" mode; #96; is the backtick entity.
    return no_controls(label).replace('"', "#quot;").replace("`", "#96;")


def _assign_ids(labels: list[str]) -> dict[str, str]:
    """Derive a safe, unique Mermaid id for every label (in the given order).

    Mermaid ids may only contain ``[A-Za-z0-9_]``, so every other character
    becomes ``_`` and the id is prefixed ``n_`` (an id may not start with a
    digit, and ``n_`` also keeps a directory named e.g. ``end`` — a Mermaid
    keyword — from breaking the diagram). The sanitizing is lossy (``a-b`` and
    ``a_b`` collide), so a numeric suffix disambiguates a repeat; ``labels``
    must be sorted by the caller so the suffixes are stable across runs.
    """
    ids: dict[str, str] = {}
    taken: set[str] = set()
    for label in labels:
        base = "n_" + _UNSAFE_ID_CHARS.sub("_", label)
        candidate = base
        n = 2
        while candidate in taken:
            candidate = f"{base}_{n}"
            n += 1
        ids[label] = candidate
        taken.add(candidate)
    return ids


def _cycle_edge_test(graph: CodebaseGraph) -> Callable[[ImportEdge], bool]:
    """Return ``is_cycle(edge)``: True when both endpoints share an import cycle.

    Membership is decided at the *file* level, before collapsing — two files in
    the same SCC are cycle members regardless of which directories they land
    in. Cycle sets are frozensets of the SCC's node paths.
    """
    cycle_sets = [
        frozenset(_norm_path(n) for n in c.nodes) for c in graph.import_cycles
    ]

    def is_cycle(edge: ImportEdge) -> bool:
        src, dst = _norm_path(edge.from_path), _norm_path(edge.to_path)
        return any(src in members and dst in members for members in cycle_sets)

    return is_cycle


def render_mermaid(graph: CodebaseGraph, *, max_nodes: int = 40) -> list[str]:
    """Render the ``## Module Graph`` section lines, or ``[]`` when there are no edges.

    The block is a ``flowchart LR`` over directory-level nodes. Cycle edges are
    emitted *last* so their ``linkStyle`` indices are simply the tail of the
    link list — Mermaid indexes links by emission order and has no per-link
    id, so styling by index is the only option, and grouping the styled links
    at the end keeps the index arithmetic trivial and testable. Directories
    that contain a cycle member get a red outline: an intra-directory cycle
    (the common case — files of one package importing each other) collapses
    to a self-edge, which is dropped as noise, so without the node outline the
    picture would show no trace of it.

    Output is fully sorted (nodes by label, edges by ``(from, to)`` within the
    plain/cycle groups) so a regenerated ``GRAPH.md`` diffs cleanly.
    """
    if graph.deterministic_edges:
        edges = graph.deterministic_edges
        source_note = "*parser-extracted import edges (ground truth)*"
    elif graph.import_edges:
        edges = graph.import_edges
        source_note = "*LLM-inferred import edges*"
    else:
        return []

    is_cycle = _cycle_edge_test(graph)
    cycle_dirs = {
        _collapse_dir(node) for cycle in graph.import_cycles for node in cycle.nodes
    }
    # Directories whose only cycle involvement is a test-only cycle get a muted
    # outline instead of the alarming red — the cycle is usually intentional test
    # scaffolding. A dir touched by *any* production cycle stays red.
    prod_cycle_dirs = {
        _collapse_dir(node)
        for cycle in graph.import_cycles
        if not cycle.test_only
        for node in cycle.nodes
    }
    test_cycle_dirs = {
        _collapse_dir(node)
        for cycle in graph.import_cycles
        if cycle.test_only
        for node in cycle.nodes
    } - prod_cycle_dirs

    # Collapse to directory level; a collapsed edge is a cycle edge if ANY of
    # the file-level edges behind it was one. Self-edges are dropped, but
    # their directory is still a node: a package whose files only import each
    # other (a single-package project is the extreme case) would otherwise
    # vanish from the picture entirely — along with its cycle outline.
    collapsed: dict[tuple[str, str], bool] = {}
    degree: dict[str, int] = defaultdict(int)
    for edge in edges:
        key = (_collapse_dir(edge.from_path), _collapse_dir(edge.to_path))
        degree[key[0]] += 0
        degree[key[1]] += 0
        if key[0] == key[1]:
            continue
        collapsed[key] = collapsed.get(key, False) or is_cycle(edge)
    for src, dst in collapsed:
        degree[src] += 1
        degree[dst] += 1

    # Highest degree first; cycle members win ties (the red outline is the
    # last thing the cap should cut); label makes the cut deterministic.
    ranked = sorted(degree, key=lambda d: (-degree[d], d not in cycle_dirs, d))
    hidden = len(ranked) - max_nodes if len(ranked) > max_nodes else 0
    kept = set(ranked[:max_nodes])
    nodes = sorted(kept)
    ids = _assign_ids(nodes)

    plain = sorted(
        k for k, cyc in collapsed.items()
        if not cyc and k[0] in kept and k[1] in kept
    )
    cyclic = [
        k for k, cyc in collapsed.items()
        if cyc and k[0] in kept and k[1] in kept
    ]
    # A collapsed cycle edge is "test-only" when neither endpoint dir touches a
    # production cycle — same rule as the node outline, so edges and nodes agree.
    prod_cyclic = sorted(
        k for k in cyclic
        if k[0] in prod_cycle_dirs or k[1] in prod_cycle_dirs
    )
    test_cyclic = sorted(
        k for k in cyclic
        if k[0] not in prod_cycle_dirs and k[1] not in prod_cycle_dirs
    )
    cycle_nodes = sorted(kept & cycle_dirs)

    lines: list[str] = ["## Module Graph\n"]
    lines.append(
        f"{source_note} — directory-level view; files are collapsed to their "
        "parent directory.\n"
    )
    fence_at = len(lines)  # fence chosen once the body is known
    lines.append("flowchart LR")
    for label in nodes:
        lines.append(f'    {ids[label]}["{_mermaid_label(label)}"]')
    for src, dst in plain + prod_cyclic + test_cyclic:
        lines.append(f"    {ids[src]} --> {ids[dst]}")
    # linkStyle indices are emission order; the two cyclic groups are contiguous
    # tails, so each gets one range.
    if prod_cyclic:
        first = len(plain)
        indices = ",".join(str(i) for i in range(first, first + len(prod_cyclic)))
        lines.append(f"    linkStyle {indices} stroke:{CYCLE_COLOR},stroke-width:2px")
    if test_cyclic:
        first = len(plain) + len(prod_cyclic)
        indices = ",".join(str(i) for i in range(first, first + len(test_cyclic)))
        lines.append(
            f"    linkStyle {indices} stroke:{TEST_CYCLE_COLOR},stroke-width:2px"
        )
    for label in cycle_nodes:
        color = TEST_CYCLE_COLOR if label in test_cycle_dirs else CYCLE_COLOR
        lines.append(f"    style {ids[label]} stroke:{color},stroke-width:2px")
    open_fence, close_fence = md_fence("\n".join(lines[fence_at:]), "mermaid")
    lines.insert(fence_at, open_fence)
    lines.append(close_fence + "\n")

    if prod_cyclic:
        lines.append("Red edges are members of a production import cycle.")
    if test_cyclic:
        lines.append("Amber edges are members of a test-code import cycle.")
    prod_cycle_nodes = [n for n in cycle_nodes if n not in test_cycle_dirs]
    test_only_nodes = [n for n in cycle_nodes if n in test_cycle_dirs]
    if prod_cycle_nodes:
        lines.append("Red-outlined directories contain a file in an import cycle.")
    if test_only_nodes:
        lines.append(
            "Amber-outlined directories contain only test-code import cycles "
            "(usually intentional scaffolding)."
        )
    if hidden:
        lines.append(f"*… {hidden} more directories not shown*")
    if cyclic or cycle_nodes or hidden:
        lines.append("")
    return lines
