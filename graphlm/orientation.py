"""The GRAPH.md orientation block — a compact, token-cheap header summary.

An agent (and the human checking its work) should get the shape of a repo from
the first few lines of the map, not after scrolling past a long directory tree.
This block sits directly under the provenance directive and prints, in a handful
of lines: the entry points, the import-cycle count (split production vs
test-code), and the highest fan-in (blast-radius) files. It is a *summary* of
data rendered in full further down — every sub-line is omitted when empty, and
the whole block is omitted when there is nothing to say, so a minimal graph
renders exactly as before.
"""

from __future__ import annotations

from graphlm.degree import top_fan_in
from graphlm.md_safe import md_code
from graphlm.models import CodebaseGraph

_MAX_ENTRY_POINTS = 6
_MAX_FAN_IN = 5


def render_orientation(
    graph: CodebaseGraph, importance_line: str | None = None
) -> list[str]:
    """Render the orientation block, or ``[]`` when there is nothing to summarize.

    ``importance_line`` is the (optional) ``importance_summary`` clause, passed
    in by the caller so this module doesn't import ``render`` (which imports
    this one).
    """
    items: list[str] = []

    entry_points = sorted(graph.entry_points, key=lambda e: (e.path, e.name))
    if entry_points:
        shown = entry_points[:_MAX_ENTRY_POINTS]
        named = ", ".join(md_code(f"{e.path}::{e.name}") for e in shown)
        extra = len(entry_points) - len(shown)
        suffix = f" (+{extra} more)" if extra > 0 else ""
        items.append(f"- **Entry points:** {named}{suffix}")

    if graph.import_cycles:
        production = sum(1 for c in graph.import_cycles if not c.test_only)
        test_only = sum(1 for c in graph.import_cycles if c.test_only)
        parts = []
        if production:
            parts.append(f"{production} production")
        if test_only:
            parts.append(f"{test_only} in test code")
        items.append(f"- **Import cycles:** {', '.join(parts)}")

    hubs = top_fan_in(graph, limit=_MAX_FAN_IN)
    if hubs:
        named = ", ".join(f"{md_code(p)} ({n})" for p, n in hubs)
        items.append(
            f"- **Most imported (blast radius):** {named}"
        )

    if importance_line:
        # importance_summary returns "most load-bearing: <files>"; bold only the
        # label so the bullet matches the others (- **Label:** value).
        label, sep, value = importance_line.partition(": ")
        if sep:
            items.append(f"- **{label[:1].upper()}{label[1:]}:** {value}")
        else:
            items.append(f"- {importance_line}")

    if not items:
        return []

    return ["## Orientation\n", *items, ""]
