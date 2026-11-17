"""Tests for ``graphlm --upgrade`` (no network — the installer is injected)."""

from __future__ import annotations

import subprocess

from graphlm.upgrade import (
    _collapse_extras,
    _installer_cwd,
    _spec,
    detect_installer,
    installed_extras,
    make_plan,
    run_upgrade,
)


class TestCollapseExtras:
    def test_all_language_extras_collapse(self):
        assert _collapse_extras(
            ("js", "java", "rust", "csharp", "cpp", "go", "php", "mcp")
        ) == ("mcp", "all")

    def test_partial_stays_listed(self):
        assert _collapse_extras(("js", "mcp")) == ("mcp", "js")

    def test_spec_empty(self):
        assert _spec(()) == "graphlm"

    def test_spec_joined(self):
        assert _spec(("mcp", "all")) == "graphlm[mcp,all]"


class TestDetectInstaller:
    def test_uv_tool_path(self, tmp_path):
        exe = tmp_path / "uv" / "tools" / "graphlm" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        assert detect_installer(exe) == "uv-tool"

    def test_uv_receipt_in_venv_root_is_uv_tool(self, tmp_path):
        # Install identity: uv-receipt.toml is written by `uv tool install`,
        # even if the venv is not under the default uv/tools/graphlm path.
        venv = tmp_path / "some-venv"
        (venv / "bin").mkdir(parents=True)
        exe = venv / "bin" / "python"
        exe.write_text("")
        (venv / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "graphlm", extras = ["all"] }]\n'
        )
        assert detect_installer(exe) == "uv-tool"

    def test_uv_tool_python_symlink_outside_venv(self, tmp_path):
        # uv tool (and some pyenv) venvs symlink bin/python at a managed
        # CPython *outside* the tool dir. Path.resolve() walks out of
        # uv/tools/graphlm and 0.4.0 then mis-classified the install as pip
        # (#71). Detection must not follow that symlink.
        real = tmp_path / "managed-cpython" / "bin" / "python3"
        real.parent.mkdir(parents=True)
        real.write_text("")
        exe = tmp_path / "uv" / "tools" / "graphlm" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.symlink_to(real)
        assert exe.resolve() != exe
        assert "uv/tools/graphlm" not in exe.resolve().as_posix()
        assert detect_installer(exe) == "uv-tool"

    def test_pipx_path(self, tmp_path):
        exe = tmp_path / "pipx" / "venvs" / "graphlm" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        assert detect_installer(exe) == "pipx"

    def test_pipx_python_symlink_outside_venv(self, tmp_path):
        real = tmp_path / "elsewhere" / "python"
        real.parent.mkdir(parents=True)
        real.write_text("")
        exe = tmp_path / "pipx" / "venvs" / "graphlm" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.symlink_to(real)
        assert detect_installer(exe) == "pipx"


class TestInstalledExtras:
    def test_uv_receipt(self, tmp_path):
        rec = tmp_path / "graphlm" / "uv-receipt.toml"
        rec.parent.mkdir(parents=True)
        rec.write_text(
            '[tool]\nrequirements = [{ name = "graphlm", extras = ["all", "mcp"] }]\n'
        )
        extras = installed_extras(installer="uv-tool", uv_tools_root=tmp_path)
        assert extras == ("mcp", "all")

    def test_pipx_metadata_list(self, tmp_path):
        meta = tmp_path / "graphlm" / "pipx_metadata.json"
        meta.parent.mkdir(parents=True)
        meta.write_text('{"main_package": {"extras": ["js", "mcp"]}}\n')
        extras = installed_extras(installer="pipx", pipx_venvs_root=tmp_path)
        assert extras == ("mcp", "js")

    def test_pipx_metadata_spec_string(self, tmp_path):
        meta = tmp_path / "graphlm" / "pipx_metadata.json"
        meta.parent.mkdir(parents=True)
        meta.write_text(
            '{"main_package": {"package_or_url": "graphlm[all,mcp]"}}\n'
        )
        extras = installed_extras(installer="pipx", pipx_venvs_root=tmp_path)
        assert extras == ("mcp", "all")

    def test_missing_receipt_falls_back_to_probe(self, tmp_path):
        extras = installed_extras(installer="uv-tool", uv_tools_root=tmp_path)
        assert isinstance(extras, tuple)


