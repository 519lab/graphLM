"""Tests for the html_render module."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from graphlm.cycles import compute_sloc_map, detect_cycles
from graphlm.html_render import render_html
from graphlm.view import build_links, build_nodes, directory_color
from graphlm.models import (
    CodebaseGraph,
    Cycle,
    FileSummary,
    ImportEdge,
    ModuleDescription,
    Symbol,
)
from graphlm.parser import build_dependency_graph
from graphlm.render import write_outputs
from graphlm.scanner import scan_project


def _embedded(html: str) -> dict:
    """Parse the JSON graphlm embeds as ``const graphData = ...;``."""
    start = html.index("const graphData = ") + len("const graphData = ")
    return json.loads(html[start:html.index(";\nconst _PALETTE")])


class TestDirectoryColor:
    def test_returns_hex_color(self):
        color = directory_color("app")
        assert color.startswith("#")
        assert len(color) == 7

    def test_deterministic(self):
        assert directory_color("app") == directory_color("app")

    def test_different_dirs_different_colors(self):
        # Use long, distinct directory names that hash to different colors
        assert directory_color("application_directory") != directory_color("testing_directory")

    def test_consistent_across_calls(self):
        results = [directory_color("lib") for _ in range(10)]
        assert len(set(results)) == 1


class TestBuildNodes:
    def test_empty_graph_no_nodes(self):
        graph = CodebaseGraph(directory_tree="root/")
        nodes = build_nodes(graph)
        assert nodes == []

    def test_modules_become_nodes(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[
                ModuleDescription(path="app/__init__.py", name="App", description="App factory"),
                ModuleDescription(path="app/main.py", name="main", description="Entry point"),
            ],
        )
        nodes = build_nodes(graph)
        assert len(nodes) == 2
        assert nodes[0]["type"] == "module"
        assert nodes[0]["r"] == 8
        assert nodes[0]["name"] == "App"

    def test_entry_points_become_nodes(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            entry_points=[
                {
                    "path": "app/main.py",
                    "name": "main",
                    "kind": "main",
                    "description": "CLI entry point",
                }
            ],
        )
        nodes = build_nodes(graph)
        assert len(nodes) == 1
        assert nodes[0]["type"] == "entry_point"
        assert nodes[0]["r"] == 12

    def test_file_summaries_become_nodes(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            file_summaries=[
                FileSummary(path="app/main.py", summary="The main module"),
            ],
        )
        nodes = build_nodes(graph)
        assert len(nodes) == 1
        assert nodes[0]["type"] == "file_summary"
        assert nodes[0]["r"] == 5

    def test_all_three_types_together(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[ModuleDescription(path="a.py", name="A", description="A")],
            entry_points=[{"path": "b.py", "name": "b", "kind": "main", "description": "b"}],
            file_summaries=[FileSummary(path="c.py", summary="C")],
        )
        nodes = build_nodes(graph)
        assert len(nodes) == 3
        types = {n["type"] for n in nodes}
        assert types == {"module", "entry_point", "file_summary"}

    def test_nodes_have_color(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[ModuleDescription(path="app/main.py", name="A", description="A")],
        )
        nodes = build_nodes(graph)
        assert nodes[0]["color"].startswith("#")

    def test_nodes_have_description(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[ModuleDescription(path="a.py", name="A", description="Description text")],
        )
        nodes = build_nodes(graph)
        assert nodes[0]["description"] == "Description text"

    def test_nodes_have_path(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[ModuleDescription(path="lib/utils.py", name="utils", description="U")],
        )
        nodes = build_nodes(graph)
        assert nodes[0]["path"] == "lib/utils.py"

    def test_duplicate_path_collapsed_to_one_node(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[ModuleDescription(path="app/main.py", name="App", description="Factory")],
            entry_points=[
                {
                    "path": "app/main.py",
                    "name": "main",
                    "kind": "main",
                    "description": "CLI",
                }
            ],
            file_summaries=[FileSummary(path="app/main.py", summary="The main module")],
        )
        nodes = build_nodes(graph)
        assert len(nodes) == 1
        assert nodes[0]["type"] == "entry_point"
        assert nodes[0]["r"] == 12
        assert nodes[0]["id"] == "app/main.py"

    def test_import_edge_endpoints_become_nodes(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_edges=[
                ImportEdge(from_path="tests/test_cli.py", to_path="graphlm/cli.py", kind="import"),
            ],
        )
        nodes = build_nodes(graph)
        ids = {n["id"] for n in nodes}
        assert ids == {"tests/test_cli.py", "graphlm/cli.py"}

    def test_data_flow_endpoints_become_nodes(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            data_flow=[{"source": "CLI (cli.py)", "destination": "Library API", "description": "calls"}],
        )
        nodes = build_nodes(graph)
        ids = {n["id"] for n in nodes}
        assert ids == {"CLI (cli.py)", "Library API"}


class TestBuildLinks:
    def test_import_edges_become_links(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_edges=[
                ImportEdge(from_path="app/main.py", to_path="app/routes.py", kind="import"),
            ],
        )
        links = build_links(graph)
        assert len(links) == 1
        assert links[0]["type"] == "import"
        assert links[0]["source"] == "app/main.py"
        assert links[0]["target"] == "app/routes.py"
        assert links[0]["dash"] is None

    def test_data_flow_become_links(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            data_flow=[
                {"source": "API", "destination": "DB", "description": "Queries"},
            ],
        )
        links = build_links(graph)
        assert len(links) == 1
        assert links[0]["type"] == "data_flow"
        assert links[0]["dash"] == "5,5"

    def test_empty_graph_no_links(self):
        graph = CodebaseGraph(directory_tree="root/")
        links = build_links(graph)
        assert links == []

    def test_multiple_edges(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_edges=[
                ImportEdge(from_path="a.py", to_path="b.py", kind="import"),
                ImportEdge(from_path="b.py", to_path="c.py", kind="from"),
            ],
            data_flow=[
                {"source": "X", "destination": "Y", "description": "Data"},
            ],
        )
        links = build_links(graph)
        assert len(links) == 3


class TestRenderHtml:
    def test_produces_valid_html(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "</head>" in html
        assert "</body>" in html

    def test_embeds_d3_cdn(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert "d3.v7.min.js" in html

    def test_embeds_graph_data(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[
                ModuleDescription(path="app/main.py", name="Main", description="Entry point"),
            ],
        )
        html = render_html(graph)
        data = _embedded(html)
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["name"] == "Main"

    def test_embedded_json_does_not_break_out_of_script_tag(self):
        # json.dumps does not escape <; a path containing </script> used to
        # close the inline script tag. The payload must round-trip and the
        # document must still have exactly the two intended <script> tags
        # (D3 CDN + inline).
        payload = "foo</script><script>alert(1)</script>.py"
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[
                ModuleDescription(path=payload, name="x", description="d"),
            ],
        )
        html = render_html(graph)
        assert payload not in html
        assert html.count("<script") == 2
        data = _embedded(html)
        assert data["nodes"][0]["path"] == payload

    def test_modules_included_as_nodes(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[
                ModuleDescription(path="app/__init__.py", name="App", description="Factory"),
                ModuleDescription(path="app/main.py", name="main", description="Entry"),
            ],
        )
        html = render_html(graph)
        data = json.loads(
            html[html.index("const graphData = ") + len("const graphData = "):html.index(";\nconst _PALETTE")]
        )
        types = [n["type"] for n in data["nodes"]]
        assert "module" in types
        assert len([n for n in data["nodes"] if n["type"] == "module"]) == 2

    def test_entry_points_included(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            entry_points=[{"path": "app/main.py", "name": "main", "kind": "main", "description": "CLI"}],
        )
        html = render_html(graph)
        data = json.loads(
            html[html.index("const graphData = ") + len("const graphData = "):html.index(";\nconst _PALETTE")]
        )
        assert any(n["type"] == "entry_point" for n in data["nodes"])

    def test_import_edges_become_links(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            import_edges=[
                ImportEdge(from_path="a.py", to_path="b.py", kind="import"),
            ],
        )
        html = render_html(graph)
        data = json.loads(
            html[html.index("const graphData = ") + len("const graphData = "):html.index(";\nconst _PALETTE")]
        )
        assert len(data["links"]) == 1
        assert data["links"][0]["type"] == "import"

    def test_empty_graph_no_nodes(self):
        graph = CodebaseGraph(directory_tree="root/")
        html = render_html(graph)
        data = json.loads(
            html[html.index("const graphData = ") + len("const graphData = "):html.index(";\nconst _PALETTE")]
        )
        assert data["nodes"] == []
        assert data["links"] == []

    def test_empty_graph_still_valid_html(self):
        graph = CodebaseGraph(directory_tree="root/")
        html = render_html(graph)
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "const graphData" in html

    def test_large_graph_no_errors(self):
        modules = []
        for i in range(150):
            modules.append(
                ModuleDescription(
                    path=f"pkg{i}/mod{i}.py",
                    name=f"Module{i}",
                    description=f"Module {i} description",
                )
            )
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=modules,
        )
        html = render_html(graph)
        data = json.loads(
            html[html.index("const graphData = ") + len("const graphData = "):html.index(";\nconst _PALETTE")]
        )
        assert len(data["nodes"]) == 150

    def test_html_contains_force_simulation(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert "forceSimulation" in html

    def test_html_contains_search_box(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert "Search nodes" in html

    def test_html_contains_theme_toggle(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert "Toggle Theme" in html

    def test_html_contains_legend(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert "Legend" in html

    def test_html_contains_tooltip(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert "tooltip" in html

    def test_html_contains_zoom(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert "d3.zoom" in html

    def test_html_contains_tick_handler(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert "simulation.on('tick'" in html
        assert "translate(" in html

    def test_scale_ordinal_does_not_pass_null_domain(self):
        """D3 v7 iterates the domain; scaleOrdinal(null, range) throws 'e is not iterable'."""
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        assert "scaleOrdinal(null" not in html
        assert "scaleOrdinal(_PALETTE)" in html

    def test_palette_placeholder_in_graph_data_does_not_corrupt_html(self):
        """A node path `{_PALETTE}` must not steal the template replace (#141)."""
        graph = CodebaseGraph(
            directory_tree=".",
            modules=[
                ModuleDescription(
                    path="{_PALETTE}", name="{_PALETTE}", description="x"
                )
            ],
        )
        html = render_html(graph)
        assert "const _PALETTE = {_PALETTE}" not in html
        data = json.loads(
            html[html.index("const graphData = ") + len("const graphData = "):html.index(";\nconst _PALETTE")]
        )
        paths = {n["path"] for n in data["nodes"]}
        assert "{_PALETTE}" in paths

    def test_rendered_links_all_resolve_to_nodes(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[ModuleDescription(path="graphlm/cli.py", name="cli", description="CLI")],
            import_edges=[
                ImportEdge(from_path="tests/test_cli.py", to_path="graphlm/cli.py", kind="import"),
            ],
            data_flow=[
                {"source": "CLI (cli.py)", "destination": "Library API", "description": "invokes"},
            ],
        )
        html = render_html(graph)
        data = json.loads(
            html[html.index("const graphData = ") + len("const graphData = "):html.index(";\nconst _PALETTE")]
        )
        ids = {n["id"] for n in data["nodes"]} | {n["path"] for n in data["nodes"]}
        assert data["links"]
        for link in data["links"]:
            assert link["source"] in ids
            assert link["target"] in ids

    def test_file_summaries_with_symbols(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            file_summaries=[
                FileSummary(
                    path="app/models.py",
                    summary="Data models",
                    symbols=[
                        Symbol(name="User", kind="class", description="User model"),
                    ],
                ),
            ],
        )
        html = render_html(graph)
        data = json.loads(
            html[html.index("const graphData = ") + len("const graphData = "):html.index(";\nconst _PALETTE")]
        )
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["type"] == "file_summary"
        assert "Data models" in data["nodes"][0]["description"]

    def test_domcontentloaded_calls_initgraph(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        marker = "window.addEventListener('DOMContentLoaded'"
        assert marker in html
        after_load = html.split(marker, 1)[1]
        assert "initGraph()" in after_load

    def test_initgraph_does_not_call_itself(self):
        graph = CodebaseGraph(directory_tree="test/")
        html = render_html(graph)
        after_def = html.split("function initGraph()", 1)[1]
        load_markers = (
            "window.addEventListener('DOMContentLoaded'",
            "DOMContentLoaded",
        )
        cut = len(after_def)
        for marker in load_markers:
            idx = after_def.find(marker)
            if idx != -1:
                cut = min(cut, idx)
        body = after_def[:cut]
        assert "initGraph()" not in body
        after_load = after_def[cut:]
        assert "DOMContentLoaded" in after_load
        assert "initGraph()" in after_load


class TestWriteOutputsWithHtml:
    def test_writes_html_by_default(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            md_path, json_path, html_path = write_outputs(graph, Path(tmpdir))
            assert md_path.exists()
            assert json_path.exists()
            assert html_path is not None
            assert html_path.exists()
            assert html_path.name == "GRAPH.html"
            assert html_path.read_text(encoding="utf-8").startswith("<!DOCTYPE")

    def test_no_html_when_disabled(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            _, _, html_path = write_outputs(graph, Path(tmpdir), html=False)
            assert html_path is None

    def test_custom_html_suffix(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            _, _, html_path = write_outputs(
                graph, Path(tmpdir), html=True, html_suffix="viz"
            )
            assert html_path is not None
            assert html_path.name == "viz.html"

    def test_creates_output_directory(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "nested" / "dir"
            _, _, html_path = write_outputs(graph, out)
            assert html_path.parent == out

    def test_html_contains_graph_data(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[
                ModuleDescription(path="app/main.py", name="Main", description="Entry"),
            ],
        )
        with TemporaryDirectory() as tmpdir:
            _, _, html_path = write_outputs(graph, Path(tmpdir))
            html = html_path.read_text(encoding="utf-8")
            assert "Main" in html
            assert "Entry" in html
            assert "app/main.py" in html

    def test_html_is_self_contained(self):
        graph = CodebaseGraph(directory_tree="root/\n")
        with TemporaryDirectory() as tmpdir:
            _, _, html_path = write_outputs(graph, Path(tmpdir))
            html = html_path.read_text(encoding="utf-8")
            assert "<style>" in html
            assert "d3js.org" in html


def _edge(a: str, b: str, kind: str = "import") -> ImportEdge:
    return ImportEdge(from_path=a, to_path=b, kind=kind)


class TestGroundTruthLinks:
    def test_ast_edges_become_ast_links(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("a.py", "b.py")],
        )
        links = build_links(graph)
        assert len(links) == 1
        assert links[0]["type"] == "ast"
        assert links[0]["source"] == "a.py"
        assert links[0]["target"] == "b.py"
        assert links[0]["stroke"] == "#7aa2f7"
        assert links[0]["dash"] is None
        assert links[0]["corroborated"] is False

    def test_duplicate_llm_edge_dropped_and_ast_link_corroborated(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("a.py", "b.py")],
            import_edges=[_edge("a.py", "b.py", kind="from")],
        )
        links = build_links(graph)
        assert len(links) == 1
        assert links[0]["type"] == "ast"
        assert links[0]["corroborated"] is True

    def test_llm_only_edge_kept_as_import_link(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("a.py", "b.py")],
            import_edges=[_edge("a.py", "b.py"), _edge("a.py", "c.py")],
        )
        links = build_links(graph)
        by_type = {(l["type"], l["source"], l["target"]) for l in links}
        assert by_type == {("ast", "a.py", "b.py"), ("import", "a.py", "c.py")}
        llm = next(l for l in links if l["type"] == "import")
        assert llm["stroke"] == "#888"
        assert "corroborated" not in llm

    def test_reverse_direction_is_not_a_duplicate(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("a.py", "b.py")],
            import_edges=[_edge("b.py", "a.py")],
        )
        links = build_links(graph)
        assert {l["type"] for l in links} == {"ast", "import"}
        assert next(l for l in links if l["type"] == "ast")["corroborated"] is False

    def test_repeated_ast_pair_emitted_once(self):
        # The parser can produce one edge per import statement; two lines
        # between the same pair would read as two relationships.
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("a.py", "b.py"), _edge("a.py", "b.py", kind="from")],
        )
        assert len(build_links(graph)) == 1

    def test_ast_none_leaves_import_links_unchanged(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=None,
            import_edges=[_edge("a.py", "b.py")],
        )
        links = build_links(graph)
        assert len(links) == 1
        assert links[0]["type"] == "import"

    def test_data_flow_links_untouched(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("a.py", "b.py")],
            data_flow=[{"source": "a.py", "destination": "b.py", "description": "d"}],
        )
        links = build_links(graph)
        assert [l["type"] for l in links] == ["ast", "data_flow"]


