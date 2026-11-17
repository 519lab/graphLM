"""Tests for the render module."""

import copy
import json
import pickle
import random
from pathlib import Path
from tempfile import TemporaryDirectory

from graphlm.mermaid import render_mermaid
from graphlm.models import (
    ArchitectureNote,
    CodebaseGraph,
    Cycle,
    DBColumn,
    DBTable,
    DataFlowEdge,
    GraphMeta,
    ImportEdge,
    ModuleDescription,
    QuickReference,
    TestMapping,
)
from graphlm.freshness import STATE_FILENAME
from graphlm.render import WriteResult, render_json, render_markdown, write_outputs


class TestRenderMarkdown:
    def test_empty_graph(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        md = render_markdown(graph)
        assert "Codebase Graph" in md
        assert "root/" in md

    def test_with_import_edges(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_edges=[
                ImportEdge(from_path="a.py", to_path="b.py", kind="import"),
                ImportEdge(from_path="c.py", to_path="b.py", kind="from"),
            ],
        )
        md = render_markdown(graph)
        assert "| From | To | Kind |" in md
        assert "`a.py`" in md
        assert "import" in md

    def test_with_modules(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[
                ModuleDescription(path="main.py", name="Main", description="Entry point"),
                ModuleDescription(path="lib.py", name="Lib", description="Library"),
            ],
        )
        md = render_markdown(graph)
        assert "| Path | Name | Description |" in md
        assert "main.py" in md
        assert "Entry point" in md

    def test_with_database_schema(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            database_schema=[
                DBTable(
                    name="users",
                    columns=[
                        DBColumn(name="id", type="INTEGER", constraints="PRIMARY KEY"),
                        DBColumn(name="email", type="TEXT", constraints="NOT NULL"),
                    ],
                    description="User accounts",
                )
            ],
        )
        md = render_markdown(graph)
        assert "users" in md
        assert "User accounts" in md
        assert "`id`" in md
        assert "INTEGER" in md

    def test_with_data_flow(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            data_flow=[
                DataFlowEdge(source="API", destination="DB", description="Queries"),
            ],
        )
        md = render_markdown(graph)
        assert "| Source | Destination | Description |" in md
        assert "API" in md
        assert "DB" in md

    def test_with_quick_reference(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            quick_reference=[
                QuickReference(query="app factory", location="main.py"),
            ],
        )
        md = render_markdown(graph)
        assert "| Find | Location |" in md
        assert "app factory" in md
        assert "main.py" in md

    def test_with_test_organization(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            test_organization=[
                TestMapping(file="test_main.py", covers="App factory"),
            ],
        )
        md = render_markdown(graph)
        assert "| Test File | Covers |" in md
        assert "test_main.py" in md

    def test_with_architecture_notes(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            architecture_notes=[
                ArchitectureNote(note="No ORM used"),
                ArchitectureNote(note="Vanilla HTML/CSS/JS"),
            ],
        )
        md = render_markdown(graph)
        assert "No ORM used" in md
        assert "Vanilla HTML/CSS/JS" in md

    def test_js_cycle_gets_benign_qualifier(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_cycles=[
                Cycle(
                    nodes=["src/foo.ts", "src/bar.ts"],
                    edges=[],
                    length=2,
                    risk_score=1.0,
                )
            ],
        )
        md = render_markdown(graph)
        assert "often benign" in md
        assert "JavaScript/TypeScript" in md

    def test_python_cycle_has_no_js_qualifier(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_cycles=[
                Cycle(nodes=["a.py", "b.py"], edges=[], length=2, risk_score=1.0)
            ],
        )
        md = render_markdown(graph)
        assert "often benign" not in md

    def test_mixed_cycle_mentions_both_languages(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_cycles=[
                Cycle(nodes=["a.py", "b.py"], edges=[], length=2, risk_score=1.0),
                Cycle(
                    nodes=["src/foo.ts", "src/bar.ts"],
                    edges=[],
                    length=2,
                    risk_score=1.0,
                ),
            ],
        )
        md = render_markdown(graph)
        assert "Python import cycle" in md
        assert "JavaScript/TypeScript" in md

    def test_markdown_has_newline_terminator(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        md = render_markdown(graph)
        assert md.endswith("\n")


class TestTestOnlyCycleGrouping:
    def _prod(self, nodes: list[str], risk: float = 2.0) -> Cycle:
        return Cycle(nodes=nodes, edges=[], length=len(nodes), risk_score=risk)

    def _test(self, nodes: list[str], risk: float = 2.0) -> Cycle:
        return Cycle(
            nodes=nodes, edges=[], length=len(nodes), risk_score=risk, test_only=True
        )

    def test_all_test_only_gets_banner_no_production_heading(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_cycles=[self._test(["tests/a.py", "tests/b.py"])],
        )
        md = render_markdown(graph)
        assert "## Import Cycles" in md
        assert "All detected import cycles are among test files" in md
        assert "### Test-code cycles" in md
        assert "#### Test cycle 1" in md
        # No production "### Cycle N" heading when nothing is production.
        assert "### Cycle 1" not in md

    def test_production_first_then_test_group(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_cycles=[
                self._prod(["a.py", "b.py"], risk=3.0),
                self._test(["tests/c.py", "tests/d.py"], risk=1.0),
            ],
        )
        md = render_markdown(graph)
        # Production section appears before the test-code group.
        assert md.index("### Cycle 1") < md.index("### Test-code cycles")
        assert "#### Test cycle 1" in md
        # The banner (which claims *all* cycles are tests) must NOT appear when
        # some cycles are production; the italic descriptor is used instead.
        assert "All detected import cycles are among test files" not in md
        assert "*Every member is a test file (dropped under `--no-tests`).*" in md

    def test_production_only_section_is_byte_identical_to_pre_feature(self):
        # Pin the whole production-only cycles section as a golden string: with no
        # test_only cycles it must render exactly as it did before the feature
        # (`### Cycle N`, no grouping, no test wording).
        from graphlm.cycles_render import render_import_cycles

        cycles = [
            self._prod(["a.py", "b.py"], risk=2.0),
            self._prod(["x.py", "y.py", "z.py"], risk=1.0),
        ]
        section = "\n".join(render_import_cycles(cycles))
        assert section == (
            "## Import Cycles\n\n"
            "### Cycle 1 (risk score: 2.0)\n"
            "*2 nodes — mutual dependency*\n"
            "- `a.py`\n"
            "- `b.py`\n"
            "\n"
            "### Cycle 2 (risk score: 1.0)\n"
            "*3 nodes*\n"
            "- `x.py`\n"
            "- `y.py`\n"
            "- `z.py`\n"
        )
        assert "Test-code cycles" not in section
        assert "test file" not in section

    def test_js_qualifier_computed_over_production_only(self):
        # A test-only TS cycle must NOT drag a "JS often benign" note above a
        # "no production cycles" banner.
        graph = CodebaseGraph(
            directory_tree="root/",
            import_cycles=[self._test(["tests/foo.ts", "tests/bar.ts"])],
        )
        md = render_markdown(graph)
        assert "often benign" not in md


class TestRenderJson:
    def test_serializes_all_fields(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_edges=[ImportEdge(from_path="a.py", to_path="b.py", kind="import")],
            modules=[ModuleDescription(path="a.py", name="A", description="A module")],
            database_schema=[
                DBTable(name="t", columns=[DBColumn(name="c", type="INT")])
            ],
        )
        data = render_json(graph)
        assert b'"directory_tree"' in data
        assert b'"import_edges"' in data
        assert b'"modules"' in data
        assert b'"database_schema"' in data

    def test_null_database_schema_excluded(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            database_schema=None,
        )
        data = render_json(graph)
        assert b'"database_schema"' not in data

    def test_empty_lists_included(self):
        graph = CodebaseGraph(directory_tree="root/")
        data = render_json(graph)
        assert b'"import_edges": []' in data


class TestWriteOutputs:
    def test_writes_both_files(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            md_path, json_path, html_path = write_outputs(graph, Path(tmpdir))
            assert md_path.exists()
            assert json_path.exists()
            assert html_path is not None
            assert html_path.exists()
            assert md_path.name == "GRAPH.md"
            assert json_path.name == "GRAPH.json"
            assert html_path.name == "GRAPH.html"

    def test_no_html_when_disabled(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            md_path, json_path, html_path = write_outputs(
                graph, Path(tmpdir), html=False
            )
            assert md_path.exists()
            assert json_path.exists()
            assert html_path is None

    def test_working_copy_always_written(self):
        # The internal working copy exists regardless of the json flag — the diff
        # baseline and --serve depend on it.
        from graphlm.freshness import STATE_FILENAME

        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            write_outputs(graph, out, json=False, html=False, diff=False)
            assert (out / STATE_FILENAME).exists()

    def test_json_off_omits_deliverable_but_keeps_working_copy(self):
        from graphlm.freshness import STATE_FILENAME

        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            md_path, json_path, _ = write_outputs(graph, out, json=False, html=False)
            assert md_path.exists()
            assert json_path is None  # no user-facing deliverable
            assert not (out / "GRAPH.json").exists()
            assert (out / STATE_FILENAME).exists()  # but the working copy is there

    def test_json_off_omits_diff_json_but_keeps_diff_md(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            result = write_outputs(graph, out, json=False, html=False, diff=True)
            assert result.diff_md is not None and result.diff_md.exists()
            assert result.diff_json is None
            assert not (out / "GRAPH_DIFF.json").exists()

    def test_json_on_writes_deliverable_and_diff_json(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            md_path, json_path, _ = write_outputs(graph, out, json=True, html=False)
            assert json_path is not None and json_path.name == "GRAPH.json"
            assert json_path.exists()

    def test_diff_reads_working_copy_across_two_runs(self):
        # Run 1 (json off) writes only the working copy; run 2 must still diff
        # against it (proving the diff no longer depends on GRAPH.json).
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            write_outputs(
                CodebaseGraph(directory_tree="root/\n"), out, json=False, html=False
            )
            result = write_outputs(
                CodebaseGraph(
                    directory_tree="root/\n",
                    modules=[ModuleDescription(path="new.py", name="n", description="d")],
                ),
                out,
                json=True,
                html=False,
            )
            # A real prior baseline existed → the diff is NORMAL, not first-run.
            assert result.diff_json is not None
            import json as _json

            assert _json.loads(result.diff_json.read_text())["state"] == "normal"

    def _versioned(self, version, **kw):
        return CodebaseGraph(
            directory_tree="root/\n",
            meta=GraphMeta(created_at="t", graphlm_version=version),
            **kw,
        )

    def test_version_change_removes_stale_json_and_reports_state(self):
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            # Run 1: version 0.5.0, --json ON (leaves a GRAPH.json deliverable).
            write_outputs(self._versioned("0.5.0"), out, json=True, html=False)
            assert (out / "GRAPH.json").exists()
            # Run 2: version 0.6.0, --json OFF. The stale GRAPH.json must go, and
            # the diff must report version_changed (not first_run/normal).
            result = write_outputs(self._versioned("0.6.0"), out, json=False, html=False)
            assert not (out / "GRAPH.json").exists()  # stale deliverable removed
            assert (out / STATE_FILENAME).exists()  # working copy refreshed
            # diff_json is off (json=False); read the state from the md instead.
            md = result.diff_md.read_text()
            assert "different graphlm version" in md or "regenerated fresh" in md

    def test_same_version_json_toggle_removes_stale_deliverable(self):
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            # Run 1: --json ON → GRAPH.json written.
            write_outputs(self._versioned("0.6.0"), out, json=True, html=False)
            assert (out / "GRAPH.json").exists()
            # Run 2: SAME version, --json OFF → stale GRAPH.json removed even
            # though the version didn't change (the flag-toggle case).
            write_outputs(self._versioned("0.6.0"), out, json=False, html=False)
            assert not (out / "GRAPH.json").exists()

    def test_same_version_normal_diff_no_spurious_version_change(self):
        import json as _json

        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            write_outputs(self._versioned("0.6.0"), out, json=True, html=False)
            result = write_outputs(self._versioned("0.6.0"), out, json=True, html=False)
            assert _json.loads(result.diff_json.read_text())["state"] == "normal"

    def test_upgrade_bootstrap_from_legacy_graph_json(self):
        # First post-upgrade run: no working copy, but a legacy 0.5.0 GRAPH.json.
        # The diff must report version_changed (NOT first_run) and name versions.
        import json as _json

        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            (out / "GRAPH.json").write_bytes(render_json(self._versioned("0.5.0")))
            assert not (out / STATE_FILENAME).exists()
            result = write_outputs(self._versioned("0.6.0"), out, json=True, html=False)
            data = _json.loads(result.diff_json.read_text())
            assert data["state"] == "version_changed"
            assert data["old_graphlm_version"] == "0.5.0"
            assert data["new_graphlm_version"] == "0.6.0"

    def test_bootstrap_removes_legacy_json_when_json_off(self):
        # Same as above but --json off: the legacy deliverable is removed and the
        # state is read from the diff md.
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            (out / "GRAPH.json").write_bytes(render_json(self._versioned("0.5.0")))
            result = write_outputs(self._versioned("0.6.0"), out, json=False, html=False)
            assert not (out / "GRAPH.json").exists()
            assert "regenerated fresh" in result.diff_md.read_text()

    def test_bootstrap_same_version_legacy_is_normal_diff(self):
        # A source checkout (version None on both sides) must NOT be a spurious
        # version change, and a legacy GRAPH.json with the same version diffs
        # NORMAL — not FIRST_RUN (ADR-002 dec.4).
        import json as _json

        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            (out / "GRAPH.json").write_bytes(
                render_json(self._versioned("0.6.0", modules=[]))
            )
            result = write_outputs(
                self._versioned(
                    "0.6.0",
                    modules=[ModuleDescription(path="new.py", name="n", description="d")],
                ),
                out,
                json=True,
                html=False,
            )
            data = _json.loads(result.diff_json.read_text())
            assert data["state"] == "normal"
            assert data["dimensions"]["modules"]["added"] == ["`new.py`"]

    def test_bootstrap_corrupt_legacy_is_uncomparable_not_first_run(self):
        import json as _json

        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            (out / "GRAPH.json").write_bytes(b"\xff\xfe not json")
            result = write_outputs(self._versioned("0.6.0"), out, json=True, html=False)
            data = _json.loads(result.diff_json.read_text())
            assert data["state"] == "uncomparable"

    def test_symlinked_html_blocks_cleanup_before_any_delete(self):
        # A symlinked output path must be refused BEFORE cleanup runs, so a stale
        # GRAPH.json is not deleted when the write is going to abort anyway.
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            # Prior graphlm run so owns_dir is true and a stale GRAPH.json exists.
            write_outputs(self._versioned("0.6.0"), out, json=True, html=False)
            assert (out / "GRAPH.json").exists()
            canary = out / "canary"
            canary.write_text("USER")
            (out / "GRAPH.html").symlink_to(canary)
            try:
                write_outputs(self._versioned("0.6.0"), out, json=False, html=True)
                assert False, "expected ValueError for symlinked GRAPH.html"
            except ValueError as e:
                assert "symlink" in str(e)
            # Cleanup must NOT have run: the stale GRAPH.json survives.
            assert (out / "GRAPH.json").exists()
            assert canary.read_text() == "USER"

    def test_unlink_failure_still_writes_graph(self, monkeypatch):
        # If cleanup's unlink fails, the new graph + working copy are still written.
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            write_outputs(self._versioned("0.6.0"), out, json=True, html=False)
            real_unlink = Path.unlink

            def boom(self, *a, **k):
                if self.name == "GRAPH.json":
                    raise OSError("cannot remove")
                return real_unlink(self, *a, **k)

            monkeypatch.setattr(Path, "unlink", boom)
            # --json off would try to remove GRAPH.json; the failure is swallowed.
            write_outputs(self._versioned("0.6.0"), out, json=False, html=False)
            assert (out / "GRAPH.md").exists()
            assert (out / STATE_FILENAME).exists()

    def test_cleanup_skipped_in_unowned_dir(self):
        # -o into a fresh user dir with a same-named file graphlm never wrote:
        # cleanup must not fire (owns_dir is False), so the user file survives.
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            user_html = out / "GRAPH.html"
            user_html.write_text("USER'S OWN GRAPH.html")
            # First graphlm run here with --no-html: without the ownership gate
            # this would delete the user's GRAPH.html.
            write_outputs(self._versioned("0.6.0"), out, json=False, html=False)
            assert user_html.read_text() == "USER'S OWN GRAPH.html"

    def test_cleanup_runs_even_with_no_diff(self):
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            # A prior graphlm graph (pre-working-copy layout): graphlm owns
            # the dir. (A GRAPH.json that is NOT a graphlm graph does not count
            # — test_unrelated_graph_json_is_never_deleted.)
            (out / "GRAPH.json").write_bytes(render_json(self._versioned("0.6.0")))
            # --no-diff: cleanup must still remove the stale deliverable.
            write_outputs(self._versioned("0.6.0"), out, json=False, html=False, diff=False)
            assert not (out / "GRAPH.json").exists()

    def test_unrelated_graph_json_is_never_deleted(self):
        # A first run into a user directory that already holds files with
        # graphlm's names (but no graphlm state) must not delete them.
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            (out / "GRAPH.json").write_text('{"user": "data"}')
            (out / "GRAPH.html").write_text("<p>user page</p>")
            (out / "GRAPH_DIFF.md").write_text("user notes")
            write_outputs(self._versioned("0.7.1"), out, json=False, html=False, diff=False)
            assert (out / "GRAPH.json").read_text() == '{"user": "data"}'
            assert (out / "GRAPH.html").read_text() == "<p>user page</p>"
            assert (out / "GRAPH_DIFF.md").read_text() == "user notes"

    def test_plain_text_graph_json_is_not_deleted(self):
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            (out / "GRAPH.json").write_text("my own notes, not JSON")
            write_outputs(self._versioned("0.7.1"), out, json=False, html=False, diff=False)
            assert (out / "GRAPH.json").read_text() == "my own notes, not JSON"

    def test_refuses_to_write_through_graph_json_symlink(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out = tmp / "out"
            out.mkdir()
            canary = tmp / "canary.txt"
            canary.write_text("USER DATA")
            (out / "GRAPH.json").symlink_to(canary)
            try:
                write_outputs(graph, out, html=False, diff=False)
            except ValueError as e:
                assert "symlink" in str(e).lower()
            else:
                raise AssertionError("expected ValueError")
            assert canary.read_text() == "USER DATA"
            assert (out / "GRAPH.json").is_symlink()

    def test_refuses_to_write_through_output_dir_symlink(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            real = tmp / "real"
            real.mkdir()
            link = tmp / "link"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                return
            try:
                write_outputs(graph, link, html=False, diff=False)
            except ValueError as e:
                assert "symlink" in str(e).lower()
            else:
                raise AssertionError("expected ValueError")
            assert not (real / "GRAPH.md").exists()

    def test_refuses_to_write_through_ancestor_directory_symlink(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            victim = tmp / "victim"
            victim.mkdir()
            decoy = tmp / "decoy"
            try:
                decoy.symlink_to(victim)
            except (OSError, NotImplementedError):
                return
            try:
                write_outputs(
                    graph, decoy / "pwned", html=False, diff=False
                )
            except ValueError as e:
                assert "symlink" in str(e).lower()
            else:
                raise AssertionError("expected ValueError")
            assert not (victim / "pwned").exists()
            assert not (victim / "pwned" / "GRAPH.md").exists()

    def test_creates_output_directory(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "nested" / "dir"
            md_path, json_path, html_path = write_outputs(graph, out)
            assert md_path.parent == out
            assert html_path.parent == out

    def test_custom_suffixes(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            result = write_outputs(
                graph, Path(tmpdir), md_suffix="graph", json_suffix="graph"
            )
            md_path, json_path, html_path = result
            assert md_path.name == "graph.md"
            assert json_path.name == "graph.json"
            assert result.diff_md is not None
            assert result.diff_md.name == "graph_DIFF.md"
            assert result.diff_md.exists()
            assert result.diff_json is not None
            assert result.diff_json.name == "graph_DIFF.json"
            assert result.diff_json.exists()

    def test_custom_diff_suffix_overrides_json_suffix(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            result = write_outputs(
                graph,
                Path(tmpdir),
                json_suffix="graph",
                diff_suffix="custom",
            )
            assert result.diff_md is not None
            assert result.diff_md.name == "custom_DIFF.md"
            assert result.diff_md.exists()
            assert result.diff_json is not None
            assert result.diff_json.name == "custom_DIFF.json"
            assert result.diff_json.exists()

    def test_write_result_preserves_diff_paths_across_round_trips(self):
        result = WriteResult(
            Path("GRAPH.md"),
            Path("GRAPH.json"),
            Path("GRAPH.html"),
            diff_md=Path("GRAPH_DIFF.md"),
            diff_json=Path("GRAPH_DIFF.json"),
        )

        round_trips = (
            copy.copy(result),
            copy.deepcopy(result),
            pickle.loads(pickle.dumps(result)),
        )
        for round_trip in round_trips:
            assert tuple(round_trip) == tuple(result)
            assert round_trip.diff_md == result.diff_md
            assert round_trip.diff_json == result.diff_json
            assert len(round_trip) == 3
            md_path, json_path, html_path = round_trip
            assert (md_path, json_path, html_path) == tuple(result)

    def test_meta_bearing_graph_renders_html_default_path(self):
        # The CLI default is html=True, so the real path renders a meta-bearing
        # graph to HTML. The directive is GRAPH.md-only and must NOT leak in.
        graph = CodebaseGraph(
            directory_tree="root/\n",
            meta=GraphMeta(
                created_at="2026-08-30T00:00:00Z", commit_sha="a" * 40
            ),
        )
        with TemporaryDirectory() as tmpdir:
            md_path, json_path, html_path = write_outputs(graph, Path(tmpdir))
            assert html_path is not None and html_path.stat().st_size > 0
            assert "Provenance & refresh directive" not in html_path.read_text()
            assert "Provenance & refresh directive" in md_path.read_text()


_GIT_META = GraphMeta(
    created_at="2026-08-30T14:22:05Z",
    commit_sha="d38e47d21406cf6482c0272587d17d92629059be",
    graphlm_version="0.1.0",
)
_NONGIT_META = GraphMeta(
    created_at="2026-08-30T14:22:05Z", commit_sha=None, graphlm_version=None
)


class TestRefreshDirective:
    def test_git_form_present_when_sha_set(self):
        md = render_markdown(CodebaseGraph(directory_tree="root/\n", meta=_GIT_META))
        # Directive is the first line, above the heading.
        assert md.lstrip().startswith(">")
        # The graphlm version is named ahead of the commit for a human check.
        assert "generated by graphlm 0.1.0 against commit `d38e47d2`" in md
        assert "2026-08-30T14:22:05Z" in md
        assert "graphlm ." in md
        assert "git rev-parse HEAD" in md
        assert "advisory" in md.lower()

    def test_directive_omits_version_prefix_when_version_unknown(self):
        # A source checkout / library caller with no graphlm_version → the
        # "by graphlm X" prefix is dropped, wording stays clean.
        meta = GraphMeta(
            created_at="2026-08-30T14:22:05Z",
            commit_sha="d38e47d21406cf6482c0272587d17d92629059be",
            graphlm_version=None,
        )
        md = render_markdown(CodebaseGraph(directory_tree="root/\n", meta=meta))
        assert "generated against commit `d38e47d2`" in md
        assert "by graphlm" not in md

    def test_non_git_form_when_sha_none(self):
        md = render_markdown(
            CodebaseGraph(directory_tree="root/\n", meta=_NONGIT_META)
        )
        assert md.lstrip().startswith(">")
        assert "No git commit tracking" in md
        assert "whenever you believe the code has changed" in md
        assert "graphlm ." in md
        # The git-only comparison instruction must not appear.
        assert "generated against commit" not in md

    def test_no_directive_without_meta(self):
        md = render_markdown(CodebaseGraph(directory_tree="root/\n"))
        assert not md.lstrip().startswith(">")
        assert "# Codebase Graph" in md

    def test_dirty_tree_honesty_never_says_reflects(self):
        # Wording guard: "generated against", never "reflects" — a SHA-fresh
        # graph can still not match an uncommitted working tree.
        for meta in (_GIT_META, _NONGIT_META):
            md = render_markdown(CodebaseGraph(directory_tree="root/\n", meta=meta))
            assert "reflect" not in md.lower()


class TestRenderJsonMeta:
    def test_meta_serialized_with_sha(self):
        data = json.loads(
            render_json(CodebaseGraph(directory_tree="root/\n", meta=_GIT_META))
        )
        assert data["meta"]["commit_sha"] == _GIT_META.commit_sha
        assert data["meta"]["schema_version"] == 1

    def test_null_commit_sha_preserved_despite_exclude_none(self):
        # The critical case: exclude_none=True must NOT drop commit_sha=None,
        # or a non-git graph is indistinguishable from an old meta-less one.
        data = json.loads(
            render_json(CodebaseGraph(directory_tree="root/\n", meta=_NONGIT_META))
        )
        assert "meta" in data
        assert "commit_sha" in data["meta"]
        assert data["meta"]["commit_sha"] is None

    def test_no_meta_key_when_meta_absent(self):
        data = json.loads(render_json(CodebaseGraph(directory_tree="root/\n")))
        assert "meta" not in data

    def test_round_trips_through_model_validate(self):
        graph = CodebaseGraph(directory_tree="root/\n", meta=_GIT_META)
        reloaded = CodebaseGraph.model_validate_json(render_json(graph))
        assert reloaded.meta is not None
        assert reloaded.meta.commit_sha == _GIT_META.commit_sha

    def test_backward_read_of_meta_less_json(self):
        # Versioned-contract guarantee: an OLD GRAPH.json with no meta block
        # still validates, with meta defaulting to None.
        old = '{"directory_tree": "root/\\n", "modules": []}'
        graph = CodebaseGraph.model_validate_json(old)
        assert graph.meta is None


def _edge(a: str, b: str) -> ImportEdge:
    return ImportEdge(from_path=a, to_path=b, kind="import")


def _mermaid_block(md: str) -> str:
    """The text between the ```mermaid fence and its closing fence."""
    start = md.index("```mermaid\n") + len("```mermaid\n")
    return md[start:md.index("```", start)]


class TestMermaidModuleGraph:
    def test_section_present_with_ast_edges(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("pkg/a.py", "lib/b.py")],
        )
        md = render_markdown(graph)
        assert "## Module Graph" in md
        assert "parser-extracted import edges (ground truth)" in md
        block = _mermaid_block(md)
        assert block.startswith("flowchart LR")
        assert 'n_pkg["pkg"]' in block
        assert 'n_lib["lib"]' in block
        assert "n_pkg --> n_lib" in block

    def test_section_sits_right_after_directory_tree(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("pkg/a.py", "lib/b.py")],
            import_edges=[_edge("pkg/a.py", "lib/b.py")],
        )
        md = render_markdown(graph)
        assert (
            md.index("## Directory Tree")
            < md.index("## Module Graph")
            < md.index("## Import Edges")
        )

    def test_absent_when_no_edges(self):
        graph = CodebaseGraph(directory_tree="root/")
        assert render_mermaid(graph) == []
        assert "## Module Graph" not in render_markdown(graph)
        # An explicitly empty AST list with no LLM edges is also "no edges".
        empty_ast = CodebaseGraph(directory_tree="root/", deterministic_edges=[])
        assert render_mermaid(empty_ast) == []

    def test_falls_back_to_llm_edges_when_ast_is_none(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=None,
            import_edges=[_edge("pkg/a.py", "lib/b.py")],
        )
        md = render_markdown(graph)
        assert "LLM-inferred import edges" in md
        assert "ground truth" not in md
        assert "n_pkg --> n_lib" in _mermaid_block(md)

    def test_dot_slash_llm_paths_do_not_collapse_to_dot(self):
        # ./a.py rpartition("/") used to yield "." (#96).
        graph = CodebaseGraph(
            directory_tree="p/",
            deterministic_edges=None,
            import_edges=[_edge("./a.py", "./b.py"), _edge("./b.py", "./a.py")],
            import_cycles=[
                Cycle(
                    nodes=["a.py", "b.py"],
                    edges=[],
                    length=2,
                    risk_score=1.0,
                )
            ],
        )
        text = "\n".join(render_mermaid(graph))
        assert 'n_["."]' not in text
        assert 'n_a_py["a.py"]' in text
        assert 'n_b_py["b.py"]' in text
        assert "n_a_py --> n_b_py" in text

    def test_ast_edges_win_over_llm_edges(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("pkg/a.py", "lib/b.py")],
            import_edges=[_edge("pkg/a.py", "other/c.py")],
        )
        block = _mermaid_block(render_markdown(graph))
        assert "n_pkg --> n_lib" in block
        assert "n_other" not in block

    def test_collapses_files_to_directory_and_drops_self_edges(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[
                _edge("pkg/a.py", "lib/x.py"),
                _edge("pkg/b.py", "lib/y.py"),  # same dir pair -> one edge
                _edge("pkg/a.py", "pkg/b.py"),  # self-edge after collapse -> dropped
            ],
        )
        block = _mermaid_block(render_markdown(graph))
        assert block.count(" --> ") == 1
        assert "n_pkg --> n_lib" in block
        assert "n_pkg --> n_pkg" not in block
        assert block.count('["pkg"]') == 1

    def test_root_level_file_is_its_own_node(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("setup.py", "pkg/a.py")],
        )
        block = _mermaid_block(render_markdown(graph))
        assert 'n_setup_py["setup.py"]' in block
        assert "n_setup_py --> n_pkg" in block

    def test_unsafe_path_chars_yield_safe_ids_and_escaped_labels(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("my-dir/a.b.py", 'we"ird/c.py')],
        )
        block = _mermaid_block(render_markdown(graph))
        assert 'n_my_dir["my-dir"]' in block
        assert 'n_we_ird["we#quot;ird"]' in block
        assert "n_my_dir --> n_we_ird" in block
        # Every id token is [A-Za-z0-9_] only.
        for line in block.splitlines()[1:]:
            for tok in line.split():
                if tok.startswith("n_"):
                    ident = tok.split("[", 1)[0]
                    assert ident.replace("_", "a").isalnum(), ident

    def test_colliding_sanitized_ids_are_disambiguated(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("a-b/x.py", "a_b/y.py")],
        )
        block = _mermaid_block(render_markdown(graph))
        assert 'n_a_b["a-b"]' in block
        assert 'n_a_b_2["a_b"]' in block
        assert "n_a_b --> n_a_b_2" in block

    def test_max_nodes_cap_keeps_highest_degree_and_notes_the_rest(self):
        # hub has degree 5; leaves have degree 1. Cap at 3 keeps hub + 2 leaves
        # (alphabetical tie-break) and hides 3 directories.
        edges = [_edge("hub/h.py", f"leaf{i}/l.py") for i in range(5)]
        graph = CodebaseGraph(directory_tree="root/", deterministic_edges=edges)
        text = "\n".join(render_mermaid(graph, max_nodes=3))
        assert 'n_hub["hub"]' in text
        assert 'n_leaf0["leaf0"]' in text
        assert 'n_leaf1["leaf1"]' in text
        assert "leaf2" not in text and "leaf4" not in text
        assert "*… 3 more directories not shown*" in text
        assert text.count(" --> ") == 2

    def test_no_more_note_when_under_cap(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("pkg/a.py", "lib/b.py")],
        )
        assert "more directories not shown" not in render_markdown(graph)

    def test_cycle_edges_styled_with_exact_link_indices(self):
        # Four collapsed edges, two of which are cycle edges (both file-level
        # endpoints in the same SCC). Cycle links are emitted last, so with two
        # plain links at indices 0 and 1 the cycle links are indices 2 and 3.
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[
                _edge("a/x.py", "b/y.py"),
                _edge("b/y.py", "a/x.py"),
                _edge("c/z.py", "d/w.py"),
                _edge("d/w.py", "e/v.py"),
            ],
            import_cycles=[
                Cycle(
                    nodes=["a/x.py", "b/y.py"],
                    edges=[_edge("a/x.py", "b/y.py"), _edge("b/y.py", "a/x.py")],
                    length=2,
                    risk_score=1.0,
                )
            ],
        )
        md = render_markdown(graph)
        block = _mermaid_block(md)
        links = [ln.strip() for ln in block.splitlines() if " --> " in ln]
        assert links == [
            "n_c --> n_d",
            "n_d --> n_e",
            "n_a --> n_b",
            "n_b --> n_a",
        ]
        assert "linkStyle 2,3 stroke:#e11,stroke-width:2px" in block
        assert "style n_a stroke:#e11,stroke-width:2px" in block
        assert "style n_b stroke:#e11,stroke-width:2px" in block
        assert "Red edges are members of a production import cycle." in md
        assert "Red-outlined directories contain a file in an import cycle." in md
        # A production cycle carries no test-code styling.
        assert "#b8860b" not in block

    def test_single_cycle_edge_gets_index_of_last_link(self):
        # Three collapsed edges, exactly one a cycle edge -> linkStyle 2.
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[
                _edge("a/x.py", "b/y.py"),
                _edge("b/y.py", "a/z.py"),  # a/y.py is NOT in the cycle
                _edge("c/z.py", "d/w.py"),
            ],
            import_cycles=[
                Cycle(nodes=["a/x.py", "b/y.py"], edges=[], length=2, risk_score=1.0)
            ],
        )
        block = _mermaid_block(render_markdown(graph))
        links = [ln.strip() for ln in block.splitlines() if " --> " in ln]
        assert links == ["n_b --> n_a", "n_c --> n_d", "n_a --> n_b"]
        assert "linkStyle 2 stroke:#e11,stroke-width:2px" in block

    def test_intra_directory_cycle_shows_as_red_node_outline(self):
        # All cycle members in one directory: the edges collapse to a dropped
        # self-edge, so the directory outline is the only trace of the cycle.
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[
                _edge("app/a.py", "app/b.py"),
                _edge("app/b.py", "app/a.py"),
                _edge("app/a.py", "lib/c.py"),
            ],
            import_cycles=[
                Cycle(nodes=["app/a.py", "app/b.py"], edges=[], length=2, risk_score=1.0)
            ],
        )
        md = render_markdown(graph)
        block = _mermaid_block(md)
        assert "linkStyle" not in block
        assert "style n_app stroke:#e11,stroke-width:2px" in block
        assert "style n_lib" not in block
        assert "Red edges are members of an import cycle." not in md
        assert "Red-outlined directories contain a file in an import cycle." in md

    def test_test_only_cycle_gets_amber_edges_and_outline(self):
        # A cross-directory cycle among test files → amber (not red) edge + node
        # outline, and the amber legend lines.
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[
                _edge("tests/a/x.py", "tests/b/y.py"),
                _edge("tests/b/y.py", "tests/a/x.py"),
            ],
            import_cycles=[
                Cycle(
                    nodes=["tests/a/x.py", "tests/b/y.py"],
                    edges=[],
                    length=2,
                    risk_score=1.0,
                    test_only=True,
                )
            ],
        )
        md = render_markdown(graph)
        block = _mermaid_block(md)
        assert "#e11" not in block  # no red anywhere
        assert "stroke:#b8860b,stroke-width:2px" in block  # amber edges + nodes
        assert "Amber edges are members of a test-code import cycle." in md
        assert (
            "Amber-outlined directories contain only test-code import cycles "
            "(usually intentional scaffolding)." in md
        )

    def test_mixed_cycle_dir_stays_red_not_amber(self):
        # A directory touched by BOTH a production and a test-only cycle keeps
        # the red (production) outline — production wins.
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[
                _edge("app/a.py", "lib/b.py"),
                _edge("lib/b.py", "app/a.py"),
                _edge("app/t.py", "tests/c.py"),
                _edge("tests/c.py", "app/t.py"),
            ],
            import_cycles=[
                Cycle(
                    nodes=["app/a.py", "lib/b.py"], edges=[], length=2, risk_score=2.0
                ),
                Cycle(
                    nodes=["app/t.py", "tests/c.py"],
                    edges=[],
                    length=2,
                    risk_score=1.0,
                    test_only=True,
                ),
            ],
        )
        block = _mermaid_block(render_markdown(graph))
        # app/ is in both cycles → red, never amber.
        assert "style n_app stroke:#e11,stroke-width:2px" in block

    def test_single_package_project_still_renders_its_node(self):
        # Every edge collapses to a self-edge (all files in one package). The
        # node must survive — with its cycle outline — even though no edge does.
        # Caught by the cyclic_project fixture: the block came out empty.
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[
                _edge("app/a.py", "app/b.py"),
                _edge("app/b.py", "app/a.py"),
            ],
            import_cycles=[
                Cycle(nodes=["app/a.py", "app/b.py"], edges=[], length=2, risk_score=1.0)
            ],
        )
        block = _mermaid_block(render_markdown(graph))
        assert 'n_app["app"]' in block
        assert " --> " not in block
        assert "style n_app stroke:#e11,stroke-width:2px" in block

    def test_cap_keeps_cycle_member_over_equal_degree_peer(self):
        # hub->a, hub->b, hub->z: all leaves have degree 1. With z in a cycle
        # and max_nodes=2, z must beat a and b despite sorting last by name.
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[
                _edge("hub/h.py", "a/x.py"),
                _edge("hub/h.py", "b/x.py"),
                _edge("hub/h.py", "z/x.py"),
            ],
            import_cycles=[
                Cycle(nodes=["z/x.py", "z/y.py"], edges=[], length=2, risk_score=1.0)
            ],
        )
        text = "\n".join(render_mermaid(graph, max_nodes=2))
        assert 'n_z["z"]' in text
        assert 'n_a["a"]' not in text
        assert "*… 2 more directories not shown*" in text

    def test_no_cycle_legend_without_cycles(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("pkg/a.py", "lib/b.py")],
        )
        md = render_markdown(graph)
        section = md.split("## Module Graph", 1)[1]
        assert "linkStyle" not in section
        assert "style n_" not in section
        assert "import cycle" not in section

    def test_output_stable_under_shuffled_edge_order(self):
        edges = [
            _edge("a/x.py", "b/y.py"),
            _edge("b/y.py", "a/x.py"),
            _edge("c/z.py", "d/w.py"),
            _edge("a/x.py", "d/w.py"),
            _edge("e/q.py", "a/x.py"),
            _edge("a-b/x.py", "a_b/y.py"),
        ]
        cycles = [Cycle(nodes=["a/x.py", "b/y.py"], edges=[], length=2, risk_score=1.0)]
        baseline = render_mermaid(
            CodebaseGraph(
                directory_tree="root/", deterministic_edges=edges, import_cycles=cycles
            )
        )
        rng = random.Random(7)
        for _ in range(5):
            shuffled = list(edges)
            rng.shuffle(shuffled)
            got = render_mermaid(
                CodebaseGraph(
                    directory_tree="root/",
                    deterministic_edges=shuffled,
                    import_cycles=cycles,
                )
            )
            assert got == baseline