class TestMakePlan:
    def test_uv_tool_symlink_plan_uses_uv(self, tmp_path):
        real = tmp_path / "managed-cpython" / "python"
        real.parent.mkdir(parents=True)
        real.write_text("")
        exe = tmp_path / "uv" / "tools" / "graphlm" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.symlink_to(real)
        plan = make_plan(
            executable=exe,
            which=lambda n: "/usr/bin/uv" if n == "uv" else None,
        )
        assert plan.installer == "uv-tool"
        assert plan.argv == ("/usr/bin/uv", "tool", "upgrade", "graphlm")

    def test_uv_tool_upgrade_argv(self, tmp_path):
        exe = tmp_path / "uv" / "tools" / "graphlm" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        rec = tmp_path / "tools" / "graphlm" / "uv-receipt.toml"
        rec.parent.mkdir(parents=True)
        rec.write_text(
            '[tool]\nrequirements = [{ name = "graphlm", extras = ["all"] }]\n'
        )
        plan = make_plan(
            executable=exe,
            which=lambda n: "/usr/bin/uv" if n == "uv" else None,
            uv_tools_root=tmp_path / "tools",
        )
        assert plan.installer == "uv-tool"
        assert plan.argv == ("/usr/bin/uv", "tool", "upgrade", "graphlm")
        assert "all" in plan.message

    def test_uv_missing_binary_refuses(self, tmp_path):
        exe = tmp_path / "uv" / "tools" / "graphlm" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        plan = make_plan(executable=exe, which=lambda n: None)
        assert plan.argv == ()
        assert "uv" in plan.message

    def test_pipx_upgrade_argv(self, tmp_path):
        exe = tmp_path / "pipx" / "venvs" / "graphlm" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        plan = make_plan(
            executable=exe,
            which=lambda n: "/usr/bin/pipx" if n == "pipx" else None,
        )
        assert plan.installer == "pipx"
        assert plan.argv == ("/usr/bin/pipx", "upgrade", "graphlm")

    def test_source_checkout_refuses(self, tmp_path, monkeypatch):
        # Pretend graphlm.__file__ is this repo, not site-packages.
        import graphlm as pkg

        monkeypatch.setattr(
            pkg, "__file__", str(tmp_path / "graphLM" / "graphlm" / "__init__.py")
        )
        exe = tmp_path / "graphLM" / ".venv" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        plan = make_plan(executable=exe, which=lambda n: None)
        assert plan.installer == "source"
        assert plan.argv == ()
        assert "source checkout" in plan.message

    def test_uv_pip_install_upgrades_with_uv(self, tmp_path, monkeypatch):
        import graphlm as pkg

        monkeypatch.setattr(
            pkg,
            "__file__",
            str(tmp_path / "lib" / "site-packages" / "graphlm" / "__init__.py"),
        )
        monkeypatch.setattr("graphlm.upgrade._is_running_interpreter", lambda _exe: True)
        monkeypatch.setattr("graphlm.upgrade._dist_installer", lambda: "uv")
        monkeypatch.setattr(
            "graphlm.upgrade.installed_extras",
            lambda **k: ("all",),
        )
        exe = tmp_path / "venv" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        plan = make_plan(
            executable=exe,
            which=lambda n: "/usr/bin/uv" if n == "uv" else None,
        )
        assert plan.installer == "uv-pip"
        assert plan.argv == (
            "/usr/bin/uv",
            "pip",
            "install",
            "--python",
            str(exe),
            "--upgrade",
            "graphlm[all]",
        )
        assert "uv pip" in plan.message

    def test_pip_restates_extras(self, tmp_path, monkeypatch):
        import importlib.util as util

        import graphlm as pkg

        monkeypatch.setattr(
            pkg,
            "__file__",
            str(tmp_path / "lib" / "site-packages" / "graphlm" / "__init__.py"),
        )
        exe = tmp_path / "venv" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        monkeypatch.setattr(
            "graphlm.upgrade.installed_extras",
            lambda **k: ("mcp", "all"),
        )
        orig = util.find_spec

        def _find(name, package=None):
            if name == "pip":
                return object()
            return orig(name, package)

        monkeypatch.setattr("graphlm.upgrade.importlib.util.find_spec", _find)
        plan = make_plan(executable=exe, which=lambda n: None)
        assert plan.installer == "pip"
        assert plan.argv[1:] == ("-P", "-m", "pip", "install", "--upgrade", "graphlm[mcp,all]")

    def test_pip_without_pip_module_refuses(self, tmp_path, monkeypatch):
        import importlib.util as util

        import graphlm as pkg

        monkeypatch.setattr(
            pkg,
            "__file__",
            str(tmp_path / "lib" / "site-packages" / "graphlm" / "__init__.py"),
        )
        exe = tmp_path / "venv" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        orig = util.find_spec

        def _find(name, package=None):
            if name == "pip":
                return None
            return orig(name, package)

        monkeypatch.setattr("graphlm.upgrade.importlib.util.find_spec", _find)
        plan = make_plan(executable=exe, which=lambda n: None)
        assert plan.argv == ()
        assert "pip is not importable" in plan.message
        assert "uv tool upgrade graphlm" in plan.message


class TestRunUpgrade:
    def test_runs_argv_and_returns_code(self, tmp_path):
        exe = tmp_path / "uv" / "tools" / "graphlm" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        calls: list[list[str]] = []

        cwds: list[object] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            cwds.append(kwargs.get("cwd"))
            return subprocess.CompletedProcess(argv, 0)

        lines: list[str] = []
        code = run_upgrade(
            executable=exe,
            which=lambda n: "/bin/uv" if n == "uv" else None,
            runner=fake_run,
            echo=lines.append,
        )
        assert code == 0
        assert calls == [["/bin/uv", "tool", "upgrade", "graphlm"]]
        assert cwds == [_installer_cwd()]  # never the caller's cwd
        assert any("uv tool" in s for s in lines)

    def test_source_returns_2_without_running(self, tmp_path, monkeypatch):
        import graphlm as pkg

        monkeypatch.setattr(
            pkg, "__file__", str(tmp_path / "src" / "graphlm" / "__init__.py")
        )
        exe = tmp_path / "src" / ".venv" / "bin" / "python"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        ran = []
        code = run_upgrade(
            executable=exe,
            which=lambda n: None,
            runner=lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(argv, 0),
            echo=lambda s: None,
        )
        assert code == 2
        assert ran == []
