"""Pydantic v2 models for the codebase graph data structure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field


class ImportEdge(BaseModel):
    """A single import/dependency edge between two files."""

    from_path: str = Field(description="Source file path (relative to project root)")
    to_path: str = Field(description="Target file path (relative to project root)")
    kind: str = Field(
        description=(
            "Type of edge: 'import', 'from', 'require', 'static', 'register', "
            "'include', or 'uses'"
        )
    )
    # Set by the parser, never asked of the LLM: None means the import runs at
    # import time (module level); "local" = inside a function (lazy import);
    # "typing" = under `if TYPE_CHECKING:`. Only import-time edges can form an
    # import cycle, so detect_cycles ignores the other two. Optional and None
    # by default, so older maps and LLM-emitted edges validate unchanged.
    scope: Optional[str] = Field(
        default=None,
        description="Leave null. Filled locally by the parser.",
    )


class ModuleDescription(BaseModel):
    """A single module/component in the codebase.

    ``role`` and ``degree`` are the two raw components of module importance,
    filled locally (never LLM-emitted, like ``directory_tree``). ``role`` is the
    TypeSafe/Jev semantic role score (0 = leaf … 3 = orchestrator); ``degree`` is
    the deterministic in+out import degree (parser ground truth). They are kept
    separate — not pre-fused — so a reader can audit each signal and the display
    weighting can change without rewriting ``GRAPH.json``. Both ``None`` when
    importance scoring did not run (TypeSafe off / ``--no-importance``); "not
    scored" must never read as zero.
    """

    path: str = Field(description="File or directory path relative to project root")
    name: str = Field(description="Human-readable module name")
    description: str = Field(
        description="One-line description of what the module does"
    )
    role: Optional[float] = Field(
        default=None,
        description="Jev semantic role score, 0 (leaf) to 3 (orchestrator), or null.",
    )
    degree: Optional[int] = Field(
        default=None,
        description="In+out import degree from AST edges (structural centrality), or null.",
    )


class DataFlowEdge(BaseModel):
    """A single data flow relationship between components."""

    source: str = Field(description="Source component name or path")
    destination: str = Field(description="Destination component name or path")
    description: str = Field(description="What data flows and in what form")


class DBColumn(BaseModel):
    """A single column in a database table."""

    name: str = Field(description="Column name")
    type: str = Field(description="SQL type (e.g. 'TEXT', 'INTEGER', 'BLOB')")
    constraints: Optional[str] = Field(
        default=None, description="Column constraints (PRIMARY KEY, NOT NULL, etc.)"
    )


class DBTable(BaseModel):
    """A single database table."""

    name: str = Field(description="Table name")
    columns: list[DBColumn] = Field(
        default_factory=list, description="List of columns in the table"
    )
    description: str = Field(
        default="", description="One-line description of what the table stores"
    )


class TestMapping(BaseModel):  # noqa: SLF001
    """A single test file and what it covers."""

    file: str = Field(description="Test file path relative to project root")
    covers: str = Field(description="What functionality this test file verifies")


class ArchitectureNote(BaseModel):
    """A single architecture decision or note."""

    note: str = Field(description="The architecture note or decision")


class Symbol(BaseModel):
    """A public symbol (class, function, or constant) defined in a file."""

    name: str = Field(description="Symbol name")
    kind: str = Field(description="Type: 'class', 'function', 'constant', 'variable'")
    description: str = Field(description="What this symbol does or represents (~100 chars)")


class FileSummary(BaseModel):
    """A short summary of a single analyzed source file."""

    path: str = Field(description="File path relative to project root")
    summary: str = Field(
        description="Concise summary of the file's purpose and key contents (~400 chars max)"
    )
    symbols: list[Symbol] = Field(
        default_factory=list,
        description="Public symbols (classes, functions, constants) defined in this file",
    )


class EntryPoint(BaseModel):
    """A known entry point into the application."""

    path: str = Field(description="File path where the entry point is defined")
    name: str = Field(description="Entry point name (function name, route path, CLI command, etc.)")
    kind: str = Field(
        description="Type of entry point: 'main', 'route', 'cli_command', 'hook', 'plugin', 'factory'"
    )
    description: str = Field(description="What this entry point does and when it's invoked")

class QuickReference(BaseModel):
    """A single quick-reference lookup entry."""

    query: str = Field(
        description="What the user is looking for (e.g. 'where is the app factory')"
    )
    location: str = Field(description="Where to find it (file path or section)")


@dataclass(frozen=True, slots=True)
class Cycle:
    """A single import cycle (strongly connected component) with risk score."""

    nodes: list[str]
    edges: list[ImportEdge]
    length: int
    risk_score: float
    # True when *every* node in the cycle is a test file (``is_test_path``).
    # Such cycles are usually intentional test scaffolding, so the renderers
    # group/label them separately from production cycles rather than hiding
    # them. Defaults False so older baselines (and hand-built cycles) validate
    # unchanged — additive-optional, GRAPH_META_SCHEMA_VERSION stays 1.
    test_only: bool = False


# Bump when the persisted metadata shape changes. graphlm reads its own prior
# GRAPH.json (for the fast-follow GRAPH_DIFF), so the stamp is a versioned
# *input* contract, not just output — this integer lets a future format change
# be detected instead of silently misparsed. See DECISIONS.md ADR on the
# self-refreshing stamp.
GRAPH_META_SCHEMA_VERSION = 1


class PassUsage(BaseModel):
    """Token accounting for one LLM pass: what the server billed vs our guess.

    ``prompt_tokens`` / ``completion_tokens`` come from the endpoint's
    ``usage`` object (via ``stream_options.include_usage``) and are ``None``
    when the endpoint sent none. ``estimated_prompt_tokens`` is graphlm's own
    ``estimate_tokens`` figure for the same prompt, so the real-vs-estimated
    ratio — the number that calibrates the ``* 2 // 5`` heuristic (#17) — is
    derivable from the stamp later without re-running anything.
    """

    prompt_tokens: Optional[int] = Field(
        default=None, description="Prompt tokens as counted by the server, or null."
    )
    completion_tokens: Optional[int] = Field(
        default=None, description="Output tokens as counted by the server, or null."
    )
    estimated_prompt_tokens: int = Field(
        description="graphlm's own estimate_tokens() figure for the same prompt."
    )


class RunUsage(BaseModel):
    """Per-pass token usage for one run (both passes optional)."""

    pass1: Optional[PassUsage] = Field(default=None, description="Pass 1 (tree only).")
    pass2: Optional[PassUsage] = Field(default=None, description="Pass 2 (full graph).")


class Faithfulness(BaseModel):
    """How well the LLM's ``import_edges`` agree with the AST ground truth.

    Computed locally by ``graphlm.faithfulness.score`` over ``(from, to)``
    pairs. ``precision`` = matched / LLM edges the parser could have seen;
    ``recall`` = matched / AST edges. Either is ``None`` when its denominator
    is zero. Absent (``meta.faithfulness`` null) when AST was off or on a
    dry run (no LLM edges to score).
    """

    precision: Optional[float] = Field(
        default=None, description="matched / comparable LLM edges, or null if none."
    )
    recall: Optional[float] = Field(
        default=None, description="matched / AST edges, or null if none."
    )
    llm_edges: int = Field(description="LLM import edges the AST could have seen.")
    ast_edges: int = Field(description="AST (deterministic) edges.")
    matched: int = Field(description="Edges present on both sides.")


class FileScore(BaseModel):
    """One file's evidence-support probability (a low outlier worth surfacing)."""

    path: str = Field(description="File path relative to project root.")
    score: float = Field(
        description="Probability (0-1) that the file's summary is supported by the "
        "source the model saw. Low = the claim outruns its evidence."
    )


