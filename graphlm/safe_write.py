"""The one symlink guard for every file graphlm writes (#33).

``Path.write_text`` and ``mkdir`` follow symlinks, so a link at the target —
or at any directory on the way to it — redirects the write to wherever the
link points: a cloned repo that plants ``.graphlm/GRAPH.json → ~/.bashrc``,
or ``.claude → /elsewhere``, would make graphlm write a file it did not
create, outside the directory it was told to write in. ``write_outputs`` and
skill install both call :func:`refuse_symlink` before writing anything.
"""

from __future__ import annotations

from pathlib import Path


def _refuse(link: Path) -> None:
    raise ValueError(
        f"refusing to write through a symlink at {link} — graphlm won't "
        "overwrite a file it didn't create. Remove the symlink and re-run."
    )


def refuse_symlink(path: Path, *, below: Path | None = None) -> None:
    """Raise ``ValueError`` if ``path`` or a directory leading to it is a symlink.

    With ``below``, only ancestors strictly inside ``below`` are checked: the
    part of the path the scanned repository controls (``.claude/skills/…``
    under a project). ``below`` itself and the directories above it belong to
    the user — a home directory or a dotfiles-managed parent may legitimately
    be a link. Without it every ancestor is checked. ``is_symlink()`` also
    catches broken links, which ``exists()`` reports as absent.
    """
    if path.is_symlink():
        _refuse(path)
    for parent in path.parents:
        if below is not None and (parent == below or not parent.is_relative_to(below)):
            break  # reached the user's part of the path
        if parent.is_symlink():
            _refuse(parent)
