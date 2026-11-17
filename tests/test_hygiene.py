"""Credential hygiene: child processes don't inherit graphlm's keys, and a
readable user config is called out."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from graphlm import config, provenance
from graphlm import setup as S
from graphlm import upgrade as U
from graphlm.child_env import child_env


def test_child_env_drops_graphlm_and_typesafe_keys(monkeypatch):
    monkeypatch.setenv("GRAPHLM_API_KEY", "k1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k2")
    monkeypatch.setenv("graphlm_lower", "k3")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:3128")
    monkeypatch.setenv("UV_INDEX_URL", "https://mirror.example.test/simple")
    env = child_env()
    assert "GRAPHLM_API_KEY" not in env and "TYPESAFE_API_KEY" not in env
    assert "graphlm_lower" not in env
    assert env["HTTPS_PROXY"] and env["UV_INDEX_URL"] and env["PATH"]


def test_git_does_not_see_the_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPHLM_API_KEY", "INERT_KEY")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(provenance.subprocess, "run", fake_run)
    provenance.git_commit_sha(tmp_path)
    assert "GRAPHLM_API_KEY" not in seen["env"]


def test_installer_does_not_see_the_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPHLM_API_KEY", "INERT_KEY")
    monkeypatch.setattr(U, "detect_installer", lambda exe, **k: "uv-tool")
    monkeypatch.setattr(U, "installed_extras", lambda **k: ())
    seen = {}

    def runner(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    U.run_upgrade(executable=Path(sys.executable), which=lambda n: "/usr/bin/uv",
                  runner=runner, echo=lambda s: None)
    assert "GRAPHLM_API_KEY" not in seen["env"]


def test_setup_installer_does_not_see_the_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "INERT_KEY")
    monkeypatch.setattr(S, "detect_installer", lambda exe: "uv-tool")
    monkeypatch.setattr(S, "installed_extras", lambda **k: ())
    seen = {}

    def runner(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    S.run_setup(echo=lambda s: None, prompt=lambda p: "1", runner=runner,
                which=lambda n: "/usr/bin/uv", executable=Path(sys.executable),
                home=tmp_path)
    assert "TYPESAFE_API_KEY" not in seen["env"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.parametrize("mode,warns", [(0o600, False), (0o640, True), (0o644, True), (0o604, True)])
def test_readable_user_config_warns(monkeypatch, tmp_path, caplog, mode, warns):
    env = tmp_path / "graphlm" / ".env"
    env.parent.mkdir()
    env.write_text("GRAPHLM_MODEL=m\n")
    env.chmod(mode)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GRAPHLM_MODEL", raising=False)
    try:
        with caplog.at_level(logging.WARNING, logger="graphlm.config"):
            config.load_user_env()
    finally:
        # load_dotenv wrote it straight into os.environ; monkeypatch would
        # restore that value at teardown, so pop it directly.
        os.environ.pop("GRAPHLM_MODEL", None)
    assert ("chmod 600" in caplog.text) is warns
