"""Fresh-run cleanup: keep the output dir to exactly the artifacts this run wrote.

When graphlm regenerates a map, any *other* graphlm-owned artifact left in the
output directory is stale — most importantly a ``GRAPH.json`` from an earlier
graphlm version (now written only under ``--json``) or from a run with different
flags. Left behind, an agent could glob that stale machine-readable map beside a
fresh ``GRAPH.md``. This module removes graphlm's own stale artifacts and
detects when the prior map came from a different graphlm version so the diff can
report that honestly instead of masquerading as a first run.

Safety contract (this runs on the write path, *after* the paid LLM call):
- **Never raises.** A failed ``unlink`` logs a warning; the new graph is still
  written. An escaping exception would discard the freshly-generated graph.
- **Exact artifact names only** — derived from the same suffix params
  ``write_outputs`` uses, never a directory and never a glob. ``-o`` is literal
  and may point at a project root full of user files.
- **Skips symlinks** (#33 — graphlm never removes what it did not create).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from graphlm.models import CodebaseGraph

if TYPE_CHECKING:
    from graphlm.diff import BaselineState

logger = logging.getLogger(__name__)

# graphlm's internal JSON working copy. Always written (unlike the opt-in
# user-facing GRAPH.json deliverable) because the graph-vs-graph diff needs the
# prior run's JSON as its baseline and ``--serve`` reads a JSON map. Dot-prefixed
# so it reads as internal state; the scanner excludes it like the other
# artifacts. Defined here (the artifact-names owner) and imported by
# render/cli/scanner so there is one source of truth, not a hardcoded copy.
STATE_FILENAME = ".graph-state.json"


@dataclass(frozen=True, slots=True)
class BaselinePlan:
    """The pre-write decisions for one ``write_outputs`` run.

    ``old_graph`` is the graph to diff against (``None`` for first-run /
    uncomparable / version-changed). ``state`` is the resolved baseline state.
    ``old_version`` / ``new_version`` name the two graphlm versions for the diff.
    """

    old_graph: Optional[CodebaseGraph]
    state: "BaselineState"
    old_version: Optional[str]
    new_version: Optional[str]
    # True when graphlm has written to this output dir before (its working copy
    # is present, or a prior GRAPH.json parses as a graphlm graph) — the gate for
    # cleanup, so a run into a fresh user directory (`-o docs/`) never deletes a
    # same-named user file.
    owns_dir: bool


def prepare_baseline(
    output_dir: Path,
    new_graph: CodebaseGraph,
    *,
    state_path: Path,
    json_suffix: str,
) -> BaselinePlan:
    """Read the baseline and resolve the version-change state (no writes).

    Baseline source order (ADR-002 dec.1 — read before any overwrite): the
    working copy at ``state_path``, else — for the first post-upgrade run, when
    no working copy exists yet — the prior ``{json_suffix}.json``. The prior
    JSON is classified exactly as before 0.6 (FIRST_RUN / UNCOMPARABLE / NORMAL),
    so an absent/corrupt file never masquerades. A version change then routes a
    NORMAL baseline to ``VERSION_CHANGED`` (old_graph dropped — not a comparison).

    ``owns_dir`` gates cleanup. It is True when the working copy exists (that
    name is graphlm's own), or — with no working copy — when the prior
    GRAPH.json is a graphlm graph (NORMAL). A ``GRAPH.json`` that merely
    exists but is not one (UNCOMPARABLE: a user's own file in an ``-o``
    directory) must not switch deletion on; that inverted the guard.
    """
    from graphlm.diff import BaselineState, load_baseline

    old_graph, state = load_baseline(state_path)
    owns_dir = state is not BaselineState.FIRST_RUN  # a state file was present
    if state is BaselineState.FIRST_RUN:
        legacy_path = output_dir / f"{json_suffix}.json"
        old_graph, state = load_baseline(legacy_path)
        owns_dir = state is BaselineState.NORMAL  # a prior graphlm GRAPH.json

    current_version = new_graph.meta.graphlm_version if new_graph.meta else None
    old_version = prior_version(old_graph)
    if state is BaselineState.NORMAL and version_changed(old_version, current_version):
        return BaselinePlan(
            None, BaselineState.VERSION_CHANGED, old_version, current_version, owns_dir
        )
    return BaselinePlan(old_graph, state, old_version, current_version, owns_dir)


def graphlm_artifact_names(
    *, md_suffix: str, json_suffix: str, html_suffix: str, diff_suffix: str
) -> set[str]:
    """The exact filenames graphlm owns in the output dir, for the given suffixes.

    Derived from the suffix params (not hardcoded to ``GRAPH``) so a library
    caller writing ``map.json`` / ``map_DIFF.*`` has *those* names cleaned, not
    the defaults. Includes the internal working copy.
    """
    return {
        f"{md_suffix}.md",
        f"{json_suffix}.json",
        f"{html_suffix}.html",
        f"{diff_suffix}_DIFF.md",
        f"{diff_suffix}_DIFF.json",
        STATE_FILENAME,
    }


def keep_names(
    *,
    md_suffix: str,
    json_suffix: str,
    html_suffix: str,
    diff_suffix: str,
    json: bool,
    html: bool,
    diff: bool,
) -> set[str]:
    """The artifact filenames a run with these flags (re)writes — never deleted.

    Always includes the Markdown map and the working copy; the JSON deliverable,
    diff files, and HTML are included only when their flag is on.
    """
    keep = {f"{md_suffix}.md", STATE_FILENAME}
    if json:
        keep.add(f"{json_suffix}.json")
        if diff:
            keep.add(f"{diff_suffix}_DIFF.json")
    if html:
        keep.add(f"{html_suffix}.html")
    if diff:
        keep.add(f"{diff_suffix}_DIFF.md")
    return keep


def prior_version(baseline: Optional[CodebaseGraph]) -> Optional[str]:
    """The graphlm version stamped on the baseline graph, or ``None`` if unknown."""
    if baseline is None or baseline.meta is None:
        return None
    return baseline.meta.graphlm_version


def version_changed(baseline_version: Optional[str], current_version: Optional[str]) -> bool:
    """True when the baseline came from a different graphlm version.

    Plain equality with ``None`` treated as a distinct value (a meta-less prior
    map counts as a different version). No semver parsing — a downgrade is also a
    change, and "fresh regeneration" is the right response either way. When the
    *current* version is unknown (a source checkout / uninstalled package), we
    cannot tell versions apart, so we never claim a change (``False``) — the run
    is treated as a normal comparison rather than triggering a spurious cleanup.
    """
    if current_version is None:
        return False
    return baseline_version != current_version


def remove_stale_artifacts(
    output_dir: Path, names: set[str], *, keep: set[str]
) -> list[str]:
    """Delete graphlm-owned artifacts in ``output_dir`` except those in ``keep``.

    ``keep`` is the set of filenames this run is (re)writing — never delete what
    we are about to write. Returns the names actually removed (for logging/tests).
    Best-effort: a symlink is skipped, and any ``unlink`` failure is logged and
    swallowed so the write path is never aborted.
    """
    removed: list[str] = []
    for name in sorted(names - keep):
        target = output_dir / name
        # Broad guard: this runs after the paid LLM call, so nothing here may
        # abort the write. Even is_symlink()/exists() can raise (e.g. an OS
        # error resolving the path), so wrap the whole per-file body.
        try:
            if target.is_symlink():
                logger.warning(
                    "graphlm: not removing symlinked artifact %s (left in place)",
                    target,
                )
                continue
            if target.exists():
                target.unlink()
                removed.append(name)
        except Exception as exc:
            logger.warning("graphlm: could not remove stale artifact %s: %s", target, exc)
    return removed