class TestRunTelemetryLine:
    """The run-telemetry blockquote (innovation #6): each half optional, the
    whole line omitted when neither is present, `n/a` for a ratio with no
    denominator."""

    @staticmethod
    def _meta(**kw):
        from graphlm.models import GraphMeta

        return GraphMeta(created_at="2026-09-02T00:00:00Z", **kw)

    def test_omitted_when_nothing_measured(self):
        md = render_markdown(CodebaseGraph(directory_tree="r/\n", meta=self._meta()))
        assert "Run telemetry" not in md

    def test_usage_only(self):
        from graphlm.models import PassUsage, RunUsage

        meta = self._meta(
            usage=RunUsage(
                pass2=PassUsage(
                    prompt_tokens=2000, completion_tokens=300, estimated_prompt_tokens=2400
                )
            )
        )
        md = render_markdown(CodebaseGraph(directory_tree="r/\n", meta=meta))
        assert (
            "> **Run telemetry.** pass 2 prompt: 2000 tokens (graphlm estimated 2400); "
            "output: 300 tokens.\n"
        ) in md
        assert "parser ground truth" not in md

    def test_usage_with_pass2_missing_is_omitted(self):
        from graphlm.models import PassUsage, RunUsage

        meta = self._meta(usage=RunUsage(pass1=PassUsage(estimated_prompt_tokens=5)))
        md = render_markdown(CodebaseGraph(directory_tree="r/\n", meta=meta))
        assert "Run telemetry" not in md

    def test_usage_without_completion_tokens_drops_output_clause(self):
        from graphlm.models import PassUsage, RunUsage

        meta = self._meta(
            usage=RunUsage(pass2=PassUsage(prompt_tokens=10, estimated_prompt_tokens=12))
        )
        md = render_markdown(CodebaseGraph(directory_tree="r/\n", meta=meta))
        assert "pass 2 prompt: 10 tokens (graphlm estimated 12)." in md
        assert "output:" not in md

    def test_faithfulness_only_with_na_ratios(self):
        from graphlm.models import Faithfulness

        meta = self._meta(
            faithfulness=Faithfulness(
                precision=None, recall=None, llm_edges=0, ast_edges=0, matched=0
            )
        )
        md = render_markdown(CodebaseGraph(directory_tree="r/\n", meta=meta))
        assert (
            "> **Run telemetry.** LLM import edges vs parser ground truth: "
            "precision n/a, recall n/a (n=0 LLM / 0 AST, 0 matched).\n"
        ) in md
        assert "pass 2 prompt" not in md

    def test_both_halves_joined(self):
        from graphlm.models import Faithfulness, PassUsage, RunUsage

        meta = self._meta(
            usage=RunUsage(
                pass2=PassUsage(prompt_tokens=None, estimated_prompt_tokens=99)
            ),
            faithfulness=Faithfulness(
                precision=0.9333, recall=0.8125, llm_edges=15, ast_edges=16, matched=14
            ),
        )
        md = render_markdown(CodebaseGraph(directory_tree="r/\n", meta=meta))
        assert (
            "> **Run telemetry.** pass 2 prompt: not reported by endpoint "
            "(graphlm estimated 99). LLM import edges vs parser ground truth: "
            "precision 0.93, recall 0.81 (n=15 LLM / 16 AST, 14 matched).\n"
        ) in md

    def test_line_sits_directly_under_directive(self):
        from graphlm.models import Faithfulness

        meta = self._meta(
            faithfulness=Faithfulness(precision=1.0, recall=1.0, llm_edges=1, ast_edges=1, matched=1)
        )
        md = render_markdown(CodebaseGraph(directory_tree="r/\n", meta=meta))
        lines = md.splitlines()
        directive_end = next(i for i, l in enumerate(lines) if "best-effort" in l)
        assert lines[directive_end + 1].startswith("> **Run telemetry.**")
        assert lines[directive_end + 2] == ""


