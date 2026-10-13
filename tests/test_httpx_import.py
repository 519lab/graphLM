"""httpx stays unloaded until an LLM call.

``import graphlm`` pulls in redaction and the parsers on purpose. It must not
pull in httpx (and Rich behind it) for a dry run, --help, --version, or --serve.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "small_project"


def _child(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["HOME"] = str(tmp_path)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    env.pop("GRAPHLM_BASE_URL", None)
    env.pop("GRAPHLM_API_KEY", None)
    env.pop("GRAPHLM_MODEL", None)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr + result.stdout


def test_import_graphlm_does_not_import_httpx(tmp_path: Path) -> None:
    result = _child(
        """
        import sys
        import graphlm
        assert "httpx" not in sys.modules
        assert "rich" not in sys.modules
        # Redaction and the parsers stay eager.
        assert "graphlm.redact" in sys.modules
        assert "graphlm.parsers" in sys.modules
        assert "graphlm.parsers.python" in sys.modules
        """,
        tmp_path,
    )
    _assert_ok(result)


def test_dry_run_does_not_import_httpx(tmp_path: Path) -> None:
    result = _child(
        f"""
        import sys
        sys.argv = ["graphlm", {str(FIXTURE)!r}, "--dry-run"]
        from graphlm.cli import app
        try:
            app()
        except SystemExit as exc:
            assert exc.code in (0, None)
        assert "httpx" not in sys.modules
        assert "rich" not in sys.modules
        assert "graphlm.redact" in sys.modules
        """,
        tmp_path,
    )
    _assert_ok(result)


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["--serve", "."]])
def test_short_circuits_do_not_import_httpx(tmp_path: Path, argv: list[str]) -> None:
    shown = repr(["graphlm", *argv])
    result = _child(
        f"""
        import sys
        sys.argv = {shown}
        from graphlm.cli import app
        try:
            app()
        except SystemExit:
            pass
        assert "httpx" not in sys.modules, "httpx loaded for {shown}"
        """,
        tmp_path,
    )
    _assert_ok(result)


def test_call_llm_imports_httpx(tmp_path: Path) -> None:
    """The client still loads httpx once a call is actually made."""
    result = _child(
        """
        import sys
        from graphlm.llm import GraphLLErrorUnconfigured, call_llm
        assert "httpx" not in sys.modules
        try:
            call_llm("", "", "m", "s", "u")
        except GraphLLErrorUnconfigured:
            pass
        assert "httpx" in sys.modules
        """,
        tmp_path,
    )
    _assert_ok(result)
