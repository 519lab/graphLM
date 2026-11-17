"""Graph-vs-graph structural diff (GRAPH_DIFF.*).

Computes what changed in the *map* between graphlm's prior ``GRAPH.json`` and
the new one — modules, edges, cycles, data-flows, entry-points, and
file-summaries **added and removed**. This is deliberately *not* a code diff
(git does that better); it reads graphlm's own prior output, which is why the
``meta`` block was made a versioned input contract (ADR-001). Design settled in
DECISIONS.md **ADR-002**.

Key properties (see ADR-002 decisions, referenced inline):

* **Added/removed only, no "changed" bucket** — identity keys are *structural*,
  so a pure prose rewrite (a module description, a summary) reports no change.
  A "changed" bucket over LLM-authored free text would be dominated by
  regeneration noise (decision 2).
* **Three baseline states, never two** — ``first_run`` (no prior file),
  ``uncomparable`` (prior file exists but is corrupt JSON or an unrecognized
  ``schema_version``), and ``normal`` (both parsed). A corrupt file must not
  masquerade as a first run (decision 4).
* **``deterministic_edges`` None vs []** — ``None`` means AST was off
  (``--no-ast``): "not compared", never "all N removed". ``[]`` means AST ran
  and found none, which *is* compared (decision 5).

This module performs pure local computation over two already-materialized
graphs — no network, no LLM call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from graphlm.md_safe import md_code, md_text
from graphlm.models import (
    GRAPH_META_SCHEMA_VERSION,
    CodebaseGraph,
)

# Bump when the GRAPH_DIFF.json shape changes. Independent of the graph's own
# GRAPH_META_SCHEMA_VERSION — the diff artifact has its own wire format.
# v2: added the `version_changed` state and `old/new_graphlm_version` fields.
DIFF_SCHEMA_VERSION = 2

# Meta schema versions this graphlm knows how to read as a comparable baseline.
# A newer graphlm reading an older *known* version still compares; only an
# unknown/future version routes to `uncomparable` (ADR-002 decision 4).
_KNOWN_META_SCHEMA_VERSIONS = frozenset(range(1, GRAPH_META_SCHEMA_VERSION + 1))


class BaselineState(str, Enum):
    """Which baseline state a diff run is in (ADR-002 decision 4, amended).

    ``VERSION_CHANGED`` was added when fresh-run cleanup landed: the prior map
    was written by a *different* graphlm version and its stale artifacts were
    removed, so the run is regenerated fresh and not compared. It is distinct
    from ``FIRST_RUN`` (which would falsely claim "no prior version existed")
    and ``UNCOMPARABLE`` (which would falsely claim the prior file was corrupt).
    """

    FIRST_RUN = "first_run"
    UNCOMPARABLE = "uncomparable"
    VERSION_CHANGED = "version_changed"
    NORMAL = "normal"


# Human-readable label per state, surfaced in both the .md and .json artifacts so
# an agent never has to infer the state from the shape of the added/removed lists.
_STATE_LABEL = {
    BaselineState.FIRST_RUN: "initial graph — no prior version to compare",
    BaselineState.UNCOMPARABLE: "prior graph could not be read — not compared",
    BaselineState.VERSION_CHANGED: "prior graph from a different graphlm version "
    "— regenerated fresh, not compared",
    BaselineState.NORMAL: "compared against the prior graph",
}


def _state_label(diff: "GraphDiff") -> str:
    """State label, naming the two versions for the VERSION_CHANGED state."""
    if diff.state is BaselineState.VERSION_CHANGED:
        old = diff.old_version or "an unknown version"
        new = diff.new_version or "an unknown version"
        return (
            f"prior graph from graphlm {old} — regenerated fresh under "
            f"{new}, not compared"
        )
    return _STATE_LABEL[diff.state]


@dataclass(frozen=True, slots=True)
class DimensionDiff:
    """Added/removed entities for one diff dimension.

    ``added`` / ``removed`` hold display strings (already formatted for the key
    that identifies the entity). ``compared`` is ``False`` only for
    ``deterministic_edges`` when either side was ``None`` (AST off) — then the
    dimension is reported "not compared" rather than fabricating a mass
    deletion (ADR-002 decision 5).
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    compared: bool = True