class TestEvidenceSummary:
    """render.evidence_summary — the terse telemetry clause."""

    def _meta(self, es):
        from graphlm.models import GraphMeta

        return GraphMeta(created_at="2026-01-01T00:00:00Z", evidence_support=es)

    def test_none_when_unset(self):
        from graphlm.telemetry_render import evidence_summary

        assert evidence_summary(self._meta(None)) is None

    def test_with_low_outliers(self):
        from graphlm.models import EvidenceSupport, FileScore
        from graphlm.telemetry_render import evidence_summary

        es = EvidenceSupport(
            mean=0.81, scored=20, skipped=2,
            low=[FileScore(path="models.py", score=0.23), FileScore(path="q.py", score=0.4)],
        )
        text = evidence_summary(self._meta(es))
        assert "mean 0.81" in text
        assert "20 scored, 2 skipped" in text
        assert "models.py 0.23" in text

    def test_na_mean_when_nothing_scored(self):
        from graphlm.models import EvidenceSupport
        from graphlm.telemetry_render import evidence_summary

        es = EvidenceSupport(mean=None, scored=0, skipped=3, low=[])
        text = evidence_summary(self._meta(es))
        assert "mean n/a" in text
        assert "weakest" not in text  # no low list

    def test_in_telemetry_line(self):
        from graphlm.models import EvidenceSupport
        from graphlm.telemetry_render import render_telemetry as _render_telemetry

        es = EvidenceSupport(mean=0.9, scored=5, skipped=0, low=[])
        line = _render_telemetry(self._meta(es))
        assert line is not None and "evidence support" in line


