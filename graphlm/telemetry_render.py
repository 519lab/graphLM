"""Render the run-telemetry stamp (`meta.usage` / `.faithfulness` / `.evidence_support`).

Split out of ``render.py`` as a cohesive concern — the ``mermaid.py`` /
``cycles_render.py`` / ``orientation.py`` precedent. These four functions turn
the ``GraphMeta`` telemetry fields into the one ``> **Run telemetry.**`` line
under the refresh directive, and the CLI reuses the per-metric summaries for its
own ``Usage:`` / ``Faithfulness:`` / ``Evidence:`` lines so the wording is
single-sourced. (Module *importance* stays in ``render.py`` — its
``importance_summary`` is tied to the modules-table fusion, not to ``meta``.)
"""

from __future__ import annotations

from graphlm.models import GraphMeta
from graphlm.md_safe import md_code


def usage_summary(meta: GraphMeta) -> str | None:
    """One terse clause on pass-2 token usage, or None when there is nothing to say.

    Reports the server's real prompt count beside graphlm's own estimate so a
    reader can see how far the ``estimate_tokens`` heuristic (#17) is off on
    this endpoint, plus the output size. Pass 1 is omitted from the prose (it
    is a tree-only prompt and rarely interesting); it stays in ``GRAPH.json``.
    Shared by ``GRAPH.md`` and the CLI so the wording cannot drift.
    """
    if meta.usage is None or meta.usage.pass2 is None:
        return None
    p2 = meta.usage.pass2
    prompt = (
        f"{p2.prompt_tokens} tokens"
        if p2.prompt_tokens is not None
        else "not reported by endpoint"
    )
    text = f"pass 2 prompt: {prompt} (graphlm estimated {p2.estimated_prompt_tokens})"
    if p2.completion_tokens is not None:
        text += f"; output: {p2.completion_tokens} tokens"
    return text


def faithfulness_summary(meta: GraphMeta) -> str | None:
    """One terse clause on LLM-vs-AST edge agreement, or None when not scored.

    ``n/a`` marks a ratio with no denominator (no comparable LLM edges, or no
    AST edges) — distinct from a real 0.00, which means the sides disagree.
    Shared by ``GRAPH.md`` and the CLI.
    """
    f = meta.faithfulness
    if f is None:
        return None

    def _ratio(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    return (
        "LLM import edges vs parser ground truth: "
        f"precision {_ratio(f.precision)}, recall {_ratio(f.recall)} "
        f"(n={f.llm_edges} LLM / {f.ast_edges} AST, {f.matched} matched)"
    )


def evidence_summary(meta: GraphMeta) -> str | None:
    """One terse clause on how well the LLM's summaries are backed by their source.

    ``None`` when evidence scoring did not run (TypeSafe off, ``--no-redact``, a
    dry run, or nothing to score). A low mean, or named low outliers, means the
    prose claims more than the source the model saw supports — a trust weight,
    not a hallucination verdict (a skeletonised fragment lowers it by design).
    Shared by ``GRAPH.md`` and the CLI so the wording cannot drift.
    """
    e = meta.evidence_support
    if e is None:
        return None
    mean = "n/a" if e.mean is None else f"{e.mean:.2f}"
    text = (
        f"summary evidence support: mean {mean} "
        f"(n={e.scored} scored, {e.skipped} skipped)"
    )
    if e.low:
        worst = ", ".join(f"{md_code(fs.path)} {fs.score:.2f}" for fs in e.low[:3])
        text += f"; weakest: {worst}"
    return text


def render_telemetry(meta: GraphMeta) -> str | None:
    """Render the run-telemetry blockquote line under the directive, or None.

    Each part is optional (a dry run has none; ``--no-ast`` has no faithfulness;
    TypeSafe off has no evidence support; an endpoint may report no usage) —
    whichever are present are shown, and the line is omitted entirely when none
    is. Terse on purpose: this is read by agents deciding how much to trust the
    LLM's edge table and prose.
    """
    parts = [
        p
        for p in (
            usage_summary(meta),
            faithfulness_summary(meta),
            evidence_summary(meta),
        )
        if p is not None
    ]
    if not parts:
        return None
    return "> **Run telemetry.** " + ". ".join(parts) + "."