@dataclass(frozen=True, slots=True)
class GraphDiff:
    """The computed diff between a prior graph and the new one.

    ``old_graph`` is ``None`` for the first-run and uncomparable states; the
    per-dimension diffs are then all empty. ``old_sha`` / ``new_sha`` are the
    two ``meta.commit_sha`` values (either may be ``None`` for a non-git or
    old-format graph), rendered as a SHA range in the header.
    """

    state: BaselineState
    old_sha: Optional[str]
    new_sha: Optional[str]
    dimensions: dict[str, DimensionDiff]
    # The two graphlm versions, populated only for the VERSION_CHANGED state so
    # the label can name them ("prior map from graphlm X; regenerated under Y").
    old_version: Optional[str] = None
    new_version: Optional[str] = None

    @property
    def has_changes(self) -> bool:
        """True if any compared dimension has an added or removed entity."""
        return any(d.added or d.removed for d in self.dimensions.values())

    @property
    def all_compared(self) -> bool:
        """True if every dimension was actually compared (none skipped).

        A dimension is skipped only when ``deterministic_edges`` was ``None`` on
        one side (AST off on one run) — then that dimension is ``compared=False``.
        The "no structural changes" banner must never be shown unqualified while
        a dimension was skipped, or an agent reads "unchanged" as "checked and
        unchanged" when it was never checked (ADR-002 decisions 4/5 — the
        not-compared state is never silently collapsed into no-change).

        Only ``NORMAL`` diffs actually compare anything; first-run and
        uncomparable diffs have *no* dimensions, and ``all(empty)`` is vacuously
        ``True`` — which would mislead a caller reading the property without
        first checking ``state``. So this is ``False`` off the normal path.
        """
        if self.state is not BaselineState.NORMAL:
            return False
        return all(d.compared for d in self.dimensions.values())


# --- Identity-key formatters (ADR-002 decision 3 — these *are* the contract) ---
# Each returns a stable display string that both identifies the entity for
# set-difference and reads cleanly in the .md. Renames are remove+add: no
# rename-matching heuristic (locked non-goal).


def _fmt_edge(e) -> str:
    # key: (from_path, to_path, kind)
    return f"{md_code(e.from_path)} -> {md_code(e.to_path)} ({md_text(e.kind)})"


def _fmt_module(m) -> str:
    # key: path
    return md_code(m.path)


def _fmt_data_flow(d) -> str:
    # key: (source, destination)
    return f"{md_text(d.source)} -> {md_text(d.destination)}"


def _fmt_entry_point(e) -> str:
    # key: (path, name)
    return f"{md_code(e.path)} :: {md_code(e.name)}"


def _fmt_file_summary(f) -> str:
    # key: path
    return md_code(f.path)


def _fmt_cycle(c) -> str:
    # key: frozenset(nodes) — order-independent (ADR-002 decision 3). The
    # *display* sorts the nodes so a newly-added cycle renders identically
    # regardless of the order the producer listed them (AST cycles are already
    # sorted in cycles.py, but LLM-emitted `import_cycles` need not be) — keeps
    # GRAPH_DIFF.* byte-deterministic.
    return " ↔ ".join(md_code(n) for n in sorted(c.nodes))


def _diff_by_key(old_items, new_items, key_fn, fmt_fn) -> DimensionDiff:
    """Set-difference two entity lists by a structural identity key.

    ``key_fn`` maps an entity to its hashable identity; ``fmt_fn`` maps it to a
    display string. Added = in new not old; removed = in old not new. Output is
    sorted for deterministic artifacts.
    """
    old_map = {key_fn(x): x for x in old_items}
    new_map = {key_fn(x): x for x in new_items}
    old_keys = set(old_map)
    new_keys = set(new_map)
    added = sorted(fmt_fn(new_map[k]) for k in new_keys - old_keys)
    removed = sorted(fmt_fn(old_map[k]) for k in old_keys - new_keys)
    return DimensionDiff(added=added, removed=removed)


