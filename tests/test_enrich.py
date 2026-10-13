"""Tests for ``graphlm.enrich`` — the post-pass-2 local-fill helpers.

These cover ``score_file_importance``'s **fusion** path without the
``typesafe-sdk`` extra: ``evidence`` is injected, so a stub whose
``score_importance`` returns a roles dict never reaches the SDK. (The real
Jev-role scoring against ``typesafe_sdk`` is covered by ``test_evidence.py``,
which is ``importorskip``-gated on the extra — see #188.) ``evidence._norm`` and
``evidence.file_degree`` are pure and touch no SDK, so the real ones are used.
"""

from __future__ import annotations

from types import SimpleNamespace

import graphlm.evidence as _evidence
from graphlm.enrich import (
    build_meta,
    is_directory_granular,
    pass_usage,
    score_file_importance,
)
from graphlm.models import (
    CodebaseGraph,
    FileSummary,
    ImportEdge,
    ModuleDescription,
)


def _edge(a: str, b: str) -> ImportEdge:
    return ImportEdge(from_path=a, to_path=b, kind="import")


def _graph(*summaries: str, edges: list[ImportEdge] | None = None) -> CodebaseGraph:
    return CodebaseGraph(
        directory_tree="root/",
        file_summaries=[FileSummary(path=p, summary=f"{p} does things") for p in summaries],
        deterministic_edges=edges or [],
    )


def _stub_evidence(roles: dict[str, float] | None) -> SimpleNamespace:
    """An ``evidence`` stand-in: real pure helpers + a fake ``score_importance``."""
    return SimpleNamespace(
        _norm=_evidence._norm,
        file_degree=_evidence.file_degree,
        score_importance=lambda *a, **k: roles,
    )


# --- score_file_importance: the fusion path (enrich.py 57-92) ----------------


class TestScoreFileImportance:
    def test_none_without_summaries(self):
        g = CodebaseGraph(directory_tree="root/")  # no file_summaries
        ev = _stub_evidence({"a.py": 2.0})
        assert score_file_importance(
            g, [], api_key="k", summaries_by_path={}, evidence=ev
        ) is None

    def test_none_when_roles_none(self):
        g = _graph("a.py")
        ev = _stub_evidence(None)  # Jev off / failed
        assert score_file_importance(
            g, [], api_key="k", summaries_by_path={}, evidence=ev
        ) is None

    def test_none_when_no_scored_paths(self):
        # roles maps a path that isn't among the file_mods → scored is empty.
        g = _graph("a.py")
        ev = _stub_evidence({"unrelated.py": 2.0})
        assert score_file_importance(
            g, [], api_key="k", summaries_by_path={}, evidence=ev
        ) is None

    def test_sorts_load_bearing_first_and_fused_in_range(self):
        g = _graph(
            "core.py",
            "leaf.py",
            edges=[_edge("leaf.py", "core.py")],  # core imported → higher degree
        )
        ev = _stub_evidence({"core.py": 3.0, "leaf.py": 0.0})
        res = score_file_importance(
            g, g.deterministic_edges, api_key="k", summaries_by_path={}, evidence=ev
        )
        assert res is not None
        assert [f.path for f in res] == ["core.py", "leaf.py"]  # load-bearing first
        assert res[0].role == 3.0 and res[1].role == 0.0  # role passes through
        assert all(0.0 <= f.fused <= 1.0 for f in res)
        # degree passes through from evidence.file_degree (core imported once).
        assert res[0].degree == 1 and res[1].degree == 1

    def test_only_roles_covered_paths_are_scored(self):
        # roles covers one of two summaries → only that one is returned.
        g = _graph("a.py", "b.py")
        ev = _stub_evidence({"a.py": 2.0})  # b.py has no role
        res = score_file_importance(
            g, [], api_key="k", summaries_by_path={}, evidence=ev
        )
        assert res is not None
        assert [f.path for f in res] == ["a.py"]

    def test_single_distinct_degree_dmax_zero_branch(self):
        # All files share degree 0 (no edges) → dmax == 0 → drank defaults to 1.0.
        g = _graph("a.py", "b.py")  # no edges → both degree 0
        ev = _stub_evidence({"a.py": 3.0, "b.py": 0.0})
        res = score_file_importance(
            g, [], api_key="k", summaries_by_path={}, evidence=ev
        )
        assert res is not None
        # fused = 0.6*(role/3) + 0.4*1.0 (degree rank is 1.0 for the sole degree).
        by_path = {f.path: f for f in res}
        assert by_path["a.py"].fused == round(0.6 * 1.0 + 0.4 * 1.0, 4)  # role 3
        assert by_path["b.py"].fused == round(0.6 * 0.0 + 0.4 * 1.0, 4)  # role 0


# --- is_directory_granular ---------------------------------------------------


class TestIsDirectoryGranular:
    def test_empty_modules_is_false(self):
        assert is_directory_granular(CodebaseGraph(directory_tree="t/")) is False

    def test_most_paths_are_directories(self):
        g = CodebaseGraph(
            directory_tree="t/",
            modules=[
                ModuleDescription(path="src/pkg", name="pkg", description="x"),
                ModuleDescription(path="src/core", name="core", description="x"),
                ModuleDescription(path="src/app.py", name="app", description="x"),
            ],
        )
        assert is_directory_granular(g) is True  # 2 of 3 are dir-like

    def test_most_paths_are_files(self):
        g = CodebaseGraph(
            directory_tree="t/",
            modules=[
                ModuleDescription(path="a.py", name="a", description="x"),
                ModuleDescription(path="b.py", name="b", description="x"),
                ModuleDescription(path="pkg", name="pkg", description="x"),
            ],
        )
        assert is_directory_granular(g) is False  # 1 of 3 dir-like


# --- pass_usage --------------------------------------------------------------


class TestPassUsage:
    def test_reads_ints(self):
        pu = pass_usage({"prompt_tokens": 900, "completion_tokens": 50}, estimated=1000)
        assert pu.prompt_tokens == 900
        assert pu.completion_tokens == 50
        assert pu.estimated_prompt_tokens == 1000

    def test_missing_and_non_int_read_as_none(self):
        pu = pass_usage({"prompt_tokens": "not-a-number"}, estimated=10)
        assert pu.prompt_tokens is None  # string → None
        assert pu.completion_tokens is None  # missing → None

    def test_bool_excluded(self):
        # bool is an int subclass; True must not stamp as 1.
        pu = pass_usage({"prompt_tokens": True}, estimated=10)
        assert pu.prompt_tokens is None

    def test_none_usage(self):
        pu = pass_usage(None, estimated=42)
        assert pu.prompt_tokens is None and pu.estimated_prompt_tokens == 42


# --- build_meta --------------------------------------------------------------


class TestBuildMeta:
    def test_stamps_version_and_timestamp(self, tmp_path):
        meta = build_meta(tmp_path)  # a non-git tmp dir
        assert meta.created_at.endswith("Z")
        assert meta.commit_sha is None  # not a git repo
        # graphlm_version is the installed version or None (source checkout) —
        # either is valid; the point is it never raises.