class TestModuleImportanceRender:
    """The Modules table: fused importance when scored, byte-identical fallback."""

    def _modules_section(self, md: str) -> str:
        return md.split("## Modules")[1].split("\n##")[0].strip()

    def test_fallback_byte_identical_when_unscored(self):
        # No module has a role → the Modules section must be exactly the old
        # 3-column, path-sorted table. This golden string is the pre-feature output.
        from graphlm.models import CodebaseGraph, ModuleDescription
        from graphlm.render import render_markdown
        g = CodebaseGraph(directory_tree="t/\n", modules=[
            ModuleDescription(path="b.py", name="B", description="second"),
            ModuleDescription(path="a.py", name="A", description="first"),
        ])
        section = self._modules_section(render_markdown(g))
        assert section == (
            "| Path | Name | Description |\n"
            "|------|------|-------------|\n"
            "| `a.py` | A | first |\n"
            "| `b.py` | B | second |"
        )

    def test_scored_adds_column_and_sorts_load_bearing_first(self):
        from graphlm.models import CodebaseGraph, ModuleDescription
        from graphlm.render import render_markdown
        g = CodebaseGraph(directory_tree="t/\n", modules=[
            ModuleDescription(path="settings.py", name="S", description="constants", role=0.1, degree=3),
            ModuleDescription(path="app.py", name="App", description="orchestrator", role=2.9, degree=2),
            ModuleDescription(path="board.py", name="Board", description="core", role=2.0, degree=5),
        ])
        section = self._modules_section(render_markdown(g))
        assert section.startswith("| Importance | Path | Name | Description |")
        # board (core, degree 5) outranks app (orchestrator, degree 2) via fusion.
        rows = [ln for ln in section.splitlines() if ln.startswith("| ") and "`" in ln]
        order = [ln.split("`")[1] for ln in rows]
        assert order == ["board.py", "app.py", "settings.py"]

    def test_fusion_none_when_no_roles(self):
        from graphlm.models import ModuleDescription
        from graphlm.render import _fused_importance
        mods = [ModuleDescription(path="a.py", name="a", description="x")]
        assert _fused_importance(mods) is None

    def test_fusion_role_and_degree_combine(self):
        from graphlm.models import ModuleDescription
        from graphlm.render import _fused_importance, _ROLE_WEIGHT, _DEGREE_WEIGHT
        mods = [
            ModuleDescription(path="hi.py", name="h", description="x", role=3.0, degree=10),
            ModuleDescription(path="lo.py", name="l", description="x", role=0.0, degree=0),
        ]
        f = _fused_importance(mods)
        # top: role 3/3=1, degree rank 1 → ROLE_W*1 + DEGREE_W*1 = 1.0
        assert f["hi.py"] == _ROLE_WEIGHT + _DEGREE_WEIGHT
        assert f["lo.py"] == 0.0


