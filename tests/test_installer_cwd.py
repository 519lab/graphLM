"""Installers never run in the caller's working directory.

``graphlm --upgrade`` / ``--setup`` are usually invoked from inside the repo
graphlm maps, which is untrusted. ``python -m pip`` (and pipx, which runs it)
imports a ``pip.py`` from the cwd, and ``uv pip`` reads ``uv.toml`` from the
cwd or a parent. These tests run the real interpreter from a directory holding
a hostile ``pip.py`` and assert it never executes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from graphlm import setup as S
from graphlm import upgrade as U


# --------------------------------------------------------------------------- #
# _installer_cwd
# --------------------------------------------------------------------------- #
class TestInstallerCwd:
    def test_is_home_when_home_is_a_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(U.Path, "home", classmethod(lambda cls: tmp_path))
        assert U._installer_cwd() == tmp_path

    def test_falls_back_to_root_when_home_unresolvable(self, monkeypatch):
        def _boom(cls):
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(U.Path, "home", classmethod(_boom))
        root = U._installer_cwd()
        assert root == Path(root.anchor)

    def test_falls_back_to_root_when_home_missing(self, monkeypatch, tmp_path):
        gone = tmp_path / "no-such-home"
        monkeypatch.setattr(U.Path, "home", classmethod(lambda cls: gone))
        root = U._installer_cwd()
        assert root == Path(root.anchor)

    def test_never_the_callers_cwd(self, monkeypatch, tmp_path):
        home, here = tmp_path / "home", tmp_path / "repo"
        home.mkdir()
        here.mkdir()
        monkeypatch.setattr(U.Path, "home", classmethod(lambda cls: home))
        monkeypatch.chdir(here)
        assert U._installer_cwd() != Path.cwd()


# --------------------------------------------------------------------------- #
# Regression: a hostile pip.py in the caller's cwd must not run
# --------------------------------------------------------------------------- #
@pytest.fixture
def hostile_cwd(monkeypatch, tmp_path):
    """chdir into a dir whose ``pip.py`` writes a marker; isolate HOME.

    Returns the marker path. HOME points at an empty dir so the neutral cwd
    the fix chooses is clean and the test never touches the real home.
    """
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    marker = tmp_path / "PIP_PY_EXECUTED"
    (repo / "pip.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(0)\n"
    )
    monkeypatch.setattr(U.Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(repo)
    return marker


def _version_only(argv: list[str]) -> list[str]:
    """Keep the planned argv up to ``pip`` (incl. ``-P``) but ask only ``--version``."""
    return argv[: argv.index("pip") + 1] + ["--version"]


def _real_runner(*, force_cwd: Path | None = None):
    """Run the planned interpreter for real, honouring the kwargs graphlm passes."""

    def run(argv, **kwargs):
        run.calls.append(list(argv))  # type: ignore[attr-defined]
        if force_cwd is not None:
            kwargs["cwd"] = force_cwd
        return subprocess.run(_version_only(list(argv)), capture_output=True, **kwargs)

    run.calls = []  # type: ignore[attr-defined]
    return run


def _assert_ran_pip(runner) -> None:
    """Guard against a vacuous pass: the pip branch was planned and executed."""
    assert len(runner.calls) == 1
    assert runner.calls[0][1:4] == ["-P", "-m", "pip"]


def _pip_present(monkeypatch) -> None:
    """Make the pip branch reachable even in a venv without pip (uv venvs)."""
    import importlib.util as _iu

    orig = _iu.find_spec
    monkeypatch.setattr(
        _iu, "find_spec",
        lambda name, package=None: object() if name == "pip" else orig(name, package),
    )


class TestHostilePipPyDoesNotRun:
    def test_sanity_the_hostile_pip_py_does_run_unprotected(self, hostile_cwd):
        # Proves the fixture is armed: the old argv in the old cwd executes it.
        subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True)
        assert hostile_cwd.exists()

    def test_run_upgrade(self, monkeypatch, hostile_cwd):
        monkeypatch.setattr(U, "detect_installer", lambda exe, **k: "pip")
        monkeypatch.setattr(U, "installed_extras", lambda **k: ())
        _pip_present(monkeypatch)
        runner = _real_runner()
        U.run_upgrade(executable=Path(sys.executable), runner=runner, echo=lambda s: None)
        _assert_ran_pip(runner)
        assert not hostile_cwd.exists()

    def test_run_upgrade_P_flag_alone_protects(self, monkeypatch, hostile_cwd):
        # Defence in depth: even if the cwd were the hostile repo, -P holds.
        monkeypatch.setattr(U, "detect_installer", lambda exe, **k: "pip")
        monkeypatch.setattr(U, "installed_extras", lambda **k: ())
        _pip_present(monkeypatch)
        runner = _real_runner(force_cwd=Path.cwd())
        U.run_upgrade(executable=Path(sys.executable), runner=runner, echo=lambda s: None)
        _assert_ran_pip(runner)
        assert not hostile_cwd.exists()

    def test_run_setup(self, monkeypatch, hostile_cwd, tmp_path):
        monkeypatch.setattr(S, "detect_installer", lambda exe: "pip")
        monkeypatch.setattr(S, "installed_extras", lambda **k: ())
        _pip_present(monkeypatch)
        runner = _real_runner()
        S.run_setup(
            echo=lambda s: None,
            prompt=lambda p: "1",
            runner=runner,
            executable=Path(sys.executable),
            home=tmp_path / "marker-home",
        )
        _assert_ran_pip(runner)
        assert not hostile_cwd.exists()