class EvidenceSupport(BaseModel):
    """How well the LLM's prose is supported by the source the model actually saw.

    Computed locally by ``graphlm.evidence.score`` — a TypeSafe/Jev Noul per
    ``file_summary``, asking whether the summary's role + named symbols match the
    *source that was sent* (the redacted/skeletonised/truncated ``frag.content``,
    not the file on disk). Like ``Faithfulness``, this is a downstream-trust
    **weight**, not a hallucination accusation: a skeletonised or truncated
    fragment lowers the score **by design**, because the model was asked to
    describe a file it only partially saw.

    ``None`` (``meta.evidence_support`` absent) when scoring did not run — TypeSafe
    off (no key / SDK not installed / ``--no-evidence``), redaction disabled
    (``--no-redact`` — the source must not reach a third party unredacted), on a
    dry run, or when no summary had a pass-2 fragment to score against. "Not
    scored" must never read as "scored zero".

    ``scored`` counts summaries actually sent to Jev; ``skipped`` counts summaries
    not scored because there was no evidence to compare against — the file was not
    in the pass-2 set (written from the tree alone), or its source is too thin to
    verify a claim (a near-empty ``__init__`` or bare entry point defines nothing,
    so the support judgment has no signal and would score a correct summary ~0.2).
    """

    mean: Optional[float] = Field(
        default=None,
        description="Mean support probability over scored summaries, or null if none.",
    )
    scored: int = Field(description="Summaries scored (had a pass-2 fragment).")
    skipped: int = Field(
        description="Summaries skipped — no pass-2 fragment, or source too thin "
        "to verify a claim against (near-empty file). Not scored."
    )
    low: list[FileScore] = Field(
        default_factory=list,
        description="Summaries below the low-support threshold, worst first.",
    )