class TestModuleImportanceBaseline:
    """role/degree round-trip through the diff baseline reader (additive-optional)."""

    def test_old_graph_json_without_fields_loads_normal(self, tmp_path):
        # A GRAPH.json whose modules lack role/degree must still parse and diff
        # NORMAL (the fields default None on a diffed model, like meta did).
        import json
        from graphlm.diff import load_baseline, BaselineState
        old = {
            "directory_tree": "t/\n", "import_edges": [],
            "modules": [{"path": "a.py", "name": "A", "description": "x"}],
            "data_flow": [], "test_organization": [], "architecture_notes": [],
            "file_summaries": [], "entry_points": [], "quick_reference": [],
        }
        p = tmp_path / "GRAPH.json"
        p.write_text(json.dumps(old))
        graph, state = load_baseline(p)
        assert state == BaselineState.NORMAL
        assert graph.modules[0].role is None
        assert graph.modules[0].degree is None


class TestImportanceSummary:
    def test_none_when_unscored(self):
        from graphlm.models import CodebaseGraph, ModuleDescription
        from graphlm.render import importance_summary
        g = CodebaseGraph(directory_tree="t/", modules=[
            ModuleDescription(path="a.py", name="a", description="x")])
        assert importance_summary(g) is None

    def test_names_top_load_bearing(self):
        from graphlm.models import CodebaseGraph, ModuleDescription
        from graphlm.render import importance_summary
        g = CodebaseGraph(directory_tree="t/", modules=[
            ModuleDescription(path="app.py", name="a", description="x", role=2.9, degree=2),
            ModuleDescription(path="leaf.py", name="l", description="x", role=0.1, degree=1)])
        s = importance_summary(g)
        assert "app.py" in s and s.index("app.py") < s.index("leaf.py")


