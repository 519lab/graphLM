"""Release artifact invariants."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _copy_project(tmp_path: Path) -> Path:
    """The files a build needs: metadata, the changelog (the version source), the package."""
    project_copy = tmp_path / "project"
    project_copy.mkdir()
    for filename in ("pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md"):
        shutil.copy2(PROJECT_ROOT / filename, project_copy / filename)
    shutil.copytree(
        PROJECT_ROOT / "graphlm",
        project_copy / "graphlm",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return project_copy


def _build(project: Path, out: Path, kind: str) -> Path:
    subprocess.run(
        ["uv", "build", kind, "--out-dir", str(out)],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    return next(out.iterdir())


def test_version_is_the_changelogs_top_release_section(tmp_path: Path) -> None:
    """The release workflow tags the changelog's top `## [X.Y.Z]` section, so the
    wheel must carry that version, and entries under [Unreleased] must not move it."""
    project = _copy_project(tmp_path)
    changelog = project / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8").replace(
            "## [Unreleased]\n",
            "## [Unreleased]\n\n### Fixed\n- not released yet\n\n"
            "## [9.9.9] - 2030-01-01\n\n### Fixed\n- the release\n",
            1,
        ),
        encoding="utf-8",
    )

    wheel = _build(project, tmp_path / "dist", "--wheel")

    assert wheel.name == "graphlm-9.9.9-py3-none-any.whl"


def test_pyproject_has_no_static_version() -> None:
    """A static version would drift from the tag the release workflow creates."""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    assert "version" not in pyproject["project"]
    assert pyproject["project"]["dynamic"] == ["version"]
    assert pyproject["tool"]["hatch"]["version"]["path"] == "CHANGELOG.md"


def test_sdist_excludes_claude_workspace_metadata(tmp_path: Path) -> None:
    """Workspace-agent state must never enter the public source archive."""
    project_copy = _copy_project(tmp_path)
    marker = project_copy / ".claude" / "packaging-sentinel.txt"
    marker.parent.mkdir()
    marker.write_text("must not ship\n", encoding="utf-8")

    archive = _build(project_copy, tmp_path / "dist", "--sdist")
    with tarfile.open(archive, "r:gz") as source_dist:
        archived_paths = source_dist.getnames()

    assert not any("/.claude/" in path for path in archived_paths)
