"""Upgrade this graphlm install to the latest PyPI release.

``graphlm --upgrade`` (a flag, not a subcommand — ADR-003) upgrades with the
**same installer that put this binary on PATH**: ``uv tool install`` →
``uv tool upgrade``; ``uv pip install`` → ``uv pip install --upgrade``;
pip → ``python -P -m pip install --upgrade``; pipx → ``pipx upgrade``. A source
checkout is refused: there is no PyPI install to bump.

This module never uses a shell. The installer argv is a fixed list.

The installer never runs in the caller's working directory (see
:func:`_installer_cwd`): ``graphlm`` is usually invoked from inside the repo it
maps, and that repo is untrusted input.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from graphlm.child_env import child_env

# Language extras that ``graphlm[all]`` aggregates. ``mcp`` is separate.
_LANG_EXTRAS = ("js", "java", "rust", "csharp", "cpp", "go", "php")
_EXTRA_MODULES = {
    "mcp": "mcp",
    "js": "tree_sitter_javascript",
    "java": "tree_sitter_java",
    "rust": "tree_sitter_rust",
    "csharp": "tree_sitter_c_sharp",
    "cpp": "tree_sitter_c",
    "go": "tree_sitter_go",
    "php": "tree_sitter_php",
}


@dataclass(frozen=True, slots=True)
class UpgradePlan:
    """What ``--upgrade`` will run. ``argv`` is empty for a source checkout."""

    installer: str  # "uv-tool" | "uv-pip" | "pipx" | "pip" | "source"
    extras: tuple[str, ...]
    argv: tuple[str, ...]
    current_version: str
    message: str


def _posix(path: Path) -> str:
    return path.as_posix()


def _abs_no_follow(path: Path) -> Path:
    """Absolute path with ``.`` / ``..`` collapsed, symlinks intact.

    ``Path.resolve()`` follows ``bin/python`` out of a uv-tool (or pipx) venv
    into the managed CPython, so installer detection must not use it (#71).
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return Path(os.path.normpath(p))


def _uv_tools_root() -> Path:
    env = os.environ.get("UV_TOOL_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "uv" / "tools"
    return Path.home() / ".local" / "share" / "uv" / "tools"


def _pipx_venvs_root() -> Path:
    env = os.environ.get("PIPX_HOME")
    if env:
        return Path(env) / "venvs"
    return Path.home() / ".local" / "pipx" / "venvs"


def _venv_root(executable: Path) -> Path:
    """``…/venv/bin/python`` → ``…/venv``, without following the interpreter symlink."""
    exe = _abs_no_follow(executable)
    if exe.parent.name in {"bin", "Scripts"}:
        return exe.parent.parent
    return exe.parent


def _is_running_interpreter(executable: Path) -> bool:
    """True when ``executable`` is this process's interpreter (resolved or not)."""
    exe = _abs_no_follow(executable)
    running = _abs_no_follow(Path(sys.executable))
    if exe == running:
        return True
    try:
        return Path(executable).resolve() == Path(sys.executable).resolve()
    except OSError:
        return False


def _dist_installer() -> str | None:
    """PEP 376 ``INSTALLER`` of the running ``graphlm`` dist (``uv`` / ``pip`` / …)."""
    try:
        import importlib.metadata as metadata

        raw = metadata.distribution("graphlm").read_text("INSTALLER")
    except Exception:
        return None
    if not raw:
        return None
    name = raw.strip().lower()
    return name or None


def detect_installer(executable: Path) -> str:
    """Classify how this graphlm was installed, so upgrade uses the same tool.

    Identity first (uv-receipt / pipx metadata / dist-info INSTALLER), then
    path layout. Never ``Path.resolve()`` the interpreter — uv-tool venvs
    symlink ``bin/python`` at a managed CPython outside the venv (#71).
    """
    exe = _abs_no_follow(executable)
    posix = _posix(exe)
    roots = [_venv_root(exe)]
    if _is_running_interpreter(exe):
        prefix = _abs_no_follow(Path(sys.prefix))
        if prefix not in roots:
            roots.append(prefix)
    for root in roots:
        if (root / "uv-receipt.toml").is_file():
            return "uv-tool"
        if (root / "pipx_metadata.json").is_file():
            return "pipx"
    try:
        exe.relative_to(_abs_no_follow(_uv_tools_root() / "graphlm"))
        return "uv-tool"
    except ValueError:
        pass
    if "/uv/tools/graphlm/" in posix or posix.endswith("/uv/tools/graphlm"):
        return "uv-tool"
    try:
        exe.relative_to(_abs_no_follow(_pipx_venvs_root() / "graphlm"))
        return "pipx"
    except ValueError:
        pass
    if "/pipx/venvs/graphlm/" in posix:
        return "pipx"
    # Installed into a venv/site-packages vs an editable source tree.
    try:
        import graphlm as pkg
    except ImportError:
        return "pip"
    pkg_path = Path(pkg.__file__).resolve().as_posix()
    if "site-packages" not in pkg_path and "dist-packages" not in pkg_path:
        return "source"
    # ``uv pip install`` writes INSTALLER=uv but no tool receipt. Upgrade
    # with uv, not ``python -m pip`` (uv venvs usually have no pip).
    if _is_running_interpreter(exe) and _dist_installer() == "uv":
        return "uv-pip"
    return "pip"


def _extras_from_uv_receipt(receipt: Path) -> tuple[str, ...] | None:
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    for req in data.get("tool", {}).get("requirements", []):
        if req.get("name") == "graphlm":
            extras = req.get("extras") or []
            return tuple(str(e) for e in extras)
    return None


def _extras_from_pipx_metadata(meta: Path) -> tuple[str, ...] | None:
    import json

    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    main = data.get("main_package") or {}
    extras = main.get("extras")
    if extras:
        return tuple(str(e) for e in extras)
    spec = str(main.get("package_or_url") or "")
    if "[" in spec and "]" in spec:
        inner = spec[spec.index("[") + 1 : spec.index("]")]
        return tuple(p.strip() for p in inner.split(",") if p.strip())
    return None


def _probe_extras() -> tuple[str, ...]:
    found: list[str] = []
    for extra, module in _EXTRA_MODULES.items():
        if importlib.util.find_spec(module) is not None:
            found.append(extra)
    return _collapse_extras(tuple(found))


def _collapse_extras(extras: tuple[str, ...]) -> tuple[str, ...]:
    """Prefer ``all`` over listing every language extra; keep ``mcp`` separate."""
    have = set(extras)
    if "all" in have or have.issuperset(_LANG_EXTRAS):
        have -= set(_LANG_EXTRAS)
        have.add("all")
    order = ("mcp", "all", *_LANG_EXTRAS)
    return tuple(e for e in order if e in have)


def installed_extras(
    *,
    installer: str,
    uv_tools_root: Path | None = None,
    pipx_venvs_root: Path | None = None,
) -> tuple[str, ...]:
    """Extras this install was created with. Receipt first, then import probes."""
    if installer == "uv-tool":
        root = uv_tools_root if uv_tools_root is not None else _uv_tools_root()
        from_receipt = _extras_from_uv_receipt(root / "graphlm" / "uv-receipt.toml")
        if from_receipt is not None:
            return _collapse_extras(from_receipt)
    if installer == "pipx":
        root = pipx_venvs_root if pipx_venvs_root is not None else _pipx_venvs_root()
        from_meta = _extras_from_pipx_metadata(
            root / "graphlm" / "pipx_metadata.json"
        )
        if from_meta is not None:
            return _collapse_extras(from_meta)
    return _probe_extras()


def _spec(extras: tuple[str, ...]) -> str:
    if not extras:
        return "graphlm"
    return "graphlm[" + ",".join(extras) + "]"


def _current_version() -> str:
    from graphlm.provenance import graphlm_version

    return graphlm_version() or "unknown"


def make_plan(
    *,
    executable: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    uv_tools_root: Path | None = None,
    pipx_venvs_root: Path | None = None,
) -> UpgradePlan:
    """Build the installer argv. Never shells out."""
    exe = Path(executable) if executable is not None else Path(sys.executable)
    installer = detect_installer(exe)
    version = _current_version()
    extras = () if installer == "source" else installed_extras(
        installer=installer,
        uv_tools_root=uv_tools_root,
        pipx_venvs_root=pipx_venvs_root,
    )
    spec = _spec(extras)

    if installer == "source":
        return UpgradePlan(
            installer=installer,
            extras=extras,
            argv=(),
            current_version=version,
            message=(
                "This graphlm is a source checkout, not a PyPI install. "
                "Pull and `uv sync`, or install a release with "
                "`uv tool install --upgrade 'graphlm[all]'`."
            ),
        )
    if installer == "uv-tool":
        uv = which("uv")
        if uv is None:
            return UpgradePlan(
                installer=installer,
                extras=extras,
                argv=(),
                current_version=version,
                message=(
                    "graphlm looks like a uv-tool install, but `uv` is not on "
                    f"PATH. Install uv, then run: uv tool upgrade graphlm"
                ),
            )
        # uv retains extras from the tool receipt (docs: "retain the settings
        # provided when installing"). `uv tool upgrade graphlm` is enough.
        return UpgradePlan(
            installer=installer,
            extras=extras,
            argv=(uv, "tool", "upgrade", "graphlm"),
            current_version=version,
            message=f"Upgrading graphlm {version} via uv tool (keeps extras: {spec}).",
        )
    if installer == "uv-pip":
        uv = which("uv")
        if uv is None:
            return UpgradePlan(
                installer=installer,
                extras=extras,
                argv=(),
                current_version=version,
                message=(
                    "graphlm was installed with uv, but `uv` is not on PATH. "
                    "Install uv, then run: uv pip install --upgrade "
                    + spec
                ),
            )
        return UpgradePlan(
            installer=installer,
            extras=extras,
            argv=(
                uv,
                "pip",
                "install",
                "--python",
                str(_abs_no_follow(exe)),
                "--upgrade",
                spec,
            ),
            current_version=version,
            message=f"Upgrading graphlm {version} via uv pip ({spec}).",
        )
    if installer == "pipx":
        pipx = which("pipx")
        if pipx is None:
            return UpgradePlan(
                installer=installer,
                extras=extras,
                argv=(),
                current_version=version,
                message=(
                    "graphlm looks like a pipx install, but `pipx` is not on "
                    "PATH. Install pipx, then run: pipx upgrade graphlm"
                ),
            )
        return UpgradePlan(
            installer=installer,
            extras=extras,
            argv=(pipx, "upgrade", "graphlm"),
            current_version=version,
            message=f"Upgrading graphlm {version} via pipx (keeps extras: {spec}).",
        )
    # pip / venv: extras must be restated on the spec. uv-tool venvs do not
    # ship pip — if detection still lands here without pip, refuse rather
    # than `python -m pip` → "No module named pip" (#71).
    if importlib.util.find_spec("pip") is None:
        return UpgradePlan(
            installer="pip",
            extras=extras,
            argv=(),
            current_version=version,
            message=(
                "This graphlm looks like a pip install, but pip is not "
                "importable in its interpreter. A uv-tool install is "
                "upgraded with `uv tool upgrade graphlm`; pipx with "
                "`pipx upgrade graphlm`."
            ),
        )
    return UpgradePlan(
        installer="pip",
        extras=extras,
        # -P: don't put the cwd on sys.path (defence in depth beside the
        # neutral cwd; requires-python >= 3.11 guarantees the flag exists).
        argv=(str(exe), "-P", "-m", "pip", "install", "--upgrade", spec),
        current_version=version,
        message=f"Upgrading graphlm {version} via pip ({spec}).",
    )


def _installer_cwd() -> Path:
    """Working directory for every installer subprocess: never the caller's cwd.

    The caller is usually standing in the repo graphlm maps — untrusted input —
    and installers read it: ``python -m pip`` (and pipx, which runs it) imports
    a local ``pip.py`` from the cwd, and ``uv pip`` reads ``uv.toml`` /
    ``pyproject.toml`` from the cwd *or any parent* (e.g. a redirected
    ``index-url``). The home directory is user-owned with root-owned parents,
    so nothing found there is attacker-controlled, and the user's own uv
    config still applies. A temp dir is *not* safe: uv's parent walk reaches
    the world-writable system temp dir. Falls back to the filesystem root
    when there is no usable home.
    """
    try:
        home = Path.home()
        if home.is_dir():
            return home
    except (RuntimeError, OSError):  # no HOME / unresolvable user
        pass
    return Path(os.path.abspath(os.sep))


def run_upgrade(
    *,
    executable: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    echo: Callable[[str], None] | None = None,
) -> int:
    """Print the plan and run it. Returns the installer exit code (2 if refused)."""
    _echo = echo or (lambda s: print(s, file=sys.stderr))
    plan = make_plan(executable=executable, which=which)
    _echo(plan.message)
    if not plan.argv:
        return 2
    _echo("+ " + " ".join(plan.argv))
    result = runner(list(plan.argv), cwd=_installer_cwd(), env=child_env())
    return int(result.returncode)
