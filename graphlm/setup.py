"""First-run setup wizard: offer the optional packs and install the chosen ones.

``graphlm --setup`` (a flag, not a subcommand — ADR-003), and the automatic
first-run prompt, present graphlm's optional extras — the language grammars, the
``mcp`` server, and the ``typesafe`` prose-scoring SDK — and install the selected
ones with the **same installer that put this binary on PATH** (reusing
``upgrade.detect_installer`` and friends). A completion marker
(``~/.config/graphlm/.setup-done``) stops the auto-prompt from firing again.

The optional extras stay optional: nothing here is a hard dependency, and a
``typesafe`` selection is followed by an instruction to add ``TYPESAFE_API_KEY``
so the (separate) evidence feature can activate. This module never uses a shell —
the installer argv is a fixed list, run through an injectable ``runner`` so the
test suite never touches the network. It also never raises past its own boundary:
a failed or refused install prints the manual command and returns, because a
first-run helper must never block the tool.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from graphlm.upgrade import (
    _abs_no_follow,
    _installer_cwd,
    _spec,
    detect_installer,
    installed_extras,
)

# The optional extras the wizard offers, in presentation order, each with a
# one-line description. ``all`` is deliberately omitted as a selectable row: the
# language rows compose it, and ``installed_extras`` already collapses a full
# language set back to ``all``. Keep these keys in sync with
# pyproject ``[project.optional-dependencies]`` and ``upgrade._EXTRA_MODULES``.
EXTRA_CATALOG: tuple[tuple[str, str], ...] = (
    ("js", "JavaScript / TypeScript import edges (tree-sitter grammars)"),
    ("java", "Java import edges (tree-sitter grammar)"),
    ("rust", "Rust module/use edges (tree-sitter grammar)"),
    ("csharp", "C# using edges (tree-sitter grammar)"),
    ("cpp", "C / C++ #include edges (tree-sitter grammars)"),
    ("go", "Go import edges (tree-sitter grammar)"),
    ("php", "PHP use/require edges (tree-sitter grammar)"),
    ("mcp", "`graphlm --serve` — expose the map to a coding agent over MCP"),
    ("typesafe", "TypeSafe/Jev prose evidence-scoring (needs an API key)"),
)

_TYPESAFE_KEY_HINT = (
    "To enable TypeSafe prose scoring, add your key to "
    "~/.config/graphlm/.env:\n"
    "    TYPESAFE_API_KEY=your-key-here"
)

MARKER_NAME = ".setup-done"


def marker_path(home: Path | None = None) -> Path:
    """Path to the first-run completion marker, in graphlm's config directory.

    Mirrors ``config._user_config_path`` (``$XDG_CONFIG_HOME/graphlm`` else
    ``<home>/.config/graphlm``). ``home`` overrides ``Path.home()`` so tests can
    point it at a temp directory without touching the real ``~``; ``XDG_CONFIG_HOME``
    still wins when set, matching config resolution exactly.
    """
    import os

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (home or Path.home()) / ".config"
    return base / "graphlm" / MARKER_NAME


@dataclass(frozen=True, slots=True)
class SetupPlan:
    """What a setup run will install. ``argv`` empty ⇒ nothing to run (refused
    source checkout, no selection, or a missing installer tool)."""

    installer: str
    selected: tuple[str, ...]
    combined: tuple[str, ...]  # union of already-installed + selected
    argv: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class SetupResult:
    """Outcome of a ``run_setup`` call.

    ``code`` is the process exit code (0 success/nothing-to-do, installer code on
    failure). ``installed`` is True only when packs were actually installed
    successfully in this run — the signal the auto-trigger uses to stop and tell
    the user to re-run, because a mid-run install rebuilds the venv this process
    is executing from (the new packs are not importable in-process).
    """

    code: int
    installed: bool = False


def available_extras(installer: str) -> tuple[tuple[str, str], ...]:
    """Catalog rows whose extra is not already installed."""
    have = set(installed_extras(installer=installer))
    lang = {"js", "java", "rust", "csharp", "cpp", "go", "php"}
    if "all" in have:
        have |= lang
    return tuple((k, d) for k, d in EXTRA_CATALOG if k not in have)


def make_setup_plan(
    selected: tuple[str, ...],
    *,
    installer: str,
    executable: Path,
    which: Callable[[str], str | None],
) -> SetupPlan:
    """Build the installer argv to add ``selected`` extras, keeping existing ones.

    The spec is the *union* of currently-installed extras and the newly-selected
    ones, so no pack the user already has is dropped by the reinstall.
    """
    existing = () if installer == "source" else installed_extras(installer=installer)
    combined = _union_extras(existing, selected)
    spec = _spec(combined)

    if installer == "source":
        return SetupPlan(
            installer,
            selected,
            combined,
            (),
            "This graphlm is a source checkout, not a PyPI install — nothing to "
            "install into. In a dev tree, add extras with `uv sync --extra <name>`.",
        )
    if not selected:
        return SetupPlan(installer, (), combined, (), "No packs selected — nothing to install.")

    if installer == "uv-tool":
        uv = which("uv")
        if uv is None:
            return SetupPlan(
                installer, selected, combined, (),
                "graphlm is a uv-tool install but `uv` is not on PATH. Install "
                f"uv, then run: uv tool install --force {spec}",
            )
        # `uv tool upgrade` cannot add extras; a forced reinstall with the
        # combined spec does (uv retains the union going forward).
        return SetupPlan(
            installer, selected, combined,
            (uv, "tool", "install", "--force", spec),
            f"Installing {spec} via uv tool.",
        )
    if installer == "uv-pip":
        uv = which("uv")
        if uv is None:
            return SetupPlan(
                installer, selected, combined, (),
                "graphlm was installed with uv but `uv` is not on PATH. Install "
                f"uv, then run: uv pip install {spec}",
            )
        return SetupPlan(
            installer, selected, combined,
            (uv, "pip", "install", "--python", str(_abs_no_follow(executable)), spec),
            f"Installing {spec} via uv pip.",
        )
    if installer == "pipx":
        pipx = which("pipx")
        if pipx is None:
            return SetupPlan(
                installer, selected, combined, (),
                "graphlm is a pipx install but `pipx` is not on PATH. Install "
                f"pipx, then run: pipx install --force {spec}",
            )
        return SetupPlan(
            installer, selected, combined,
            (pipx, "install", "--force", spec),
            f"Installing {spec} via pipx.",
        )
    # pip / venv.
    import importlib.util

    if importlib.util.find_spec("pip") is None:
        return SetupPlan(
            installer, selected, combined, (),
            "graphlm's interpreter has no importable pip. A uv-tool install "
            f"adds extras with `uv tool install --force {spec}`.",
        )
    return SetupPlan(
        installer, selected, combined,
        (str(executable), "-P", "-m", "pip", "install", spec),
        f"Installing {spec} via pip.",
    )


def _union_extras(existing: tuple[str, ...], selected: tuple[str, ...]) -> tuple[str, ...]:
    """Union of existing + selected, collapsing a full language set to ``all``.

    ``upgrade._collapse_extras`` only knows the language packs, ``all`` and
    ``mcp`` — it silently drops anything else (e.g. ``typesafe``). So collapse
    the packs it understands, then re-append any extras outside its vocabulary,
    preserving a deterministic order.
    """
    from graphlm.upgrade import _collapse_extras

    lang = {"js", "java", "rust", "csharp", "cpp", "go", "php"}
    known = lang | {"all", "mcp"}
    have = set(existing) | set(selected)
    if "all" in have:
        have |= lang
    collapsed = _collapse_extras(tuple(have & known))
    extra = tuple(sorted(have - known))  # e.g. ("typesafe",)
    return collapsed + extra


def run_setup(
    *,
    echo: Callable[[str], None] | None = None,
    prompt: Callable[[str], str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which: Callable[[str], str | None] | None = None,
    executable: Path | None = None,
    home: Path | None = None,
    interactive: bool = True,
) -> SetupResult:
    """Run the wizard: offer packs, install the chosen ones, write the marker.

    Returns a :class:`SetupResult` (exit code + whether packs were installed).
    Never raises: any failure prints the manual command and returns cleanly.
    Writes the completion marker regardless of install outcome, so the automatic
    first-run prompt does not fire on every subsequent run.
    """
    import shutil

    _echo = echo or (lambda s: print(s, file=sys.stderr))
    _which = which or shutil.which
    exe = Path(executable) if executable is not None else Path(sys.executable)

    try:
        installer = detect_installer(exe)
    except Exception:  # detection must never crash the tool
        installer = "pip"

    rc = 0
    installed = False
    try:
        if installer == "source":
            _echo(
                "graphlm is running from a source checkout — the setup wizard "
                "installs into PyPI installs only. Use `uv sync --extra <name>` "
                "here."
            )
            return SetupResult(0)

        rows = available_extras(installer)
        if not rows:
            _echo("All optional graphlm packs are already installed. Nothing to do.")
            return SetupResult(0)

        selected: tuple[str, ...]
        if not interactive or prompt is None:
            _echo(
                "graphlm has optional packs available (language grammars, MCP "
                "server, TypeSafe scoring). Run `graphlm --setup` to choose and "
                "install them."
            )
            return SetupResult(0)

        selected = _ask_selection(rows, prompt, _echo)
        plan = make_setup_plan(selected, installer=installer, executable=exe, which=_which)
        _echo(plan.message)
        if plan.argv:
            _echo("+ " + " ".join(plan.argv))
            try:
                result = runner(list(plan.argv), cwd=_installer_cwd())
                rc = int(result.returncode)
            except Exception as e:  # installer tool blew up
                _echo(f"Install command failed to run: {e}")
                _echo("Run it yourself: " + " ".join(plan.argv))
                rc = 1
            if rc == 0:
                installed = True
            else:
                _echo("Install did not complete. You can retry with the command above.")
        if "typesafe" in selected:
            _echo(_TYPESAFE_KEY_HINT)
    finally:
        _write_marker(home, _echo)
    return SetupResult(rc, installed)


def _ask_selection(
    rows: tuple[tuple[str, str], ...],
    prompt: Callable[[str], str],
    echo: Callable[[str], None],
) -> tuple[str, ...]:
    """Present the numbered catalog and parse the user's comma/space selection."""
    echo("Optional graphlm packs available:")
    for i, (key, desc) in enumerate(rows, 1):
        echo(f"  {i}. {key:<9} — {desc}")
    echo("Enter numbers to install (e.g. 1,3,8), 'all', or blank to skip.")
    try:
        raw = (prompt("> ") or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # No usable input (piped stdin, or the user aborted) — treat as "skip"
        # rather than letting it crash the tool. The wizard's contract is that
        # it never raises past its boundary.
        echo("")
        return ()
    if not raw:
        return ()
    if raw == "all":
        return tuple(k for k, _ in rows)
    picked: list[str] = []
    for tok in raw.replace(",", " ").split():
        if tok.isdigit():
            idx = int(tok) - 1
            if 0 <= idx < len(rows):
                picked.append(rows[idx][0])
        else:
            # allow naming an extra directly
            for k, _ in rows:
                if tok == k:
                    picked.append(k)
    # Return in catalog order, one entry per key (iterating rows dedupes naturally).
    chosen = set(picked)
    return tuple(k for k, _ in rows if k in chosen)


def _write_marker(home: Path | None, echo: Callable[[str], None]) -> None:
    """Create the completion marker. Refuses to write through a symlink (#33)."""
    try:
        path = marker_path(home)
        if path.is_symlink():
            echo(f"Refusing to write the setup marker through a symlink: {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("graphlm setup complete\n", encoding="utf-8")
    except Exception:
        # A missing marker only means the prompt may re-offer; never fatal.
        pass
