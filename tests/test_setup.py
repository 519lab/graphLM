"""Tests for the first-run setup wizard (no network — installer/runner injected)."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from graphlm import setup as S


class _Runner:
    """Records the argv it is asked to run and returns a chosen exit code."""

    def __init__(self, returncode: int = 0, raises: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.raises = raises

    def __call__(self, argv, *a, **k):  # noqa: ANN001
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return types.SimpleNamespace(returncode=self.returncode)


@pytest.fixture(autouse=True)
def _clear_xdg(monkeypatch):
    """Isolate marker resolution from the runner's environment.

    ``marker_path`` is XDG-aware, so a runner with ``XDG_CONFIG_HOME`` set (as
    GitHub Actions does) resolves the marker away from the injected ``home=`` and
    the ``home``-based tests (notably the symlink-refusal one) fail spuriously.
    Clear it for the whole module; the one test that exercises XDG sets it again
    explicitly after this autouse fixture runs.
    """
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _which_uv(name: str) -> str | None:
    return "/usr/bin/uv" if name == "uv" else ("/usr/bin/pipx" if name == "pipx" else None)


def _force(monkeypatch, *, installer: str, existing: tuple[str, ...] = ()) -> None:
    monkeypatch.setattr(S, "detect_installer", lambda exe: installer)
    monkeypatch.setattr(S, "installed_extras", lambda **k: existing)


# --------------------------------------------------------------------------- #
# _union_extras — the collapse bug that dropped `typesafe`
# --------------------------------------------------------------------------- #
class TestUnionExtras:
    def test_typesafe_preserved_alongside_language(self):
        assert S._union_extras((), ("js", "typesafe")) == ("js", "typesafe")

    def test_full_language_set_collapses_to_all_keeping_typesafe(self):
        got = S._union_extras((), ("js", "java", "rust", "csharp", "cpp", "go", "php", "typesafe"))
        assert got == ("all", "typesafe")

    def test_existing_mcp_is_not_dropped(self):
        assert S._union_extras(("mcp",), ("js",)) == ("mcp", "js")

    def test_dedup(self):
        assert S._union_extras(("js",), ("js", "typesafe")) == ("js", "typesafe")


# --------------------------------------------------------------------------- #
# available_extras — hides already-installed packs
# --------------------------------------------------------------------------- #
class TestAvailableExtras:
    def test_excludes_installed(self, monkeypatch):
        monkeypatch.setattr(S, "installed_extras", lambda **k: ("mcp",))
        keys = [k for k, _ in S.available_extras("pip")]
        assert "mcp" not in keys
        assert "typesafe" in keys and "js" in keys

    def test_all_hides_every_language(self, monkeypatch):
        monkeypatch.setattr(S, "installed_extras", lambda **k: ("all",))
        keys = [k for k, _ in S.available_extras("pip")]
        assert not ({"js", "java", "rust", "csharp", "cpp", "go", "php"} & set(keys))
        assert "typesafe" in keys  # not a language pack


# --------------------------------------------------------------------------- #
# make_setup_plan — per-installer argv
# --------------------------------------------------------------------------- #
class TestMakeSetupPlan:
    def _plan(self, monkeypatch, installer, selected, existing=()):
        _force(monkeypatch, installer=installer, existing=existing)
        return S.make_setup_plan(
            selected, installer=installer, executable=Path("/venv/bin/python"),
            which=_which_uv,
        )

    def test_uv_tool_force_install(self, monkeypatch):
        p = self._plan(monkeypatch, "uv-tool", ("js", "typesafe"))
        assert p.argv == ("/usr/bin/uv", "tool", "install", "--force", "graphlm[js,typesafe]")

    def test_uv_pip_uses_interpreter(self, monkeypatch):
        p = self._plan(monkeypatch, "uv-pip", ("mcp",))
        assert p.argv[:3] == ("/usr/bin/uv", "pip", "install")
        assert "--python" in p.argv and p.argv[-1] == "graphlm[mcp]"

    def test_pipx_force(self, monkeypatch):
        p = self._plan(monkeypatch, "pipx", ("go",))
        assert p.argv == ("/usr/bin/pipx", "install", "--force", "graphlm[go]")

    def test_pip_uses_module(self, monkeypatch):
        # Force pip present, independent of whether THIS (uv) venv has pip (#71).
        import importlib.util as _iu

        monkeypatch.setattr(
            _iu, "find_spec", lambda name: object() if name == "pip" else _iu.find_spec(name)
        )
        p = self._plan(monkeypatch, "pip", ("php",))
        assert p.argv == ("/venv/bin/python", "-P", "-m", "pip", "install", "graphlm[php]")

    def test_pip_absent_refuses(self, monkeypatch):
        # A uv-tool/uv venv often has no importable pip — refuse with a hint
        # rather than emit a `python -m pip` argv that would fail (#71).
        import importlib.util as _iu

        monkeypatch.setattr(
            _iu, "find_spec", lambda name: None if name == "pip" else _iu.find_spec(name)
        )
        p = self._plan(monkeypatch, "pip", ("php",))
        assert p.argv == () and "pip" in p.message

    def test_source_is_refused(self, monkeypatch):
        p = self._plan(monkeypatch, "source", ("js",))
        assert p.argv == ()
        assert "source checkout" in p.message

    def test_empty_selection_no_argv(self, monkeypatch):
        p = self._plan(monkeypatch, "uv-tool", ())
        assert p.argv == ()

    def test_missing_uv_tool_gives_manual_hint(self, monkeypatch):
        _force(monkeypatch, installer="uv-tool")
        p = S.make_setup_plan(
            ("js",), installer="uv-tool", executable=Path("/x/python"),
            which=lambda n: None,
        )
        assert p.argv == () and "uv" in p.message

    def test_existing_extras_carried_into_spec(self, monkeypatch):
        p = self._plan(monkeypatch, "uv-tool", ("typesafe",), existing=("mcp", "js"))
        assert p.argv[-1] == "graphlm[mcp,js,typesafe]"


# --------------------------------------------------------------------------- #
# run_setup — end-to-end with injected prompt/runner/home
# --------------------------------------------------------------------------- #
class TestRunSetup:
    def test_interactive_install_and_key_hint(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="uv-tool")
        runner = _Runner()
        out: list[str] = []
        rc = S.run_setup(
            echo=out.append, prompt=lambda p: "1, 9", runner=runner,
            which=_which_uv, executable=Path("/venv/bin/python"), home=tmp_path,
        )
        assert rc.code == 0
        assert runner.calls == [["/usr/bin/uv", "tool", "install", "--force", "graphlm[js,typesafe]"]]
        assert any("TYPESAFE_API_KEY" in s for s in out)  # key hint printed
        assert S.marker_path(tmp_path).exists()

    def test_no_key_hint_without_typesafe(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="uv-tool")
        out: list[str] = []
        S.run_setup(
            echo=out.append, prompt=lambda p: "1", runner=_Runner(),
            which=_which_uv, executable=Path("/venv/bin/python"), home=tmp_path,
        )
        assert not any("TYPESAFE_API_KEY" in s for s in out)

    def test_blank_selection_installs_nothing(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="uv-tool")
        runner = _Runner()
        S.run_setup(
            echo=lambda s: None, prompt=lambda p: "", runner=runner,
            which=_which_uv, home=tmp_path,
        )
        assert runner.calls == []
        assert S.marker_path(tmp_path).exists()  # still marks setup done

    def test_all_keyword(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="pipx")
        runner = _Runner()
        S.run_setup(
            echo=lambda s: None, prompt=lambda p: "all", runner=runner,
            which=_which_uv, home=tmp_path,
        )
        spec = runner.calls[0][-1]
        assert "all" in spec and "typesafe" in spec

    def test_non_interactive_prints_hint_no_prompt(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="uv-pip")
        out: list[str] = []
        called = {"prompted": False}

        def prompt(_):
            called["prompted"] = True
            return ""

        rc = S.run_setup(
            echo=out.append, prompt=prompt, interactive=False, home=tmp_path,
        )
        assert rc.code == 0
        assert called["prompted"] is False
        assert any("--setup" in s for s in out)
        assert S.marker_path(tmp_path).exists()

    def test_runner_failure_falls_back_cleanly(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="uv-tool")
        runner = _Runner(returncode=1)
        out: list[str] = []
        rc = S.run_setup(
            echo=out.append, prompt=lambda p: "1", runner=runner,
            which=_which_uv, home=tmp_path,
        )
        assert rc.code == 1
        assert any("retry" in s.lower() for s in out)
        assert S.marker_path(tmp_path).exists()

    def test_runner_raising_does_not_propagate(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="uv-tool")
        runner = _Runner(raises=OSError("boom"))
        out: list[str] = []
        rc = S.run_setup(
            echo=out.append, prompt=lambda p: "1", runner=runner,
            which=_which_uv, home=tmp_path,
        )
        assert rc.code == 1  # non-zero, but no exception escaped
        assert any("failed to run" in s.lower() for s in out)

    def test_source_checkout_skips_install(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="source")
        runner = _Runner()
        rc = S.run_setup(
            echo=lambda s: None, prompt=lambda p: "1", runner=runner, home=tmp_path,
        )
        assert rc.code == 0 and runner.calls == []
        assert S.marker_path(tmp_path).exists()

    def test_all_installed_nothing_to_do(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="pip", existing=("mcp", "all", "typesafe"))
        runner = _Runner()
        out: list[str] = []
        rc = S.run_setup(
            echo=out.append, prompt=lambda p: "1", runner=runner, home=tmp_path,
        )
        assert rc.code == 0 and runner.calls == []
        assert any("already installed" in s.lower() for s in out)

    def test_selection_by_name(self, monkeypatch, tmp_path):
        # A user can type the extra name instead of its number.
        _force(monkeypatch, installer="uv-tool")
        runner = _Runner()
        S.run_setup(
            echo=lambda s: None, prompt=lambda p: "typesafe rust", runner=runner,
            which=_which_uv, home=tmp_path,
        )
        spec = runner.calls[0][-1]
        assert "typesafe" in spec and "rust" in spec

    def test_detect_installer_crash_falls_back(self, monkeypatch, tmp_path):
        # If installer detection raises, setup must not crash the tool.
        monkeypatch.setattr(S, "detect_installer", lambda exe: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setattr(S, "installed_extras", lambda **k: ("mcp", "all", "typesafe"))
        rc = S.run_setup(
            echo=lambda s: None, prompt=lambda p: "", runner=_Runner(), home=tmp_path,
        )
        assert rc.code == 0  # fell back to "pip", found all installed, clean exit


class TestMissingInstallerHints:
    def test_uv_pip_without_uv(self, monkeypatch):
        _force(monkeypatch, installer="uv-pip")
        p = S.make_setup_plan(
            ("js",), installer="uv-pip", executable=Path("/x/python"), which=lambda n: None,
        )
        assert p.argv == () and "uv" in p.message

    def test_pipx_without_pipx(self, monkeypatch):
        _force(monkeypatch, installer="pipx")
        p = S.make_setup_plan(
            ("js",), installer="pipx", executable=Path("/x/python"), which=lambda n: None,
        )
        assert p.argv == () and "pipx" in p.message


# --------------------------------------------------------------------------- #
# marker_path — symlink refusal and XDG
# --------------------------------------------------------------------------- #
class TestMarker:
    def test_refuses_symlink(self, monkeypatch, tmp_path):
        _force(monkeypatch, installer="uv-tool")
        cfg = tmp_path / ".config" / "graphlm"
        cfg.mkdir(parents=True)
        target = tmp_path / "evil"
        target.write_text("")
        (cfg / S.MARKER_NAME).symlink_to(target)
        out: list[str] = []
        S.run_setup(
            echo=out.append, prompt=lambda p: "", runner=_Runner(),
            which=_which_uv, home=tmp_path,
        )
        assert any("symlink" in s.lower() for s in out)
        assert target.read_text() == ""  # not clobbered through the link

    def test_xdg_config_home_wins(self, monkeypatch, tmp_path):
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        assert S.marker_path(Path("/some/home")) == xdg / "graphlm" / S.MARKER_NAME

    def test_home_used_without_xdg(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert S.marker_path(tmp_path) == tmp_path / ".config" / "graphlm" / S.MARKER_NAME