class TestGroundTruthNodes:
    def test_ast_only_endpoints_become_nodes(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("pkg/a.py", "pkg/b.py")],
        )
        nodes = build_nodes(graph)
        assert {n["id"] for n in nodes} == {"pkg/a.py", "pkg/b.py"}
        assert all(n["type"] == "file" for n in nodes)

    def test_in_cycle_flag_set_from_import_cycles(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[ModuleDescription(path="c.py", name="C", description="not in cycle")],
            deterministic_edges=[_edge("a.py", "b.py"), _edge("b.py", "a.py"), _edge("a.py", "c.py")],
            import_cycles=[Cycle(nodes=["a.py", "b.py"], edges=[], length=2, risk_score=1.0)],
        )
        flags = {n["id"]: n["in_cycle"] for n in build_nodes(graph)}
        assert flags == {"a.py": True, "b.py": True, "c.py": False}

    def test_is_test_flag_set_from_path(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[
                ModuleDescription(path="app/core.py", name="core", description="prod"),
            ],
            file_summaries=[
                FileSummary(path="tests/test_core.py", summary="a test"),
            ],
        )
        flags = {n["id"]: n["is_test"] for n in build_nodes(graph)}
        assert flags["tests/test_core.py"] is True
        assert flags["app/core.py"] is False

    def test_in_cycle_false_everywhere_without_cycles(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[ModuleDescription(path="a.py", name="A", description="A")],
            import_edges=[_edge("a.py", "b.py")],
        )
        nodes = build_nodes(graph)
        assert nodes and all(n["in_cycle"] is False for n in nodes)

    def test_existing_module_node_gains_in_cycle_flag(self):
        # A cycle member that is already a module keeps its module identity
        # and only gains the flag.
        graph = CodebaseGraph(
            directory_tree="root/",
            modules=[ModuleDescription(path="a.py", name="A", description="A")],
            import_cycles=[Cycle(nodes=["a.py", "b.py"], edges=[], length=2, risk_score=1.0)],
        )
        nodes = build_nodes(graph)
        assert len(nodes) == 1
        assert nodes[0]["type"] == "module"
        assert nodes[0]["in_cycle"] is True

    def test_dot_slash_llm_paths_still_flag_in_cycle(self):
        # detect_cycles emits normalised nodes; LLM edges keep ./ (#96).
        graph = CodebaseGraph(
            directory_tree="p/",
            import_edges=[_edge("./a.py", "./b.py"), _edge("./b.py", "./a.py")],
            import_cycles=[
                Cycle(nodes=["a.py", "b.py"], edges=[], length=2, risk_score=1.0)
            ],
            deterministic_edges=None,
        )
        nodes = build_nodes(graph)
        flags = {n["id"]: n["in_cycle"] for n in nodes}
        assert flags == {"a.py": True, "b.py": True}
        links = build_links(graph)
        assert {(lk["source"], lk["target"]) for lk in links} == {
            ("a.py", "b.py"),
            ("b.py", "a.py"),
        }


