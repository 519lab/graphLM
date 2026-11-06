"""Host-injected LLM backends, structure-only maps, and env isolation (0.7.0)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from graphlm import LLMReply, generate_graph
from graphlm import config as graphlm_config
from graphlm.backend import complete
from graphlm.llm import CodebaseGraph, GraphLLErrorParse
from graphlm.view import graph_view

FIXTURES = Path(__file__).parent / "fixtures"


def _graph_json(**overrides: object) -> str:
    base: dict[str, object] = {
        "directory_tree": "",
        "import_edges": [],
        "modules": [{"name": "main", "path": "main.py", "description": "entry"}],
        "data_flow": [],
        "database_schema": None,
        "test_organization": [],
        "architecture_notes": [{"note": "from backend"}],
        "quick_reference": [],
    }
    base.update(overrides)
    return json.dumps(base)


class FakeBackend:
    """Records every request; replies pass 1 then pass 2 in order."""

    def __init__(self, replies: list[LLMReply]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []

    def __call__(self, system_prompt, user_prompt, *, json_schema, schema_name,
                 timeout, max_output_tokens):
        self.calls.append({
            "system": system_prompt, "user": user_prompt, "schema": json_schema,
            "schema_name": schema_name, "timeout": timeout,
            "max_output_tokens": max_output_tokens,
        })
        return self.replies.pop(0)


@pytest.fixture
def no_user_env(monkeypatch):
    """Fail the test if graphlm tries to read its user .env."""
    def _boom() -> None:
        raise AssertionError("graphlm read ~/.config/graphlm/.env")
    monkeypatch.setattr(graphlm_config, "_load_env_files", _boom)


class TestComplete:
    def test_structured_reply_is_validated_into_the_model(self):
        backend = FakeBackend([LLMReply(_graph_json())])
        result = complete(backend, "sys", "user", response_format=CodebaseGraph,
                          timeout=12.0, max_output_tokens=345)
        assert isinstance(result, CodebaseGraph)
        assert result.modules[0].path == "main.py"
        call = backend.calls[0]
        assert call["schema"] == CodebaseGraph.model_json_schema()
        assert call["schema_name"] == "CodebaseGraph"
        assert (call["timeout"], call["max_output_tokens"]) == (12.0, 345)

    def test_free_form_reply_returns_extracted_json_text(self):
        backend = FakeBackend([LLMReply('Sure:\n```json\n{"requested_files": ["a.py"]}\n```')])
        result = complete(backend, "s", "u", timeout=1.0, max_output_tokens=1)
        assert json.loads(result) == {"requested_files": ["a.py"]}
        assert backend.calls[0]["schema"] is None
        assert backend.calls[0]["schema_name"] is None

    def test_usage_is_forwarded_only_when_reported(self):
        seen: list[dict[str, object]] = []
        usage = {"prompt_tokens": 10, "completion_tokens": 2}
        complete(FakeBackend([LLMReply("{}", usage)]), "s", "u", timeout=1,
                 max_output_tokens=1, on_usage=seen.append)
        complete(FakeBackend([LLMReply("{}")]), "s", "u", timeout=1,
                 max_output_tokens=1, on_usage=seen.append)
        assert seen == [usage]

    def test_wrong_return_type_is_a_parse_error(self):
        backend = FakeBackend(["{}"])  # type: ignore[list-item]
        with pytest.raises(GraphLLErrorParse, match="expected LLMReply"):
            complete(backend, "s", "u", timeout=1, max_output_tokens=1)

    def test_invalid_structured_reply_fails_like_the_http_path(self):
        backend = FakeBackend([LLMReply('{"modules": "not a list"}')])
        with pytest.raises(GraphLLErrorParse, match="did not match expected schema"):
            complete(backend, "s", "u", response_format=CodebaseGraph,
                     timeout=1, max_output_tokens=1)

    def test_empty_reply_is_a_parse_error(self):
        with pytest.raises(GraphLLErrorParse, match="empty response"):
            complete(FakeBackend([LLMReply("")]), "s", "u", timeout=1,
                     max_output_tokens=1)


class TestGenerateGraphWithBackend:
    def test_both_passes_go_through_the_backend_not_http(self, httpx_mock, no_user_env):
        # httpx_mock with no registered responses: any HTTP request fails the test.
        backend = FakeBackend([
            LLMReply('{"requested_files": ["main.py"]}',
                     {"prompt_tokens": 100, "completion_tokens": 5}),
            LLMReply(_graph_json(), {"prompt_tokens": 900, "completion_tokens": 50}),
        ])
        result = generate_graph(FIXTURES / "small_project", llm=backend, timeout=33,
                                max_output_tokens=4000, include_evidence=False,
                                include_importance=False)
        assert len(backend.calls) == 2
        assert backend.calls[0]["schema"] is None
        assert backend.calls[1]["schema"] == CodebaseGraph.model_json_schema()
        assert all(c["timeout"] == 33 for c in backend.calls)
        assert all(c["max_output_tokens"] == 4000 for c in backend.calls)
        assert result.graph.modules[0].path == "main.py"
        assert result.graph.directory_tree  # filled locally, as with HTTP
        usage = result.graph.meta.usage
        assert usage.pass1.prompt_tokens == 100
        assert usage.pass2.completion_tokens == 50
        assert httpx_mock.get_requests() == []

    def test_endpoint_settings_are_not_required(self, monkeypatch, no_user_env):
        for var in ("GRAPHLM_BASE_URL", "GRAPHLM_API_KEY", "GRAPHLM_MODEL"):
            monkeypatch.delenv(var, raising=False)
        backend = FakeBackend([LLMReply('{"requested_files": []}'),
                               LLMReply(_graph_json())])
        generate_graph(FIXTURES / "small_project", llm=backend,
                       include_evidence=False, include_importance=False)
        assert len(backend.calls) == 2

    def test_without_backend_graphlm_still_reads_its_user_env(self, monkeypatch):
        loaded: list[bool] = []
        monkeypatch.setattr(graphlm_config, "_load_env_files", lambda: loaded.append(True))
        generate_graph(FIXTURES / "small_project", dry_run=True)
        assert loaded == [True]


class TestStructureOnly:
    def test_cycles_and_edges_without_any_llm(self, httpx_mock, no_user_env, tmp_path):
        result = generate_graph(FIXTURES / "cyclic_project", structure_only=True,
                                output_dir=tmp_path, include_html=False)
        graph = result.graph
        assert graph.deterministic_edges, "AST edges expected for the fixture"
        assert graph.import_cycles, "cyclic_project has an import cycle"
        assert graph.architecture_notes == []  # a real map, not a DRY RUN note
        assert graph.modules == [] and graph.file_summaries == []
        assert graph.meta is not None and graph.meta.usage is None
        assert (result.pass1_context_tokens, result.files_analyzed) == (0, 0)
        assert (tmp_path / "GRAPH.md").is_file()
        assert not (tmp_path / "GRAPH.html").exists()
        assert httpx_mock.get_requests() == []

    def test_does_not_write_without_output_dir(self, tmp_path, no_user_env, monkeypatch):
        monkeypatch.chdir(tmp_path)
        generate_graph(FIXTURES / "small_project", structure_only=True)
        assert list(tmp_path.iterdir()) == []

    def test_cycles_respect_threshold_and_toggle(self, no_user_env):
        off = generate_graph(FIXTURES / "cyclic_project", structure_only=True,
                             show_cycles=False)
        high = generate_graph(FIXTURES / "cyclic_project", structure_only=True,
                              cycle_threshold=1e9)
        assert off.graph.import_cycles == []
        assert high.graph.import_cycles == []

    def test_dry_run_keeps_its_stats_note(self):
        result = generate_graph(FIXTURES / "small_project", dry_run=True)
        assert result.graph.architecture_notes[0].note.startswith("DRY RUN:")


class TestGraphView:
    def test_view_has_nodes_links_cycles_provenance(self, no_user_env):
        graph = generate_graph(FIXTURES / "cyclic_project", structure_only=True).graph
        view = graph_view(graph)
        assert set(view) == {"nodes", "links", "cycles", "provenance"}
        ids = {n["id"] for n in view["nodes"]}
        assert all(link["source"] in ids and link["target"] in ids
                   for link in view["links"])
        assert view["cycles"] == len(graph.import_cycles) > 0
        assert any(n["in_cycle"] for n in view["nodes"])
        assert view["provenance"]["graphlm_version"] == graph.meta.graphlm_version

    def test_view_without_meta_has_no_provenance(self):
        assert graph_view(CodebaseGraph(directory_tree=""))["provenance"] is None


def test_importing_graphlm_does_not_load_user_env(tmp_path):
    """A host process that imports graphlm must not inherit graphlm's .env."""
    cfg = tmp_path / "graphlm"
    cfg.mkdir()
    (cfg / ".env").write_text("GRAPHLM_MODEL=leaked-from-dotenv\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRAPHLM_")}
    env["XDG_CONFIG_HOME"] = str(tmp_path)
    probe = (
        "import os, graphlm, graphlm.backend, graphlm.view, graphlm.query; "
        "print(os.environ.get('GRAPHLM_MODEL', 'unset'))"
    )
    out = subprocess.run([sys.executable, "-c", probe], env=env, check=True,
                         capture_output=True, text=True).stdout.strip()
    assert out == "unset"
