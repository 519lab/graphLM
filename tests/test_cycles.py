"""Tests for import cycle detection (Tarjan's SCC algorithm)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from graphlm.cycles import (
    _build_adjacency,
    _compute_risk_score,
    tarjan_scc,
    compute_sloc_map,
    detect_cycles,
)
from graphlm.models import Cycle, ImportEdge
from graphlm.scanner import FileFragment
from graphlm.testpaths import is_test_path


def _edge(a: str, b: str, kind: str = "import") -> ImportEdge:
    return ImportEdge(from_path=a, to_path=b, kind=kind)


# ---------------------------------------------------------------------------
# Tarjan's algorithm (low-level)
# ---------------------------------------------------------------------------


class TestTarjanSCC:
    def test_no_cycles(self):
        nodes = {"a", "b", "c"}
        adj = {"a": {"b"}, "b": {"c"}, "c": set()}
        sccs = tarjan_scc(nodes, adj)
        assert all(len(scc) == 1 for scc in sccs)

    def test_two_way_cycle(self):
        nodes = {"a", "b"}
        adj = {"a": {"b"}, "b": {"a"}}
        sccs = tarjan_scc(nodes, adj)
        scc_with_two = [s for s in sccs if len(s) == 2]
        assert len(scc_with_two) == 1
        assert set(scc_with_two[0]) == {"a", "b"}

    def test_three_node_cycle(self):
        nodes = {"a", "b", "c"}
        adj = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
        sccs = tarjan_scc(nodes, adj)
        scc_with_three = [s for s in sccs if len(s) == 3]
        assert len(scc_with_three) == 1
        assert set(scc_with_three[0]) == {"a", "b", "c"}

    def test_mixed_graph(self):
        """A graph with both cyclic and acyclic parts."""
        nodes = {"a", "b", "c", "d", "e"}
        adj = {
            "a": {"b"},
            "b": {"a", "c"},
            "c": set(),
            "d": {"e"},
            "e": {"d"},
        }
        sccs = tarjan_scc(nodes, adj)
        multi = [s for s in sccs if len(s) > 1]
        assert len(multi) == 2
        sets = {frozenset(s) for s in multi}
        assert {"a", "b"} in {frozenset(s) for s in multi}
        assert {"d", "e"} in sets

    def test_self_loop_not_scc(self):
        """A self-loop alone is a single-node SCC and should be excluded."""
        nodes = {"a"}
        adj = {"a": {"a"}}
        sccs = tarjan_scc(nodes, adj)
        assert len(sccs) == 1
        assert sccs[0] == ["a"]


# ---------------------------------------------------------------------------
# detect_cycles (high-level)
# ---------------------------------------------------------------------------


class TestDetectCycles:
    def test_empty_edges(self):
        assert detect_cycles([]) == []

    def test_no_cycles(self):
        edges = [
            _edge("a.py", "b.py"),
            _edge("b.py", "c.py"),
        ]
        assert detect_cycles(edges) == []

    def test_mutual_dependency(self):
        edges = [
            _edge("a.py", "b.py"),
            _edge("b.py", "a.py"),
        ]
        cycles = detect_cycles(edges)
        assert len(cycles) == 1
        assert cycles[0].length == 2
        assert set(cycles[0].nodes) == {"a.py", "b.py"}

    def test_three_node_cycle(self):
        edges = [
            _edge("main.py", "routes.py"),
            _edge("routes.py", "services.py"),
            _edge("services.py", "main.py"),
        ]
        cycles = detect_cycles(edges)
        assert len(cycles) == 1
        assert cycles[0].length == 3
        assert set(cycles[0].nodes) == {"main.py", "routes.py", "services.py"}

    def test_self_loop_excluded(self):
        edges = [_edge("a.py", "a.py")]
        assert detect_cycles(edges) == []

    def test_multiple_cycles_sorted_by_risk(self):
        edges = [
            _edge("a.py", "b.py"),
            _edge("b.py", "a.py"),
            _edge("c.py", "d.py"),
            _edge("d.py", "e.py"),
            _edge("e.py", "c.py"),
        ]
        cycles = detect_cycles(edges)
        assert len(cycles) == 2
        assert cycles[0].risk_score >= cycles[1].risk_score

    def test_only_self_loops(self):
        edges = [
            _edge("a.py", "a.py"),
            _edge("b.py", "b.py"),
        ]
        assert detect_cycles(edges) == []

    def test_risk_score_without_sloc(self):
        edges = [
            _edge("a.py", "b.py"),
            _edge("b.py", "a.py"),
        ]
        cycles = detect_cycles(edges, sloc_map=None)
        assert len(cycles) == 1
        expected = _compute_risk_score(["a.py", "b.py"], None)
        assert cycles[0].risk_score == pytest.approx(expected)

    def test_edges_filtered_to_scc(self):
        edges = [
            _edge("a.py", "b.py"),
            _edge("b.py", "a.py"),
            _edge("x.py", "y.py"),
        ]
        cycles = detect_cycles(edges)
        assert len(cycles) == 1
        edge_paths = {(e.from_path, e.to_path) for e in cycles[0].edges}
        assert ("x.py", "y.py") not in edge_paths

    def test_cycle_nodes_sorted(self):
        edges = [
            _edge("zebra.py", "apple.py"),
            _edge("apple.py", "zebra.py"),
        ]
        cycles = detect_cycles(edges)
        assert cycles[0].nodes == ["apple.py", "zebra.py"]

    def test_dot_slash_prefix_still_scores_scanned_line_counts(self):
        # LLM import_edges often carry ./; sloc_map is keyed by scanner
        # rel_paths. Without normalisation the lookups miss and risk is 0 (#84).
        fragments = [
            FileFragment("a.py", "x", 1, line_count=101),
            FileFragment("b.py", "x", 1, line_count=21),
        ]
        edges = [
            _edge("./a.py", "./b.py"),
            _edge("./b.py", "./a.py"),
        ]
        (cycle,) = detect_cycles(edges, compute_sloc_map(fragments))
        assert cycle.nodes == ["a.py", "b.py"]
        assert cycle.risk_score == pytest.approx(math.log10(122) * 2)


# ---------------------------------------------------------------------------
# test_only classification
# ---------------------------------------------------------------------------


class TestCycleTestOnly:
    def test_all_production_is_not_test_only(self):
        edges = [_edge("a.py", "b.py"), _edge("b.py", "a.py")]
        (cycle,) = detect_cycles(edges)
        assert cycle.test_only is False

    def test_all_test_is_test_only(self):
        edges = [
            _edge("tests/a.py", "tests/b.py"),
            _edge("tests/b.py", "tests/a.py"),
        ]
        (cycle,) = detect_cycles(edges)
        assert cycle.test_only is True

    def test_mixed_cycle_is_not_test_only(self):
        # One production file caught in a cycle with test files → production.
        edges = [
            _edge("tests/a.py", "b.py"),
            _edge("b.py", "tests/a.py"),
        ]
        (cycle,) = detect_cycles(edges)
        assert cycle.test_only is False

    def test_fixture_dir_cycle_is_test_only(self):
        # A cycle among tests/fixtures/... (the graphlm dogfood case).
        edges = [
            _edge("tests/fixtures/p/foo.rs", "tests/fixtures/p/bar.rs"),
            _edge("tests/fixtures/p/bar.rs", "tests/fixtures/p/foo.rs"),
        ]
        (cycle,) = detect_cycles(edges)
        assert cycle.test_only is True

    def test_default_is_false_for_hand_built_cycle(self):
        # Additive-optional: a Cycle built without the field defaults False so
        # old baselines validate NORMAL.
        c = Cycle(nodes=["a.py"], edges=[], length=1, risk_score=1.0)
        assert c.test_only is False


# ---------------------------------------------------------------------------
# Risk score computation
# ---------------------------------------------------------------------------


class TestRiskScore:
    def test_no_sloc_map(self):
        nodes = ["a.py", "b.py", "c.py"]
        score = _compute_risk_score(nodes, None)
        expected = math.log10(3) * 3
        assert score == pytest.approx(expected)

    def test_with_sloc_map(self):
        nodes = ["a.py", "b.py"]
        sloc_map = {"a.py": 100, "b.py": 200}
        score = _compute_risk_score(nodes, sloc_map)
        expected = math.log10(300) * 2
        assert score == pytest.approx(expected)

    def test_zero_sloc(self):
        score = _compute_risk_score(["a.py"], {"a.py": 0})
        assert score == 0.0

    def test_single_node_risk(self):
        nodes = ["a.py"]
        score = _compute_risk_score(nodes, None)
        expected = math.log10(1) * 1
        assert score == pytest.approx(expected)


# ---------------------------------------------------------------------------
# compute_sloc_map
# ---------------------------------------------------------------------------


class TestComputeSlocMap:
    def test_basic(self):
        fragments = [
            FileFragment("a.py", "x = 1\ny = 2\n", 1),
            FileFragment("b.py", "def foo():\n    pass\n", 1),
        ]
        sloc_map = compute_sloc_map(fragments)
        assert sloc_map["a.py"] == 3
        assert sloc_map["b.py"] == 3

    def test_empty_file(self):
        fragments = [FileFragment("empty.py", "", 1)]
        sloc_map = compute_sloc_map(fragments)
        assert sloc_map["empty.py"] == 1

    def test_empty_fragments(self):
        assert compute_sloc_map([]) == {}

    def test_uses_real_line_count_not_truncated_content(self):
        # A file longer than max_file_chars used to score by the lines that
        # survived the cut (skeleton or head slice), under-weighting exactly
        # the large modules a cycle risk score should weigh most.
        from graphlm.scanner import scan_project

        project = Path(__file__).parent / "fixtures" / "skeleton_project"
        source = (project / "big_module.py").read_text()
        real_lines = source.count("\n") + 1
        for skeleton in (True, False):
            result = scan_project(project, max_file_chars=500, skeleton=skeleton)
            (frag,) = [f for f in result.file_fragments if f.rel_path == "big_module.py"]
            assert frag.content.count("\n") + 1 < real_lines  # it really was cut
            assert compute_sloc_map(result.file_fragments)["big_module.py"] == real_lines

    def test_risk_score_reflects_real_size_of_cut_file(self):
        # Two-node cycle where one file is "large": with the real line count the
        # score is log10(1000 + 1) * 2, not log10(1 + 1) * 2.
        fragments = [
            FileFragment("a.py", "x = 1", 1, line_count=1000),
            FileFragment("b.py", "y = 2", 1, line_count=1),
        ]
        edges = [
            ImportEdge(from_path="a.py", to_path="b.py", kind="import"),
            ImportEdge(from_path="b.py", to_path="a.py", kind="import"),
        ]
        (cycle,) = detect_cycles(edges, compute_sloc_map(fragments))
        assert cycle.risk_score == pytest.approx(math.log10(1001) * 2)


# ---------------------------------------------------------------------------
# Integration: CodebaseGraph includes import_cycles
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_codebase_graph_has_import_cycles_field(self):
        from graphlm.models import CodebaseGraph

        graph = CodebaseGraph(directory_tree="root/")
        assert hasattr(graph, "import_cycles")
        assert graph.import_cycles == []

    def test_codebase_graph_with_cycles(self):
        from graphlm.models import CodebaseGraph

        graph = CodebaseGraph(
            directory_tree="root/",
            import_edges=[
                _edge("a.py", "b.py"),
                _edge("b.py", "a.py"),
            ],
        )
        graph.import_cycles = detect_cycles(graph.import_edges)
        assert len(graph.import_cycles) == 1
        assert graph.import_cycles[0].length == 2

    def test_generate_graph_includes_cycles(self, small_project):
        from graphlm import generate_graph

        result = generate_graph(small_project, dry_run=True)
        assert hasattr(result.graph, "import_cycles")
        assert isinstance(result.graph.import_cycles, list)

    def test_no_tests_drops_the_cycles_that_were_labelled_test_only(self, tmp_path):
        """Invariant: a cycle labelled ``test_only`` ⇔ dropped by ``--no-tests``.

        Build a self-contained project with a **Python** all-test cycle (Python
        is parsed in every install — the language-pack fixtures aren't, so this
        can't depend on which grammar extras are installed). With tests included
        the cycle is flagged ``test_only``; with ``--no-tests`` it disappears
        (its files aren't scanned).
        """
        from graphlm import generate_graph

        # A 2-file import cycle, both files under tests/ (so both are test paths).
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tmp_path / "app.py").write_text("VALUE = 1\n")  # a non-test module
        (tests_dir / "test_a.py").write_text("from tests import test_b\n")
        (tests_dir / "test_b.py").write_text("from tests import test_a\n")
        (tests_dir / "__init__.py").write_text("")

        with_tests = generate_graph(tmp_path, dry_run=True).graph
        labelled_test = [c for c in with_tests.import_cycles if c.test_only]
        assert labelled_test, "expected the all-test cycle to be labelled test_only"
        # Every test_only cycle is genuinely all test paths.
        for c in labelled_test:
            assert all(is_test_path(n) for n in c.nodes)

        without_tests = generate_graph(
            tmp_path, dry_run=True, include_tests=False
        ).graph
        # None of the previously-test_only cycles survive --no-tests.
        surviving = {frozenset(c.nodes) for c in without_tests.import_cycles}
        for c in labelled_test:
            assert frozenset(c.nodes) not in surviving
        # And nothing left after --no-tests is test_only (they were all dropped).
        assert not [c for c in without_tests.import_cycles if c.test_only]

    def test_render_markdown_includes_cycles(self):
        from graphlm.models import CodebaseGraph
        from graphlm.render import render_markdown

        graph = CodebaseGraph(
            directory_tree="root/",
            import_edges=[
                _edge("a.py", "b.py"),
                _edge("b.py", "a.py"),
            ],
        )
        graph.import_cycles = detect_cycles(graph.import_edges)
        md = render_markdown(graph)
        assert "## Import Cycles" in md
        assert "risk score:" in md
        assert "`a.py`" in md
        assert "`b.py`" in md

    def test_render_markdown_no_cycles(self):
        from graphlm.models import CodebaseGraph
        from graphlm.render import render_markdown

        graph = CodebaseGraph(directory_tree="root/")
        md = render_markdown(graph)
        assert "## Import Cycles" not in md

    def test_cycle_is_frozen_dataclass(self):
        cycle = Cycle(
            nodes=["a.py", "b.py"],
            edges=[],
            length=2,
            risk_score=0.602,
        )
        with pytest.raises(Exception):
            cycle.risk_score = 1.0

    def test_cyclic_fixture_project(self, fixtures_dir):
        """Verify the cyclic_project fixture has the expected structure."""
        app_dir = fixtures_dir / "cyclic_project" / "app"
        assert (app_dir / "main.py").exists()
        assert (app_dir / "routes.py").exists()
        assert (app_dir / "services.py").exists()
        assert (app_dir / "__init__.py").exists()
