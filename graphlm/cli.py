"""CLI entry point — Typer-based command-line interface."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    name="graphlm",
    help="Generate codebase graphs (Markdown + JSON) from any project directory.",
    add_completion=False,
)


def _resolve_version() -> str:
    """graphlm's version for ``--version``.

    ``provenance.graphlm_version()`` reads installed package metadata (which
    tracks ``pyproject.toml``) and returns ``None`` in an un-installed source
    checkout. There is no better source in that case — it already wraps the only
    metadata API — so report the checkout state rather than printing ``None``.
    """
    from graphlm.provenance import graphlm_version

    return graphlm_version() or "unknown (source checkout)"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"graphlm {_resolve_version()}")
        raise typer.Exit()


#: Default output subdirectory inside the scanned project (when -o is absent).
GRAPHLM_OUTPUT_DIRNAME = ".graphlm"


def output_destination(project_dir: Path, output_dir: str | None) -> Path:
    """Directory for GRAPH.* files.

    ``-o`` is honored literally (the user named the directory). Otherwise the
    output lands in a ``.graphlm/`` subdirectory of the scanned project — kept
    out of the project root so it doesn't clutter the tree, and excluded from
    scanning so a re-run never ingests its own map.
    """
    if output_dir:
        return Path(output_dir)
    # Resolve so `graphlm /symlink/to/project` writes into the real
    # checkout's `.graphlm/` rather than being refused as an ancestor
    # symlink after the paid LLM calls (#154). `-o` stays literal.
    return Path(project_dir).resolve() / GRAPHLM_OUTPUT_DIRNAME


def _do_install_skill(
    harness: str, project_dir: Path | None, local: bool, force: bool
) -> None:
    """Run --install-skill: install the agent guide and report the outcome."""
    from graphlm.skills import SUPPORTED_HARNESSES, install_skill

    harness = harness.lower()
    if harness not in SUPPORTED_HARNESSES:
        typer.echo(
            f"Error: unknown harness {harness!r}. Supported: "
            f"{', '.join(SUPPORTED_HARNESSES)}.",
            err=True,
        )
        raise typer.Exit(2)
    if local and project_dir is None:
        typer.echo(
            "Error: --skill-local needs a PROJECT_DIR. e.g. "
            f"'graphlm . --install-skill {harness} --skill-local'.",
            err=True,
        )
        raise typer.Exit(2)

    try:
        result = install_skill(
            harness, project_dir=project_dir, local=local, force=force
        )
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)
    if result.skipped:
        typer.echo(
            f"Skipped: {result.path} already exists (use --skill-force to "
            f"overwrite)."
        )
    else:
        typer.echo(f"Installed {harness} graphlm guide → {result.path}")
    if result.note:
        typer.echo("")
        typer.echo(result.note)


def _do_serve(project_dir: Path | None, output_dir: str | None) -> None:
    """Run --serve: expose the generated map to a coding agent over MCP (stdio).

    The map must already exist — serving never triggers a paid LLM run (the
    agent is the scheduler, ADR-001). The existence check runs *before* the
    ``mcp`` import so a missing map reports the actionable problem (run
    ``graphlm .``) rather than a missing extra.
    """
    from graphlm.freshness import STATE_FILENAME
    from graphlm.query import MapUnavailable, load_map

    project = project_dir if project_dir is not None else Path(".")
    # Serve reads graphlm's internal JSON working copy, which is written on every
    # real run regardless of --json — so `--serve` works without the user-facing
    # GRAPH.json deliverable.
    json_path = output_destination(project, output_dir) / STATE_FILENAME
    try:
        load_map(json_path)
    except MapUnavailable as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)
    try:
        from graphlm.mcp_server import run_server
    except ImportError:
        typer.echo(
            "Error: --serve needs the 'mcp' extra. Install with "
            "`uv tool install 'graphlm[mcp]'` (or `pip install 'graphlm[mcp]'`).",
            err=True,
        )
        raise typer.Exit(2)
    run_server(project.resolve(), json_path.resolve())


def _maybe_first_run_setup() -> None:
    """Offer the setup wizard once, on the first interactive analysis run.

    No-op when the completion marker already exists. Interactive (TTY on stdin)
    → run the wizard; non-interactive → the wizard prints a one-line ``--setup``
    hint and returns. Either path writes the marker, so this fires at most once.

    If the wizard actually installed packs, this **exits** (not returns): the
    install rebuilt the venv this process runs from, so the freshly-installed
    packs are not importable in-process — continuing would silently produce a
    graph without the edges the user just asked for. Re-running picks them up.

    Never raises: setup is a convenience, not a gate on the analysis.
    """
    import sys

    try:
        from graphlm.setup import marker_path, run_setup

        if marker_path().exists():
            return
        interactive = sys.stdin.isatty()
        result = run_setup(
            echo=lambda s: typer.echo(s, err=True),
            prompt=input if interactive else None,
            interactive=interactive,
        )
        if result.installed:
            typer.echo(
                "Packs installed. Re-run `graphlm .` to use them (they are not "
                "loaded into the current process).",
                err=True,
            )
            raise typer.Exit(0)
    except typer.Exit:
        raise
    except Exception:
        # A first-run helper must never break the actual analysis.
        pass


@app.command()
def main(
    project_dir: Path | None = typer.Argument(
        None,
        help="Path to the project directory to analyze.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the graphlm version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    upgrade: bool = typer.Option(
        False,
        "--upgrade",
        help="Upgrade this graphlm install to the latest PyPI release and exit. "
        "Detects uv tool / pipx / pip and keeps extras (mcp, language packs). "
        "A source checkout is refused.",
    ),
    install_skill: str | None = typer.Option(
        None,
        "--install-skill",
        help="Install an agent guide teaching a coding harness to use graphlm's "
        "map, then exit. HARNESS is 'claude' or 'codex'. Installs user-global by "
        "default; combine with --skill-local to write into the project.",
        metavar="HARNESS",
    ),
    skill_local: bool = typer.Option(
        False,
        "--skill-local",
        help="With --install-skill: write into the scanned project instead of "
        "the user-global config dir.",
    ),
    skill_force: bool = typer.Option(
        False,
        "--skill-force",
        help="With --install-skill: overwrite an existing guide instead of "
        "skipping it.",
    ),
    serve: bool = typer.Option(
        False,
        "--serve",
        help="Serve the generated map to a coding agent over MCP (stdio) and "
        "exit when the client disconnects. Needs the 'mcp' extra "
        "(graphlm[mcp]) and an existing map (run graphlm first). PROJECT_DIR "
        "defaults to '.'; -o points at the map's directory as usual. Register "
        "with e.g. `claude mcp add graphlm -- graphlm --serve /path/to/repo`.",
    ),
    setup: bool = typer.Option(
        False,
        "--setup",
        help="Run the first-run setup wizard: choose and install optional packs "
        "(language grammars, the MCP server, TypeSafe prose scoring) with the "
        "installer that put graphlm on PATH, then exit. Runs automatically the "
        "first time graphlm is used interactively.",
    ),
    output_dir: str = typer.Option(
        None,
        "-o",
        "--output-dir",
        help="Output directory for GRAPH.md/json/html (default: the scanned project).",
    ),
    base_url: str = typer.Option(
        None,
        "-b",
        "--base-url",
        help="LLM API base URL (or set GRAPHLM_BASE_URL env var).",
    ),
    api_key: str = typer.Option(
        None,
        "-k",
        "--api-key",
        help="LLM API key (or set GRAPHLM_API_KEY env var).",
    ),
    model: str = typer.Option(
        None,
        "-m",
        "--model",
        help="Model name (or set GRAPHLM_MODEL env var).",
    ),
    max_files: int = typer.Option(
        200,
        "--max-files",
        help="Maximum number of files to scan initially.",
    ),
    max_file_chars: int = typer.Option(
        4000,
        "--max-file-chars",
        help="Maximum characters per file.",
    ),
    max_pass2_files: int = typer.Option(
        80,
        "--max-pass2-files",
        help="Maximum files to include in pass 2 context (after LLM selects).",
    ),
    max_context: int | None = typer.Option(
        None,
        "--max-context",
        help="Maximum context window in tokens "
        "(default: GRAPHLM_MAX_CONTEXT env var, else 120000).",
    ),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        help="LLM request timeout in seconds "
        "(default: GRAPHLM_TIMEOUT env var, else 300). Pass 2 is streamed, so "
        "a large project's generation can take minutes.",
    ),
    max_output_tokens: int | None = typer.Option(
        None,
        "--max-output-tokens",
        help="Max tokens the model may emit in each LLM pass "
        "(default: GRAPHLM_MAX_OUTPUT_TOKENS env var, else 128000). A ceiling, "
        "not a reservation — it is NOT taken out of the input budget "
        "(--max-context), since input and output ceilings are independent on "
        "the target endpoint (#25/#26). Lower it only on an endpoint that "
        "bounds prompt+generation together (e.g. vLLM max_model_len).",
    ),
    no_tests: bool = typer.Option(
        False,
        "--no-tests",
        help="Exclude test files from analysis.",
    ),
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        help="Exclude pattern (repeatable). e.g. __pycache__ .git",
    ),
    no_graphlmignore: bool = typer.Option(
        False,
        "--no-graphlmignore",
        help="Do not read .graphlmignore from the project root.",
    ),
    no_redact: bool = typer.Option(
        False,
        "--no-redact",
        help="Do not redact secret-like patterns from file content.",
    ),
    no_skeleton: bool = typer.Option(
        False,
        "--no-skeleton",
        help="Send the head of an oversized file instead of its tree-sitter "
        "signature skeleton.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Analyze and show context stats without calling the LLM.",
    ),
    no_ast: bool = typer.Option(
        False,
        "--no-ast",
        help="Skip Tree-sitter AST parsing for deterministic import edges.",
    ),
    no_html: bool = typer.Option(
        False,
        "--no-html",
        help="Do not generate GRAPH.html visualization output.",
    ),
    no_diff: bool = typer.Option(
        False,
        "--no-diff",
        help="Do not write the GRAPH_DIFF.* graph-vs-graph diff (what changed "
        "in the map since the prior run).",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Also write the machine-readable GRAPH.json (and GRAPH_DIFF.json). "
        "By default graphlm writes GRAPH.md + GRAPH.html — the .md is the "
        "agent-facing map; JSON node/link arrays fill a context window faster. "
        "graphlm keeps an internal working copy either way for --serve/diff.",
    ),
    no_evidence: bool = typer.Option(
        False,
        "--no-evidence",
        help="Do not score the LLM's file summaries against their source with "
        "TypeSafe/Jev (meta.evidence_support). Off anyway without a "
        "TYPESAFE_API_KEY, the graphlm[typesafe] extra, or under --no-redact.",
    ),
    no_importance: bool = typer.Option(
        False,
        "--no-importance",
        help="Do not score module architectural importance with TypeSafe/Jev "
        "(the Importance column in GRAPH.md). Off anyway without a "
        "TYPESAFE_API_KEY or the graphlm[typesafe] extra.",
    ),
    no_show_cycles: bool = typer.Option(
        False,
        "--no-show-cycles",
        help="Do not show import cycle detection results.",
    ),
    cycle_threshold: float = typer.Option(
        0.0,
        "--cycle-threshold",
        help="Only show cycles with risk score >= this value.",
    ),
) -> None:
    """Analyze a project directory and produce a codebase graph (Markdown + JSON).

    Two-pass strategy:
    1. LLM identifies key files from directory tree only
    2. LLM produces the graph from tree + selected files
    """
    from graphlm import generate_graph, GraphLLError
    from graphlm.config import load_user_env

    # The CLI is graphlm's own process, so it reads the user config up front
    # (the library no longer does this at import time — see config.load_user_env).
    load_user_env()

    # --upgrade short-circuits: no LLM, no scan — bump the install and exit.
    # Flag, not a subcommand, so `graphlm <project>` stays the primary interface
    # (ADR-003). project_dir is unused.
    if upgrade:
        from graphlm.upgrade import run_upgrade

        raise typer.Exit(
            run_upgrade(echo=lambda s: typer.echo(s, err=True))
        )

    # --install-skill short-circuits the analysis pipeline: drop the agent guide
    # and exit. It does not need (or use) an LLM, so it runs before any config.
    if install_skill is not None:
        _do_install_skill(install_skill, project_dir, skill_local, skill_force)
        raise typer.Exit(0)

    # --serve likewise short-circuits: no LLM, no scan — just the map over MCP.
    if serve:
        _do_serve(project_dir, output_dir)
        raise typer.Exit(0)

    # --setup short-circuits: run the wizard explicitly and exit. project_dir is
    # unused (the wizard installs into the graphlm install, not a target repo).
    # Honor the TTY: a piped `--setup` prints the hint instead of blocking on input.
    if setup:
        import sys as _sys

        from graphlm.setup import run_setup

        _tty = _sys.stdin.isatty()
        setup_result = run_setup(
            echo=lambda s: typer.echo(s, err=True),
            prompt=input if _tty else None,
            interactive=_tty,
        )
        raise typer.Exit(setup_result.code)

    # project_dir is optional in the signature so --install-skill / --serve can
    # run without it; for the analysis path it's required.
    if project_dir is None:
        typer.echo(
            "Error: missing PROJECT_DIR. Pass a directory to analyze, or use "
            "--install-skill <harness> / --serve / --upgrade / --setup. "
            "See 'graphlm --help'.",
            err=True,
        )
        raise typer.Exit(2)

    # First-run auto-trigger: if setup has never completed, offer the wizard once
    # before the first analysis. Interactive only — a non-TTY (piped/CI) run
    # prints a one-line hint and proceeds, never blocking. The marker is written
    # either way so this fires at most once. A --dry-run is a real analysis and
    # is not a reason to skip: the marker will still be written.
    _maybe_first_run_setup()

    typer.echo(f"Scanning {project_dir}...", err=True)

    try:
        result = generate_graph(
            project_dir=project_dir,
            base_url=base_url,
            api_key=api_key,
            model=model,
            output_dir=None,
            max_file_chars=max_file_chars,
            max_files=max_files,
            max_pass2_files=max_pass2_files,
            max_context=max_context,
            timeout=timeout,
            max_output_tokens=max_output_tokens,
            include_tests=not no_tests,
            exclude_patterns=tuple(exclude),
            use_graphlmignore=not no_graphlmignore,
            dry_run=dry_run,
            redact_secrets=not no_redact,
            skeleton=not no_skeleton,
            ast=not no_ast,
            show_cycles=not no_show_cycles,
            cycle_threshold=cycle_threshold,
            include_html=not no_html,
            include_diff=not no_diff,
            include_evidence=not no_evidence,
            include_importance=not no_importance,
        )
    except ValueError as e:
        typer.echo(f"Configuration error: {e}", err=True)
        raise typer.Exit(1)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except GraphLLError as e:
        typer.echo(f"LLM error: {e}", err=True)
        raise typer.Exit(1)

    if dry_run:
        typer.echo("Dry run complete. No LLM call was made.", err=True)
        typer.echo(
            f"Files selected for pass-2 analysis: {result.files_analyzed}",
            err=True,
        )
        typer.echo(
            f"Pass 1 context: ~{result.pass1_context_tokens} tokens", err=True
        )
        typer.echo(
            f"Pass 2 context: ~{result.pass2_context_tokens} tokens", err=True
        )
        # A dry run makes no LLM call, so `import_edges` (the LLM's field) is
        # always empty — printing it as "0 import edges" misread as "no
        # imports found". The edges a dry run *does* have are the AST's.
        det = result.graph.deterministic_edges
        ast_edges = "AST off" if det is None else f"{len(det)}"
        typer.echo(f"AST import edges: {ast_edges}", err=True)
        typer.echo(
            f"Graph sections: tree, "
            f"{len(result.graph.modules)} modules, "
            f"{len(result.graph.data_flow)} data flows, "
            f"{len(result.graph.file_summaries)} file summaries, "
            f"{len(result.graph.entry_points)} entry points, "
            f"{len(result.graph.architecture_notes)} architecture notes, "
            f"{len(result.graph.quick_reference)} quick references",
            err=True,
        )
        raise typer.Exit(0)

    dest = output_destination(project_dir, output_dir)
    try:
        written = result.write(
            dest,
            include_json=json_out,
            include_html=not no_html,
            include_diff=not no_diff,
        )
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)
    md_path, json_path, html_path = written
    typer.echo(f"Markdown:  {md_path}", err=True)
    if json_path:
        typer.echo(f"JSON:      {json_path}", err=True)
    if html_path:
        typer.echo(f"HTML:      {html_path}", err=True)
    if written.diff_md:
        typer.echo(f"Diff (md): {written.diff_md}", err=True)
    if written.diff_json:
        typer.echo(f"Diff (json): {written.diff_json}", err=True)

    typer.echo(
        f"Modules: {len(result.graph.modules)} | "
        f"Import edges: {len(result.graph.import_edges)} | "
        f"Data flows: {len(result.graph.data_flow)} | "
        f"Tests: {len(result.graph.test_organization)} | "
        f"Files: {len(result.graph.file_summaries)} | "
        f"Entry points: {len(result.graph.entry_points)} | "
        f"Notes: {len(result.graph.architecture_notes)} | "
        f"Lookups: {len(result.graph.quick_reference)}",
        err=True,
    )
    # Run telemetry (innovation #6) — same wording as the GRAPH.md line, one
    # fact per line. Each is omitted when it wasn't measured (no usage from
    # the endpoint / AST off).
    if result.graph.meta is not None:
        from graphlm.render import importance_summary
        from graphlm.telemetry_render import (
            evidence_summary,
            faithfulness_summary,
            usage_summary,
        )

        usage_line = usage_summary(result.graph.meta)
        if usage_line:
            typer.echo(f"Usage: {usage_line}", err=True)
        faith_line = faithfulness_summary(result.graph.meta)
        if faith_line:
            typer.echo(f"Faithfulness: {faith_line}", err=True)
        evidence_line = evidence_summary(result.graph.meta)
        if evidence_line:
            typer.echo(f"Evidence: {evidence_line}", err=True)
        importance_line = importance_summary(result.graph)
        if importance_line:
            typer.echo(f"Importance: {importance_line}", err=True)
    typer.echo("Done.", err=True)


if __name__ == "__main__":
    app()
