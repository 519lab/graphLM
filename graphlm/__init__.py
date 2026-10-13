"""graphLM — Generate codebase graphs from any project directory.

Usage as a library:

    from graphlm import generate_graph

    result = generate_graph("/path/to/project")
    md_path, json_path, html_path = result.write("./output")

Usage as a CLI:

    graphlm /path/to/project -o ./output
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import logging

from graphlm.config import Settings
from graphlm.context import (
    Pass2Context,
    assemble_pass1_prompt,
    assemble_pass2_prompt,
    filter_requested_files,
)
from graphlm.cycles import compute_sloc_map, detect_cycles
from graphlm.enrich import (
    build_meta,
    is_directory_granular,
    pass_usage,
    score_file_importance,
)
from graphlm.faithfulness import score as score_faithfulness
from graphlm.llm import (
    CodebaseGraph,
    GraphLLError,
    call_llm,
)
from graphlm.models import (
    ArchitectureNote,
    ImportEdge,
    RunUsage,
)
from graphlm.parser import build_dependency_graph
from graphlm.prompts import SYSTEM_PROMPT
from graphlm.render import WriteResult, write_outputs
from graphlm.scanner import ScanResult, scan_project


class GraphResult:
    """Output artifacts from a graph generation run."""

    def __init__(
        self,
        graph: CodebaseGraph,
        pass1_context_tokens: int,
        pass2_context_tokens: int,
        files_analyzed: int,
    ) -> None:
        self.graph = graph
        self.pass1_context_tokens = pass1_context_tokens
        self.pass2_context_tokens = pass2_context_tokens
        self.files_analyzed = files_analyzed

    def write(
        self,
        output_dir: str | Path,
        *,
        include_json: bool = True,
        include_html: bool = True,
        include_diff: bool = True,
    ) -> WriteResult:
        """Write .md, optionally .json/.html, and (by default) the diff to output_dir.

        Returns ``(md, json, html)`` (+ ``.diff_md``/``.diff_json``); json slot is
        ``None`` when ``include_json=False``. Library default is ``True``, CLI off;
        an internal working copy for diff/``--serve`` is kept either way (ADR-016).
        """
        return write_outputs(
            self.graph, Path(output_dir),
            json=include_json, html=include_html, diff=include_diff)


def generate_graph(
    project_dir: str | Path,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    output_dir: str | Path | None = None,
    max_file_chars: int = 4000,
    max_files: int = 200,
    max_pass2_files: int = 80,
    max_context: int | None = None,
    timeout: float | None = None,
    max_output_tokens: int | None = None,
    include_tests: bool = True,
    exclude_patterns: tuple[str, ...] = (),
    use_graphlmignore: bool = True,
    dry_run: bool = False,
    redact_secrets: bool = True,
    ast: bool = True,
    show_cycles: bool = True,
    cycle_threshold: float = 0.0,
    include_html: bool = True,
    include_diff: bool = True,
    include_evidence: bool = True,
    include_importance: bool = True,
    skeleton: bool = True,
) -> GraphResult:
    """Generate a codebase graph for a project directory.

    Two-pass strategy:
    1. Send directory tree to LLM → LLM identifies key files to read
    2. Send tree + key files to LLM → LLM produces the final graph

    Args:
        project_dir: Path to the project directory to analyze.
        base_url: LLM API base URL (falls back to GRAPHLM_BASE_URL env var).
        api_key: LLM API key (falls back to GRAPHLM_API_KEY env var).
        model: Model name (falls back to GRAPHLM_MODEL env var).
        output_dir: Where to write GRAPH.md/json/html. None means do not write
            (the CLI defaults to the scanned project directory).
        max_file_chars: Maximum characters to send per file. A longer file is
            sent as its tree-sitter signature skeleton (see ``skeleton``), or
            as its head when no skeleton is available.
        max_files: Maximum files to scan initially.
        max_pass2_files: Maximum files to include in pass 2 context.
        max_context: Maximum context window in tokens. If None, falls back to
            the GRAPHLM_MAX_CONTEXT env var, then to 120000. An explicit value
            (e.g. from the CLI --max-context flag) takes precedence over both.
        timeout: LLM request timeout in seconds. If None, falls back to the
            GRAPHLM_TIMEOUT env var, then to 300. An explicit value (the CLI
            --timeout flag) takes precedence. Pass 2 is streamed, so a large
            project's generation can legitimately take minutes (#18).
        max_output_tokens: Max tokens the model may emit in either LLM pass —
            the `max_tokens` each request sends. A ceiling, not a reservation:
            it is NOT taken out of the input budget (max_context), because input
            and output ceilings are independent on the target endpoint (#25).
            If None, falls back to GRAPHLM_MAX_OUTPUT_TOKENS env, then
            LLM_MAX_OUTPUT_TOKENS (the model's practical max). Truncation past
            even this raises a clear GraphLLErrorTruncated.
        include_tests: Whether to include test files in the analysis.
        exclude_patterns: Additional glob patterns to exclude.
        use_graphlmignore: If True (default), merge patterns from a
            ``.graphlmignore`` at the project root. Pass False /
            ``--no-graphlmignore`` to skip the file.
        dry_run: If True, return the scan context without calling the LLM.
        redact_secrets: If True, redact secret-like patterns from file content.
        ast: If True (default), run AST-based deterministic import detection,
            attach those edges to the graph, and pass them to the LLM as
            ground truth. Pass False / --no-ast to skip.
        skeleton: If True (default), a file over ``max_file_chars`` is sent as
            its signature skeleton — imports, class/def headers, docstring
            first lines, short constants, bodies elided — so the model sees the
            whole API surface instead of the first few hundred lines (Python
            only today; other languages still send the head). Pass False /
            --no-skeleton to send the head of the file instead.
        include_html: If output_dir is set, whether to also write GRAPH.html.
        include_diff: If output_dir is set, whether to also write the
            graph-vs-graph diff (GRAPH_DIFF.md/json) against the prior
            GRAPH.json in that directory. On by default; see ADR-002. Never
            reached on a --dry-run (the dry-run branch returns before any write).
        include_evidence: Whether to score the LLM's file summaries against the
            source the model saw (TypeSafe/Jev) into meta.evidence_support. On by
            default, but a no-op (None) unless redaction is on, a TYPESAFE_API_KEY
            is set, and the graphlm[typesafe] extra is installed. Never on a dry
            run (no summaries). Pass False / --no-evidence to skip.
        include_importance: Whether to score each module's semantic architectural
            role (TypeSafe/Jev) and fill ModuleDescription.role/.degree. On by
            default, a no-op (both None) unless a TYPESAFE_API_KEY is set and the
            graphlm[typesafe] extra is installed. NOT gated on redaction (it sends
            descriptions + a degree count, never source). Pass False /
            --no-importance to skip.

    Returns:
        GraphResult with the graph and output metadata.

    Raises:
        ValueError: If configuration is invalid.
        GraphLLError: If the LLM call fails.
    """
    project_path = Path(project_dir).resolve()

    if not project_path.exists():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")
    if not project_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {project_dir}")

    # Resolve the context budget: explicit arg > GRAPHLM_MAX_CONTEXT env > 120000.
    # Passing max_context=None (the CLI default when --max-context is unset) lets
    # the env var take effect; an explicit value always wins.
    if max_context is None:
        import os

        max_context = int(os.environ.get("GRAPHLM_MAX_CONTEXT", "120000"))

    # Resolve the output-token ceiling: explicit arg > GRAPHLM_MAX_OUTPUT_TOKENS
    # env > LLM_MAX_OUTPUT_TOKENS default. One ceiling applies to both LLM passes
    # so the documented override can also recover a large pass-1 file-selection
    # response (#73). It remains independent of pass-2 input admission (#25).
    if max_output_tokens is None:
        import os

        from graphlm.limits import LLM_MAX_OUTPUT_TOKENS

        max_output_tokens = int(
            os.environ.get("GRAPHLM_MAX_OUTPUT_TOKENS", str(LLM_MAX_OUTPUT_TOKENS))
        )

    # Resolve configuration (not needed for dry run)
    if dry_run:
        settings = None
    elif base_url or api_key or model:
        if not base_url or not api_key or not model:
            raise ValueError(
                "If any of base_url/api_key/model are provided, "
                "all three must be provided."
            )
        settings = Settings(base_url=base_url, api_key=api_key, model=model)
    else:
        try:
            settings = Settings.from_env()
        except ValueError as e:
            raise ValueError(str(e)) from None

    # Resolve the request timeout independently of how the endpoint was
    # configured: explicit arg > GRAPHLM_TIMEOUT env > 300. Same pattern as
    # max_context / max_output_tokens. Building Settings from an explicit
    # base_url/api_key/model triple used to take the dataclass default 300 and
    # skip the env, so `graphlm -b -k -m` silently ignored GRAPHLM_TIMEOUT (#92).
    if timeout is None:
        import os

        timeout = float(os.environ.get("GRAPHLM_TIMEOUT", "300"))
    resolved_timeout = timeout

    # Phase 1: Scan the project
    scan = scan_project(
        project_path,
        max_file_chars=max_file_chars,
        max_files=max_files,
        include_tests=include_tests,
        exclude_patterns=exclude_patterns,
        use_graphlmignore=use_graphlmignore,
        redact_secrets=redact_secrets,
        skeleton=skeleton,
    )

    # Deterministic import edges from AST parsing (on by default)
    deterministic_edges: list[ImportEdge] | None = None
    partial_languages: set[str] = set()
    if ast:
        try:
            deterministic_edges = build_dependency_graph(
                scan.file_fragments,
                project_dir=project_path,
                max_files=max_files,
                partial_languages=partial_languages,
            )
        except Exception as e:
            logging.warning("AST parsing failed, continuing without it: %s", e)

    sloc_map = compute_sloc_map(scan.file_fragments)

    if dry_run:
        # Don't call the LLM, just show context stats
        # Simulate pass 1 selecting all scanned files
        pass2_files = scan.file_fragments[:max_pass2_files]
        pass2_prompt, pass2_tokens, _truncated = assemble_pass2_prompt(
            scan.tree,
            pass2_files,
            max_context=max_context,
            deterministic_edges=deterministic_edges,
            edges_partial=bool(partial_languages),
        )
        graph = CodebaseGraph(
            directory_tree=scan.tree,
            architecture_notes=[
                ArchitectureNote(
                    note=f"DRY RUN: {len(scan.file_fragments)} files scanned, "
                    f"{len(pass2_files)} files selected for analysis, "
                    f"{pass2_tokens} estimated pass-2 tokens"
                ),
            ],
            deterministic_edges=deterministic_edges,
        )
        if show_cycles:
            graph.import_cycles = [
                c
                for c in detect_cycles(
                    deterministic_edges or [], sloc_map=sloc_map
                )
                if c.risk_score >= cycle_threshold
            ]
        # Stamp the dry-run graph too, so its provenance is consistent with a
        # real run (a --dry-run write would otherwise carry no directive).
        graph.meta = build_meta(project_path)
        return GraphResult(
            graph=graph,
            pass1_context_tokens=pass1_tokens(scan.tree),
            pass2_context_tokens=pass2_tokens,
            files_analyzed=len(pass2_files),
        )

    # Phase 1: LLM identifies key files from tree only
    assert settings is not None

    # Real token accounting per pass, when the endpoint reports it (innovation
    # #6). Captured via call_llm's on_usage callback so the return contract is
    # untouched; each slot stays None if the server sent no usage chunk.
    usage_seen: dict[str, dict[str, object] | None] = {"pass1": None, "pass2": None}

    pass1_prompt = assemble_pass1_prompt(scan.tree)
    pass1_result_json = call_llm(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=pass1_prompt,
        timeout=resolved_timeout,
        max_output_tokens=max_output_tokens,
        on_usage=lambda u: usage_seen.__setitem__("pass1", u),
    )
    pass1_result_json = cast(str, pass1_result_json)

    # Parse pass 1 result
    import json as _json

    try:
        pass1_data = _json.loads(pass1_result_json)
    except (_json.JSONDecodeError, TypeError, KeyError) as e:
        raise GraphLLError(
            f"Pass 1 LLM response was not valid JSON: {e}\n"
            f"Response: {pass1_result_json[:200]}"
        ) from e
    # Pass 1 is free-form JSON. null / a string / a missing object must not
    # TypeError after the paid call (#115).
    if not isinstance(pass1_data, dict):
        requested_files: list[str] = []
    else:
        raw = pass1_data.get("requested_files", [])
        requested_files = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []

    # Phase 2: Filter requested files and assemble context
    pass2_files = filter_requested_files(scan, requested_files, max_pass2_files)
    pass2_prompt, pass2_tokens, _truncated = assemble_pass2_prompt(
        scan.tree,
        pass2_files,
        max_context=max_context,
        deterministic_edges=deterministic_edges,
        edges_partial=bool(partial_languages),
    )

    # Phase 2: LLM produces the final graph
    assert settings is not None
    graph_result = call_llm(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=pass2_prompt,
        response_format=CodebaseGraph,
        timeout=resolved_timeout,
        max_output_tokens=max_output_tokens,
        on_usage=lambda u: usage_seen.__setitem__("pass2", u),
    )
    graph = cast(CodebaseGraph, graph_result)
    # Fill the tree locally rather than making the model echo it back — the echo
    # alone can exceed the output-token ceiling on a large repo (argus's tree is
    # ~20k output tokens), truncating the graph before any module is described
    # (#18). The pass-2 prompt now asks the model for an empty directory_tree.
    graph.directory_tree = scan.tree
    graph.deterministic_edges = deterministic_edges
    if show_cycles:
        cycle_edges = (
            deterministic_edges
            if deterministic_edges is not None
            else graph.import_edges
        )
        graph.import_cycles = [
            c
            for c in detect_cycles(cycle_edges, sloc_map=sloc_map)
            if c.risk_score >= cycle_threshold
        ]
    else:
        graph.import_cycles = []

    # Stamp provenance locally, overwriting anything the model may have emitted
    # for `meta` (like directory_tree, meta is filled here, never trusted from
    # the LLM). The GRAPH.md refresh directive is rendered from this.
    graph.meta = build_meta(project_path)
    # Run telemetry (innovation #6), also local-only. `usage` pairs the
    # server's real counts with graphlm's own estimate for the same prompt so
    # the estimate_tokens heuristic (#17) is auditable from the stamp;
    # `faithfulness` scores the LLM's import_edges against the AST ground
    # truth (None when AST was off — never a fake zero). Neither is set on a
    # dry run: no LLM call, so no usage and no LLM edges to score.
    graph.meta.usage = RunUsage(
        pass1=pass_usage(usage_seen["pass1"], pass1_tokens(scan.tree)),
        pass2=pass_usage(usage_seen["pass2"], pass2_tokens),
    )
    graph.meta.faithfulness = score_faithfulness(
        graph.import_edges, deterministic_edges
    )
    # Evidence support: how well the LLM's file summaries are backed by the
    # source the model saw (TypeSafe/Jev). Local-only, best-effort, never raises
    # (it runs after the paid pass-2 call). Off — None, never a fake zero — when
    # --no-evidence, --no-redact (source must not reach a third party unredacted),
    # no TYPESAFE_API_KEY, or the graphlm[typesafe] extra is not installed. Scored
    # against pass2_files (the fragments the model actually received).
    if include_evidence:
        import os

        from graphlm import evidence as _evidence

        # Defence in depth: evidence.score is contractually non-raising, but this
        # runs *after* the paid pass-2 call, so a belt-and-suspenders guard keeps
        # any future scorer bug from discarding the finished graph (same reason
        # diff.load_baseline never raises). A failure just leaves the field None.
        try:
            graph.meta.evidence_support = _evidence.score(
                graph.file_summaries,
                pass2_files,
                redact_secrets=redact_secrets,
                api_key=os.environ.get("TYPESAFE_API_KEY"),
            )
        except Exception as e:  # never let telemetry cost the paid graph
            logging.warning("Evidence scoring failed, continuing without it: %s", e)

    # Module importance: fill each module's raw `role` (Jev semantic score) and
    # `degree` (structural, from AST edges). Kept as two components — the renderer
    # fuses them for the display order — so the weighting can change without
    # rewriting GRAPH.json. Same best-effort discipline as evidence, but NOT gated
    # on redaction (it sends descriptions + a degree count, never source). Off —
    # both fields None, never fake zero — under --no-importance, no key, or no SDK.
    if include_importance:
        import os

        from graphlm import evidence as _evidence

        try:
            api_key = os.environ.get("TYPESAFE_API_KEY")
            summaries_by_path = {
                _evidence._norm(s.path): s.summary for s in graph.file_summaries
            }
            # On a directory-granular graph (the LLM described `modules` as packages
            # on a large repo) the module-level role/degree are too coarse — a
            # pre-registered eval measured the fused-vs-degree gap jump +0.011 ->
            # +0.105 when scoring the file-level `file_summaries` instead. So score
            # files and stamp meta.file_importance; on a file-granular graph keep the
            # module-level fill exactly as before (INNOVATIONS #6).
            if is_directory_granular(graph):
                graph.meta.file_importance = score_file_importance(
                    graph, deterministic_edges or [], api_key=api_key,
                    summaries_by_path=summaries_by_path, evidence=_evidence,
                )
            else:
                roles = _evidence.score_importance(
                    graph.modules,
                    deterministic_edges or [],
                    api_key=api_key,
                    summaries_by_path=summaries_by_path,
                )
                if roles is not None:
                    degree = _evidence.file_degree(deterministic_edges or [])
                    for mod in graph.modules:
                        p = _evidence._norm(mod.path)
                        if p in roles:
                            mod.role = roles[p]
                            # Resolve degree for a file OR a directory module (#170):
                            # a package module is credited with its members' degree.
                            mod.degree = _evidence.degree_for_module(mod.path, degree)
        except Exception as e:  # never let telemetry cost the paid graph
            logging.warning("Importance scoring failed, continuing without it: %s", e)

    # Write outputs if output_dir specified
    if output_dir is not None:
        write_outputs(graph, Path(output_dir), html=include_html, diff=include_diff)

    return GraphResult(
        graph=graph,
        pass1_context_tokens=pass1_tokens(scan.tree),
        pass2_context_tokens=pass2_tokens,
        files_analyzed=len(pass2_files),
    )


def pass1_tokens(tree: str) -> int:
    """Estimate token count for the pass-1 *request* (user prompt + overhead).

    Must be ``estimate_tokens`` of the prompt actually sent
    (``assemble_pass1_prompt``) plus ``MESSAGE_OVERHEAD_TOKENS`` for the system
    prompt and framing — the same accounting pass 2 uses — so
    ``meta.usage.pass1.estimated_prompt_tokens`` can be compared to the
    server's ``prompt_tokens`` (#86). A two-sentence stub of the instructions
    plus the raw tree under-counted the real prompt by ~7x.
    """
    from graphlm.context import (
        MESSAGE_OVERHEAD_TOKENS,
        assemble_pass1_prompt,
        estimate_tokens,
    )

    return estimate_tokens(assemble_pass1_prompt(tree)) + MESSAGE_OVERHEAD_TOKENS