class TestGroundTruthHtml:
    def test_template_has_three_layer_toggles(self):
        html = render_html(CodebaseGraph(directory_tree="root/"))
        assert 'data-layer="ast"' in html
        assert 'data-layer="import"' in html
        assert 'data-layer="data_flow"' in html
        assert "Parser imports" in html
        assert "LLM imports" in html
        assert "Data flow" in html

    def test_rendered_html_embeds_ast_links_and_cycle_flags(self):
        graph = CodebaseGraph(
            directory_tree="root/",
            deterministic_edges=[_edge("a.py", "b.py"), _edge("b.py", "a.py")],
            import_edges=[_edge("a.py", "b.py")],
            import_cycles=[Cycle(nodes=["a.py", "b.py"], edges=[], length=2, risk_score=1.0)],
        )
        data = _embedded(render_html(graph))
        assert [l["type"] for l in data["links"]] == ["ast", "ast"]
        assert data["links"][0]["corroborated"] is True
        assert data["links"][1]["corroborated"] is False
        assert all(n["in_cycle"] for n in data["nodes"])
        assert data["cycles"] == 1

    def test_cycles_count_zero_when_no_cycles(self):
        data = _embedded(render_html(CodebaseGraph(directory_tree="root/")))
        assert data["cycles"] == 0

    def test_template_draws_cycle_ring_and_legend(self):
        html = render_html(CodebaseGraph(directory_tree="root/"))
        assert "const CYCLE_COLOR = '#e11'" in html
        assert "in_cycle" in html
        assert "In Import Cycle" in html
        assert "Parser Import (AST)" in html
        assert "LLM Import" in html
        assert "border-color:#7aa2f7" in html

    def test_stats_line_reports_parser_edges_and_cycles(self):
        html = render_html(CodebaseGraph(directory_tree="root/"))
        after_load = html.split("window.addEventListener('DOMContentLoaded'", 1)[1]
        assert "count('ast')" in after_load
        assert "graphData.cycles" in after_load
        assert "import cycle" in after_load

    def test_layer_toggle_uses_display_not_simulation(self):
        html = render_html(CodebaseGraph(directory_tree="root/"))
        toggle_block = html.split("const layerVisible", 1)[1].split("buildLegend()", 1)[0]
        assert "link.style('display'" in toggle_block
        assert "simulation" not in toggle_block


FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestGroundTruthEndToEnd:
    def test_cyclic_project_renders_red_in_both_outputs(self):
        project = FIXTURES_DIR / "cyclic_project"
        scan = scan_project(project)
        edges = build_dependency_graph(scan.file_fragments, project_dir=project)
        cycles = detect_cycles(edges, compute_sloc_map(scan.file_fragments))
        assert edges and cycles, "fixture must yield AST edges and a cycle"

        graph = CodebaseGraph(
            directory_tree=scan.tree,
            deterministic_edges=edges,
            import_cycles=cycles,
        )
        with TemporaryDirectory() as tmpdir:
            md_path, _, html_path = write_outputs(graph, Path(tmpdir))
            md = md_path.read_text(encoding="utf-8")
            html = html_path.read_text(encoding="utf-8")

        # GRAPH.md: a Mermaid block from the parser edges. The fixture's cycle
        # is entirely inside app/, so it surfaces as the red directory outline.
        assert "## Module Graph" in md
        assert "parser-extracted import edges (ground truth)" in md
        assert "```mermaid" in md
        assert 'n_app["app"]' in md
        assert "style n_app stroke:#e11,stroke-width:2px" in md
        assert "Red-outlined directories contain a file in an import cycle." in md

        # GRAPH.html: AST links, and every cycle member ringed red.
        data = _embedded(html)
        ast_links = [l for l in data["links"] if l["type"] == "ast"]
        assert len(ast_links) == len({(e.from_path, e.to_path) for e in edges})
        cycle_members = {n for c in cycles for n in c.nodes}
        flagged = {n["id"] for n in data["nodes"] if n["in_cycle"]}
        assert flagged == cycle_members
        assert data["cycles"] == len(cycles)
        assert "#e11" in html
