"""Markdown rendering of the ``## Import Cycles`` section.

Split out of ``render.py`` (which owns the public ``write_outputs``) as a
cohesive, self-contained render concern — the ``mermaid.py`` precedent. The
section lists *production* cycles first (what an agent should worry about), then
a separately-tagged group for cycles whose every member is a test file
(``Cycle.test_only``) — usually intentional test scaffolding, so surfacing them
indistinguishably from real architecture would mislead a reader.
"""

from __future__ import annotations

from pathlib import Path

from graphlm.md_safe import md_code
from graphlm.models import Cycle

_JS_CYCLE_EXTS = frozenset({".js", ".jsx", ".ts", ".tsx"})


def render_import_cycles(cycles: list[Cycle]) -> list[str]:
    """Render the whole ``## Import Cycles`` section, or ``[]`` when there are none."""
    if not cycles:
        return []

    by_risk = sorted(cycles, key=lambda c: c.risk_score, reverse=True)
    production = [c for c in by_risk if not c.test_only]
    test_only = [c for c in by_risk if c.test_only]

    lines: list[str] = ["## Import Cycles\n"]

    # The JS/TS "often benign" qualifier is about *production* cycles — computing
    # it over test-only cycles too would print a cycle warning above a "no
    # production cycles" banner.
    note = _cycle_language_note(production)
    if note:
        lines.append(note)
        lines.append("")

    if production:
        lines.extend(_render_cycle_group(production, "Cycle", level=3))

    if test_only:
        if not production:
            lines.append(
                "All detected import cycles are among test files "
                "(fixtures/test helpers), dropped under `--no-tests` — usually "
                "intentional scaffolding, not a production concern.\n"
            )
            lines.append("### Test-code cycles\n")
        else:
            lines.append("### Test-code cycles\n")
            lines.append(
                "*Every member is a test file (dropped under `--no-tests`).*\n"
            )
        lines.extend(_render_cycle_group(test_only, "Test cycle", level=4))

    return lines


def _cycle_language_note(cycles: list[Cycle]) -> str | None:
    """One-line qualifier: JS/TS cycles are often benign, Python cycles less so."""
    exts = {Path(node).suffix.lower() for cycle in cycles for node in cycle.nodes}
    js = bool(exts & _JS_CYCLE_EXTS)
    py = ".py" in exts
    if js and py:
        return (
            "> Note: a Python import cycle is usually a design smell; a "
            "JavaScript/TypeScript cycle is often benign (circular "
            "`import`/`require` is common). The risk score still reflects "
            "size × length."
        )
    if js:
        return (
            "> Note: import cycles among JavaScript/TypeScript modules are "
            "often benign (circular `import`/`require` is common); the risk "
            "score still reflects size × length."
        )
    return None


def _render_cycle_group(cycles: list[Cycle], heading: str, level: int) -> list[str]:
    """Render a run of cycles as numbered ``<level> <heading> N`` blocks.

    ``cycles`` is assumed already sorted (highest risk first); numbering
    restarts at 1 per group so production and test-code groups each read
    ``<heading> 1``, ``<heading> 2``, …. ``level`` is the ATX heading depth
    (3 for production under ``## Import Cycles``, 4 for test cycles nested under
    ``### Test-code cycles``).
    """
    hashes = "#" * level
    lines: list[str] = []
    for i, cycle in enumerate(cycles):
        label = (
            f"*{cycle.length} nodes — mutual dependency*"
            if cycle.length == 2
            else f"*{cycle.length} nodes*"
        )
        lines.append(f"{hashes} {heading} {i + 1} (risk score: {cycle.risk_score:.1f})")
        lines.append(label)
        for node in cycle.nodes:
            lines.append(f"- {md_code(node)}")
        lines.append("")
    return lines