class FileImportance(BaseModel):
    """One file's importance, at file granularity.

    Filled only when the LLM described ``modules`` at *directory* granularity (large
    repos), where the module-level ``role``/``degree`` are too coarse. The importance
    is then scored over ``file_summaries`` (always file-level) and stamped here in
    ``meta`` instead of onto ``modules`` — ``modules`` is a diff-compared field, and
    at file granularity there is no graph object that carries importance without a new
    diff key. ``role`` is the Jev semantic score (0-3), ``degree`` the per-file in+out
    import degree, ``fused`` the renderer's blend (0-1). See ADR-015 / INNOVATIONS #6.
    """

    path: str = Field(description="File path relative to project root.")
    role: float = Field(description="Jev semantic role score, 0 (leaf) to 3 (orchestrator).")
    degree: int = Field(description="In+out import degree (structural centrality).")
    fused: float = Field(description="Fused importance (0-1), the renderer's blend.")


class GraphMeta(BaseModel):
    """Provenance stamp: when the graph was generated and against which commit.

    Filled locally by ``generate_graph`` after pass 2 (never emitted by the
    LLM, like ``directory_tree`` and ``deterministic_edges``). The
    ``GRAPH.md`` refresh directive is *rendered from* this, so the two cannot
    drift. ``commit_sha`` is ``None`` for a non-git project — that is a normal
    state, not an error, and is preserved (not dropped) in ``GRAPH.json`` so a
    reader can tell "no git tracking" from "old format, field absent".
    """

    schema_version: int = Field(
        default=GRAPH_META_SCHEMA_VERSION,
        description="Metadata format version (for reading prior graphs).",
    )
    created_at: str = Field(
        description="ISO 8601 UTC timestamp when the graph was generated. "
        "Human context only — never the staleness trigger."
    )
    commit_sha: Optional[str] = Field(
        default=None,
        description="Git HEAD commit SHA the graph was generated against, or "
        "null if the project is not a git repo (or has no commits). Staleness "
        "= this differs from the repo's current HEAD.",
    )
    graphlm_version: Optional[str] = Field(
        default=None, description="graphlm package version that generated the graph."
    )
    # Run telemetry (innovation #6). Both are ADDITIVE and optional: an older
    # GRAPH.json without them still validates (defaults to None) and the
    # meaning of the existing fields is unchanged, so GRAPH_META_SCHEMA_VERSION
    # stays at 1 — ADR-001 bumps it only when the *meaning* changes.
    usage: Optional[RunUsage] = Field(
        default=None,
        description="Real vs estimated token usage per pass. Null on a dry run "
        "or when the endpoint reported no usage.",
    )
    faithfulness: Optional[Faithfulness] = Field(
        default=None,
        description="LLM import_edges vs AST deterministic_edges agreement. "
        "Null when AST was off or on a dry run.",
    )
    evidence_support: Optional[EvidenceSupport] = Field(
        default=None,
        description="How well the LLM's file summaries are supported by the source "
        "the model saw (TypeSafe/Jev). Null when TypeSafe is off, --no-redact, on a "
        "dry run, or no summary had a pass-2 fragment.",
    )
    file_importance: Optional[list[FileImportance]] = Field(
        default=None,
        description="File-level importance (Jev role x degree), most load-bearing "
        "first. Filled only on directory-granular graphs, where the module-level "
        "role/degree are too coarse; null otherwise (module importance is on the "
        "modules themselves). Additive optional — GRAPH_META_SCHEMA_VERSION stays 1.",
    )


class CodebaseGraph(BaseModel):
    """The complete codebase graph as produced by the LLM."""

    directory_tree: str = Field(
        description="Annotated directory tree of the project"
    )
    import_edges: list[ImportEdge] = Field(
        default_factory=list, description="Import/dependency edges between files"
    )
    modules: list[ModuleDescription] = Field(
        default_factory=list, description="Modules and what they do"
    )
    data_flow: list[DataFlowEdge] = Field(
        default_factory=list, description="Data flow between components"
    )
    database_schema: Optional[list[DBTable]] = Field(
        default=None, description="Database tables and columns, or null if no DB"
    )
    test_organization: list[TestMapping] = Field(
        default_factory=list, description="Test file to coverage mapping"
    )
    architecture_notes: list[ArchitectureNote] = Field(
        default_factory=list, description="Architecture decisions and notes"
    )
    file_summaries: list[FileSummary] = Field(
        default_factory=list, description="Short summaries (~400 chars) of analyzed source files"
    )
    entry_points: list[EntryPoint] = Field(
        default_factory=list, description="Known application entry points (main, routes, CLI commands, etc.)"
    )
    quick_reference: list[QuickReference] = Field(
        default_factory=list, description="Quick-reference lookup table"
    )
    deterministic_edges: list[ImportEdge] | None = Field(
        default=None,
        description="Import edges derived from AST parsing (deterministic, not LLM-generated)",
    )
    import_cycles: list[Cycle] = Field(
        default_factory=list,
        description="Import cycles (strongly connected components) with risk scores",
    )
    meta: Optional[GraphMeta] = Field(
        default=None,
        description="Provenance stamp (generation time, commit SHA, version). "
        "Filled locally, not by the LLM. Null on graphs built without it "
        "(older format or library callers) — the renderer omits the refresh "
        "directive in that case.",
    )
