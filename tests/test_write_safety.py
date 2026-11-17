"""Write-path symlink safety shared by skill install and write_outputs."""

from __future__ import annotations

from pathlib import Path

import pytest

from graphlm.safe_write import refuse_symlink
from graphlm.skills import install_skill


def _victim(tmp_path: Path) -> Path:
    victim = tmp_path / "victim"
    victim.mkdir()
    return victim


def test_local_install_refuses_symlinked_dot_claude(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    victim = _victim(tmp_path)
    (proj / ".claude").symlink_to(victim, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        install_skill("claude", project_dir=proj, local=True, home=tmp_path / "home")
    assert list(victim.iterdir()) == []  # nothing created outside the project


def test_local_install_refuses_symlinked_skill_dir_even_with_force(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude" / "skills").mkdir(parents=True)
    victim = _victim(tmp_path)
    (victim / "SKILL.md").write_text("USER FILE")
    (proj / ".claude" / "skills" / "graphlm").symlink_to(victim, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        install_skill("claude", project_dir=proj, local=True, force=True,
                      home=tmp_path / "home")
    assert (victim / "SKILL.md").read_text() == "USER FILE"


def test_global_install_still_works_through_a_dotfiles_linked_home_dir(tmp_path):
    # ~/.claude managed by a dotfiles repo is the user's own choice; the
    # scanned repo does not control the home directory.
    home = tmp_path / "home"
    home.mkdir()
    dotfiles = tmp_path / "dotfiles" / ".claude"
    dotfiles.mkdir(parents=True)
    (home / ".claude").symlink_to(dotfiles, target_is_directory=True)
    result = install_skill("claude", home=home)
    assert not result.skipped
    assert (dotfiles / "skills" / "graphlm" / "SKILL.md").is_file()


def test_global_install_still_refuses_a_symlinked_target_file(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "graphlm").mkdir(parents=True)
    canary = tmp_path / "canary"
    canary.write_text("x")
    (home / ".claude" / "skills" / "graphlm" / "SKILL.md").symlink_to(canary)
    with pytest.raises(ValueError, match="symlink"):
        install_skill("claude", home=home, force=True)
    assert canary.read_text() == "x"


def test_refuse_symlink_checks_every_ancestor_without_below(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        refuse_symlink(tmp_path / "link" / "a" / "b.txt")


def test_refuse_symlink_below_ignores_the_root_and_above(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "root").symlink_to(real, target_is_directory=True)
    root = tmp_path / "root"
    refuse_symlink(root / "a" / "b.txt", below=root)  # root itself may be a link
    (real / "a").mkdir()
    (real / "inner").mkdir()
    (real / "a2").symlink_to(real / "inner", target_is_directory=True)
    with pytest.raises(ValueError):
        refuse_symlink(root / "a2" / "b.txt", below=root)