def load_baseline(json_path: Path) -> tuple[Optional[CodebaseGraph], BaselineState]:
    """Read and classify the prior ``GRAPH.json`` at ``json_path``.

    Returns ``(parsed_graph_or_None, state)``:

    * missing file → ``(None, FIRST_RUN)``
    * corrupt JSON, invalid model, or unreadable → ``(None, UNCOMPARABLE)``
    * an *unrecognized* ``meta.schema_version`` → ``(None, UNCOMPARABLE)``
    * otherwise → ``(graph, NORMAL)``

    A prior graph with ``meta`` absent (old, pre-stamp format) parses cleanly —
    ``GraphMeta.schema_version`` is not required to be present — and compares as
    ``NORMAL`` with an ``unknown`` old SHA (ADR-002 decision 4/6). Only corrupt
    files and unknown/future versions are uncomparable.
    """
    # FIRST_RUN is *only* a genuinely-absent file. A broken symlink has
    # exists()==False but is_symlink()==True — something is there but unreadable,
    # which is UNCOMPARABLE, not first-run (decision 4 keeps the states distinct).
    if not json_path.exists():
        if json_path.is_symlink():
            return None, BaselineState.UNCOMPARABLE
        return None, BaselineState.FIRST_RUN

    # From here, ANY failure to interpret the file degrades to UNCOMPARABLE and
    # never propagates — this runs on the write path *after* the (paid) LLM call,
    # so an escaping exception would abort the run and discard the new graph. A
    # broad guard is deliberate: the contract is "cannot read → uncomparable",
    # not "cannot read in one of three specific ways". Realistic triggers include
    # invalid/truncated UTF-8 (render_json writes ensure_ascii=False, so a
    # killed/disk-full prior run can leave a mid-codepoint file — UnicodeDecodeError
    # is a ValueError, not an OSError) and a non-hashable schema_version (list/dict)
    # blowing up the set-membership test with TypeError.
    try:
        raw = json_path.read_bytes().decode("utf-8")
        data = json.loads(raw)
    except Exception:
        return None, BaselineState.UNCOMPARABLE

    # Reject an unrecognized meta schema *before* model validation, so a future
    # format we can't safely interpret is flagged rather than half-parsed. The
    # version must be a real int and NOT a bool: `True in {1}` is truthy in
    # Python (bool subclasses int), so `schema_version: true` would otherwise
    # sneak through as version 1. An absent/None version stays NORMAL — that is
    # the old meta-less / pre-stamp backward-compat path.
    try:
        if isinstance(data, dict):
            meta = data.get("meta")
            if isinstance(meta, dict):
                version = meta.get("schema_version")
                if version is not None:
                    if isinstance(version, bool) or not isinstance(version, int):
                        return None, BaselineState.UNCOMPARABLE
                    if version not in _KNOWN_META_SCHEMA_VERSIONS:
                        return None, BaselineState.UNCOMPARABLE
        graph = CodebaseGraph.model_validate(data)
    except Exception:
        return None, BaselineState.UNCOMPARABLE
    return graph, BaselineState.NORMAL


def compute_diff(
    old_graph: Optional[CodebaseGraph],
    new_graph: CodebaseGraph,
    state: BaselineState,
    *,
    old_version: Optional[str] = None,
    new_version: Optional[str] = None,
) -> GraphDiff:
    """Compute the structural diff of ``old_graph`` vs ``new_graph``.

    When ``state`` is not ``NORMAL``, every dimension is empty — the artifact
    still carries the state label so an agent can tell "no changes" from "never
    compared". SHA range is read from each side's ``meta.commit_sha`` regardless
    of state. ``old_version`` / ``new_version`` are recorded only for the
    ``VERSION_CHANGED`` state so its label can name the two graphlm versions.
    """
    old_sha = _sha_of(old_graph)
    new_sha = _sha_of(new_graph)

    dims: dict[str, DimensionDiff] = {}
    if old_graph is None or state is not BaselineState.NORMAL:
        # first-run / uncomparable / version-changed: no comparison, empty dims.
        return GraphDiff(
            state=state,
            old_sha=old_sha,
            new_sha=new_sha,
            dimensions=dims,
            old_version=old_version,
            new_version=new_version,
        )

    dims["modules"] = _diff_by_key(
        old_graph.modules, new_graph.modules, lambda m: m.path, _fmt_module
    )
    dims["import_edges"] = _diff_by_key(
        old_graph.import_edges,
        new_graph.import_edges,
        lambda e: (e.from_path, e.to_path, e.kind),
        _fmt_edge,
    )
    dims["deterministic_edges"] = _diff_deterministic_edges(
        old_graph.deterministic_edges, new_graph.deterministic_edges
    )
    dims["import_cycles"] = _diff_by_key(
        old_graph.import_cycles,
        new_graph.import_cycles,
        lambda c: frozenset(c.nodes),
        _fmt_cycle,
    )
    dims["data_flow"] = _diff_by_key(
        old_graph.data_flow,
        new_graph.data_flow,
        lambda d: (d.source, d.destination),
        _fmt_data_flow,
    )
    dims["entry_points"] = _diff_by_key(
        old_graph.entry_points,
        new_graph.entry_points,
        lambda e: (e.path, e.name),
        _fmt_entry_point,
    )
    dims["file_summaries"] = _diff_by_key(
        old_graph.file_summaries,
        new_graph.file_summaries,
        lambda f: f.path,
        _fmt_file_summary,
    )
    return GraphDiff(state=state, old_sha=old_sha, new_sha=new_sha, dimensions=dims)


def _sha_of(graph: Optional[CodebaseGraph]) -> Optional[str]:
    if graph is None or graph.meta is None:
        return None
    return graph.meta.commit_sha


def _diff_deterministic_edges(old_edges, new_edges) -> DimensionDiff:
    """Diff AST edges, honoring None (AST off) as "not compared" (ADR-002 dec. 5).

    If *either* side is ``None`` the dimension is not compared — toggling
    ``--no-ast`` between runs must not fabricate a mass deletion. Only when both
    sides ran (each is a list, possibly empty) do we set-difference them.
    """
    if old_edges is None or new_edges is None:
        return DimensionDiff(compared=False)
    return _diff_by_key(
        old_edges,
        new_edges,
        lambda e: (e.from_path, e.to_path, e.kind),
        _fmt_edge,
    )