class TestImportanceMixedScoring:
    """Partial Jev response: some modules scored, some not (reachable when Jev
    omits a module from its answer). Scored branch renders, unscored get an
    em-dash and sort last."""

    def test_mixed_scored_and_unscored(self):
        from graphlm.models import CodebaseGraph, ModuleDescription
        from graphlm.render import render_markdown, _fused_importance
        g = CodebaseGraph(directory_tree="t/", modules=[
            ModuleDescription(path="unscored.py", name="u", description="y"),
            ModuleDescription(path="scored.py", name="s", description="x", role=2.5, degree=3),
        ])
        # fusion covers only the scored module
        assert set(_fused_importance(g.modules)) == {"scored.py"}
        section = render_markdown(g).split("## Modules")[1].split("\n##")[0]
        assert "| Importance |" in section  # scored branch taken
        rows = [ln for ln in section.splitlines() if "`" in ln]
        order = [ln.split("`")[1] for ln in rows]
        assert order == ["scored.py", "unscored.py"]  # unscored sorts last
        assert "| — | `unscored.py`" in section  # em-dash cell


class TestFileImportanceRender:
    """meta.file_importance renders a File Importance section (directory-granular
    repos); file-granular repos (no field) are byte-identical to before."""

    def test_section_present_when_file_importance_set(self):
        from graphlm.models import CodebaseGraph, GraphMeta, FileImportance, ModuleDescription
        from graphlm.render import render_markdown
        g = CodebaseGraph(
            directory_tree="t/",
            modules=[ModuleDescription(path="src/pkg", name="pkg", description="package")],
            meta=GraphMeta(created_at="x", file_importance=[
                FileImportance(path="src/pkg/main.py", role=2.9, degree=5, fused=0.95),
                FileImportance(path="src/pkg/const.py", role=0.1, degree=8, fused=0.35),
            ]),
        )
        md = render_markdown(g)
        assert "## File Importance" in md
        sec = md.split("## File Importance")[1].split("\n##")[0]
        rows = [ln for ln in sec.splitlines() if ln.startswith("| 0.")]
        # load-bearing first
        assert rows[0].split("`")[1] == "src/pkg/main.py"
        assert rows[1].split("`")[1] == "src/pkg/const.py"

    def test_no_section_and_modules_unchanged_when_absent(self):
        from graphlm.models import CodebaseGraph, ModuleDescription
        from graphlm.render import render_markdown
        g = CodebaseGraph(
            directory_tree="t/",
            modules=[ModuleDescription(path="b.py", name="B", description="second"),
                     ModuleDescription(path="a.py", name="A", description="first")],
        )
        md = render_markdown(g)
        assert "## File Importance" not in md
        # Modules section byte-identical to the pre-feature 3-column, path-sorted form.
        sec = md.split("## Modules")[1].split("\n##")[0].strip()
        assert sec == (
            "| Path | Name | Description |\n"
            "|------|------|-------------|\n"
            "| `a.py` | A | first |\n"
            "| `b.py` | B | second |"
        )

    def test_summary_prefers_file_importance(self):
        from graphlm.models import CodebaseGraph, GraphMeta, FileImportance, ModuleDescription
        from graphlm.render import importance_summary
        g = CodebaseGraph(
            directory_tree="t/",
            modules=[ModuleDescription(path="src/pkg", name="pkg", description="x")],
            meta=GraphMeta(created_at="x", file_importance=[
                FileImportance(path="src/pkg/main.py", role=2.9, degree=3, fused=0.95)]),
        )
        s = importance_summary(g)
        assert "src/pkg/main.py" in s