# --- Rendering ---------------------------------------------------------------

# Human-facing dimension titles, in a stable display order.
_DIM_TITLES = [
    ("modules", "Modules"),
    ("import_edges", "Import Edges (LLM)"),
    ("deterministic_edges", "Import Edges (AST / deterministic)"),
    ("import_cycles", "Import Cycles"),
    ("data_flow", "Data Flow"),
    ("entry_points", "Entry Points"),
    ("file_summaries", "File Summaries"),
]


def _sha_range(old_sha: Optional[str], new_sha: Optional[str]) -> str:
    """Render the old→new SHA range for the header.

    A ``None`` side (non-git or old-format graph) renders as ``unknown`` rather
    than being omitted, so the range is always present (ADR-002 decision 6).
    """
    old = f"`{old_sha[:8]}`" if old_sha else "unknown"
    new = f"`{new_sha[:8]}`" if new_sha else "unknown"
    return f"{old} → {new}"


def render_diff_markdown(diff: GraphDiff) -> str:
    """Render a GraphDiff as a Markdown document."""
    lines: list[str] = ["# Codebase Graph Diff\n"]
    lines.append(
        "Structural diff of graphlm's prior graph vs the new one — what changed "
        "in the *map*, not the code. Added/removed only (a pure prose rewrite is "
        "intentionally invisible).\n"
    )
    lines.append(f"- **State:** {_state_label(diff)}")
    lines.append(f"- **Commit range:** {_sha_range(diff.old_sha, diff.new_sha)}")
    lines.append("")

    if diff.state is BaselineState.FIRST_RUN:
        lines.append(
            "No prior graph was found in the output directory, so there is "
            "nothing to compare against. The next run will diff against this one."
        )
        return "\n".join(lines) + "\n"
    if diff.state is BaselineState.UNCOMPARABLE:
        lines.append(
            "A prior graph exists but could not be read (corrupt JSON or an "
            "unrecognized schema version), so no comparison was made. This is "
            "distinct from a first run."
        )
        return "\n".join(lines) + "\n"
    if diff.state is BaselineState.VERSION_CHANGED:
        old = diff.old_version or "an unknown version"
        new = diff.new_version or "an unknown version"
        lines.append(
            f"The prior graph was generated by graphlm {old}; this run is "
            f"{new}. The map was regenerated fresh rather than compared across a "
            "version boundary. The next run will diff against this one."
        )
        return "\n".join(lines) + "\n"

    if not diff.has_changes:
        if diff.all_compared:
            lines.append("**No structural changes** since the prior graph.")
        else:
            # A dimension was skipped (AST off on one run), so we can't claim the
            # map is unchanged — only that the *compared* dimensions are. Never
            # collapse "not compared" into "no change" (ADR-002 dec. 4/5).
            lines.append(
                "**No structural changes in the compared dimensions.** One or more "
                "dimensions were not compared (see below) — the map may still "
                "differ there."
            )
        lines.append("")

    for key, title in _DIM_TITLES:
        dim = diff.dimensions.get(key)
        if dim is None:
            continue
        if not dim.compared:
            lines.append(f"## {title}\n")
            lines.append(
                "_Not compared_ — AST parsing was off (`--no-ast`) on one of the "
                "two runs.\n"
            )
            continue
        if not dim.added and not dim.removed:
            continue
        lines.append(f"## {title}\n")
        if dim.added:
            lines.append(f"### Added ({len(dim.added)})\n")
            for item in dim.added:
                lines.append(f"- {item}")
            lines.append("")
        if dim.removed:
            lines.append(f"### Removed ({len(dim.removed)})\n")
            for item in dim.removed:
                lines.append(f"- {item}")
            lines.append("")

    return "\n".join(lines) + "\n"


def render_diff_json(diff: GraphDiff) -> bytes:
    """Serialize a GraphDiff to JSON bytes.

    Carries its own ``diff_schema_version`` (independent of the graph's), the
    baseline-state label, the SHA range, and per-dimension added/removed lists
    (with ``compared`` for the AST dimension).
    """
    dimensions: dict[str, dict] = {}
    for key, dim in diff.dimensions.items():
        dimensions[key] = {
            "compared": dim.compared,
            "added": dim.added,
            "removed": dim.removed,
        }
    data = {
        "diff_schema_version": DIFF_SCHEMA_VERSION,
        "state": diff.state.value,
        "state_label": _state_label(diff),
        "old_commit_sha": diff.old_sha,
        "new_commit_sha": diff.new_sha,
        "old_graphlm_version": diff.old_version,
        "new_graphlm_version": diff.new_version,
        "dimensions": dimensions,
    }
    return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
