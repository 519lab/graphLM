"""Integration tests for the full pipeline (mocked LLM, no real API calls)."""

import json
from pathlib import Path

import pytest
from httpx import Response

from graphlm import generate_graph
from graphlm.llm import GraphLLError
from graphlm.models import CodebaseGraph


def _mock_pass1_response(httpx_mock, requested_files):
    """Mock pass 1 LLM response returning requested file paths."""
    body = {"choices": [{"message": {"content": json.dumps({"requested_files": requested_files})}, "index": 0}]}
    httpx_mock.add_response(json=body)


def _mock_pass2_response(httpx_mock, graph_data):
    """Mock pass 2 LLM response returning a complete graph."""
    body = {"choices": [{"message": {"content": json.dumps(graph_data)}, "index": 0}]}
    httpx_mock.add_response(json=body)


def _make_graph(**overrides):
    """Create a minimal valid graph dict, with optional overrides."""
    base = {
        "directory_tree": "test-project/\n",
        "import_edges": [],
        "modules": [],
        "data_flow": [],
        "database_schema": None,
        "test_organization": [],
        "architecture_notes": [{"note": "test"}],
        "quick_reference": [],
    }
    base.update(overrides)
    return base


class TestFullPipeline:
    def test_small_project_full_pipeline(self, httpx_mock, small_project, tmp_path):
        """End-to-end: small project scan → LLM passes → graph output."""
        _mock_pass1_response(httpx_mock, ["main.py", "mylib/helpers.py"])
        graph = _make_graph()
        _mock_pass2_response(httpx_mock, graph)

        result = generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
            dry_run=False,
        )

        assert isinstance(result.graph, CodebaseGraph)
        assert result.files_analyzed > 0
        # Verify outputs were written
        md_files = list(tmp_path.glob("*.md"))
        json_files = list(tmp_path.glob("*.json"))
        assert len(md_files) >= 1
        assert len(json_files) >= 1

    def test_pass1_null_requested_files_still_writes_a_graph(
        self, httpx_mock, small_project, tmp_path
    ):
        # {"requested_files": null} is valid JSON and used to TypeError after
        # the paid pass-1 call (#115).
        httpx_mock.add_response(
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"requested_files": None})
                        },
                        "index": 0,
                    }
                ]
            }
        )
        _mock_pass2_response(httpx_mock, _make_graph())
        result = generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
        )
        assert isinstance(result.graph, CodebaseGraph)
        assert (tmp_path / "GRAPH.md").exists()

    def test_directory_tree_filled_locally_not_from_llm(
        self, httpx_mock, small_project, tmp_path
    ):
        """The model returns an empty directory_tree (to save output tokens);
        generate_graph fills it from the scan instead (#18)."""
        _mock_pass1_response(httpx_mock, ["main.py"])
        # Model returns empty tree, as the pass-2 prompt now instructs.
        _mock_pass2_response(httpx_mock, _make_graph(directory_tree=""))

        result = generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
        )
        # Despite the LLM returning "", the graph carries the real scanned tree.
        assert result.graph.directory_tree
        assert small_project.name in result.graph.directory_tree
        # And it reached the rendered Markdown.
        md = (tmp_path / "GRAPH.md").read_text()
        assert small_project.name in md

    def test_timeout_arg_reaches_http_client(
        self, httpx_mock, small_project, tmp_path, monkeypatch
    ):
        """An explicit generate_graph(timeout=...) must configure the httpx
        client's timeout — the --timeout flag / GRAPHLM_TIMEOUT path (#18)."""
        import graphlm.llm as llm_mod

        seen: list[float | None] = []
        real_client = llm_mod.httpx.Client

        def spy_client(*args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return real_client(*args, **kwargs)

        monkeypatch.setattr(llm_mod.httpx, "Client", spy_client)

        _mock_pass1_response(httpx_mock, ["main.py"])
        _mock_pass2_response(httpx_mock, _make_graph())
        generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
            timeout=42.0,
        )
        # Both passes' clients were built with the explicit timeout.
        assert seen and all(t == 42.0 for t in seen)

    def test_timeout_env_honoured_when_endpoint_is_explicit(
        self, httpx_mock, small_project, tmp_path, monkeypatch
    ):
        # GRAPHLM_TIMEOUT must still apply when the endpoint triple is passed
        # as args (CLI -b/-k/-m). Settings(base_url, api_key, model) used to
        # take the dataclass default 300 and skip the env (#92).
        monkeypatch.setenv("GRAPHLM_TIMEOUT", "600")
        import graphlm.llm as llm_mod

        seen: list[float | None] = []
        real_client = llm_mod.httpx.Client

        def spy_client(*args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return real_client(*args, **kwargs)

        monkeypatch.setattr(llm_mod.httpx, "Client", spy_client)

        _mock_pass1_response(httpx_mock, ["main.py"])
        _mock_pass2_response(httpx_mock, _make_graph())
        generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
        )
        assert seen and all(t == 600.0 for t in seen)

    def test_max_output_tokens_independent_of_input_admission(
        self, large_project, monkeypatch
    ):
        """max_output_tokens is a request ceiling, NOT taken out of the input
        budget — so changing it must not change how many files are admitted into
        pass 2 (input and output ceilings are independent — #25)."""
        # A binding input budget so admission is observable, held constant.
        monkeypatch.setenv("GRAPHLM_MAX_CONTEXT", "4000")
        small_out = generate_graph(
            large_project, dry_run=True, max_output_tokens=8000
        )
        big_out = generate_graph(
            large_project, dry_run=True, max_output_tokens=128000
        )
        # Same input admission regardless of the output ceiling.
        assert small_out.files_analyzed == big_out.files_analyzed
        assert small_out.pass2_context_tokens == big_out.pass2_context_tokens

    def test_max_output_tokens_reaches_both_requests_as_max_tokens(
        self, httpx_mock, small_project, tmp_path
    ):
        """An explicit max_output_tokens must reach both LLM requests.

        A large directory tree can exhaust pass 1 before pass 2 begins, so the
        documented override must not leave pass 1 at the default ceiling (#73).
        """
        _mock_pass1_response(httpx_mock, ["main.py"])
        _mock_pass2_response(httpx_mock, _make_graph())
        generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
            max_output_tokens=99000,
        )
        bodies = [json.loads(request.content) for request in httpx_mock.get_requests()]
        assert [body["max_tokens"] for body in bodies] == [99000, 99000]

    def test_small_project_dry_run(self, httpx_mock, small_project, tmp_path):
        """Dry run should NOT call the LLM."""
        result = generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            dry_run=True,
        )

        # Should have graph with tree but no LLM data
        assert "test-project" in result.graph.directory_tree or "small_project" in result.graph.directory_tree
        assert len(result.graph.architecture_notes) == 1
        assert "DRY RUN" in result.graph.architecture_notes[0].note
        # No LLM calls should have been made
        assert len(httpx_mock.get_requests()) == 0

    def test_medium_project_with_imports(self, httpx_mock, medium_project):
        """Test that import edges are captured in the graph."""
        _mock_pass1_response(httpx_mock, [
            "pyproject.toml",
            "src/__init__.py",
            "src/core/__init__.py",
            "src/utils/__init__.py",
        ])
        graph = _make_graph(
            import_edges=[
                {"from_path": "src/__init__.py", "to_path": "src/core/__init__.py", "kind": "import"},
                {"from_path": "src/__init__.py", "to_path": "src/utils/__init__.py", "kind": "import"},
            ],
            modules=[
                {"path": "src/core/__init__.py", "name": "Core Engine", "description": "Main processing engine"},
                {"path": "src/utils/__init__.py", "name": "Utils", "description": "Utility functions"},
            ],
        )
        _mock_pass2_response(httpx_mock, graph)

        result = generate_graph(
            medium_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
        )

        assert len(result.graph.import_edges) == 2
        assert len(result.graph.modules) == 2

    def test_medium_project_with_database(self, httpx_mock, medium_project):
        """Test that database schema is captured."""
        _mock_pass1_response(httpx_mock, ["pyproject.toml", "migrations/001_initial.sql"])
        graph = _make_graph(
            database_schema=[
                {
                    "name": "items",
                    "columns": [
                        {"name": "id", "type": "INTEGER", "constraints": "PRIMARY KEY"},
                        {"name": "name", "type": "TEXT", "constraints": "NOT NULL"},
                    ],
                    "description": "Items table",
                },
            ],
        )
        _mock_pass2_response(httpx_mock, graph)

        result = generate_graph(
            medium_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
        )

        assert result.graph.database_schema is not None
        assert len(result.graph.database_schema) == 1
        assert result.graph.database_schema[0].name == "items"

    def test_large_project_full_pipeline(self, httpx_mock, large_project, tmp_path):
        """End-to-end: large project with many files."""
        # LLM requests a subset of files
        requested = [
            "pyproject.toml",
            "README.md",
            "app/main.py",
            "app/__init__.py",
            "app/models/user.py",
            "app/models/item.py",
            "app/routes/users.py",
            "app/routes/items.py",
            "app/services/auth.py",
            "app/services/user_service.py",
            "app/services/item_service.py",
            "migrations/001_create_users.sql",
            "migrations/002_create_items.sql",
            "templates/base.html",
            "static/css/app.css",
            "static/js/app.js",
        ]
        _mock_pass1_response(httpx_mock, requested)

        graph = _make_graph(
            import_edges=[
                {"from_path": "app/main.py", "to_path": "app/routes/users.py", "kind": "include"},
                {"from_path": "app/main.py", "to_path": "app/routes/items.py", "kind": "include"},
                {"from_path": "app/routes/users.py", "to_path": "app/services/user_service.py", "kind": "import"},
                {"from_path": "app/services/auth.py", "to_path": "app/services/user_service.py", "kind": "uses"},
            ],
            modules=[
                {"path": "app/main.py", "name": "App Factory", "description": "FastAPI app assembly"},
                {"path": "app/routes/users.py", "name": "User Routes", "description": "User API endpoints"},
                {"path": "app/routes/items.py", "name": "Item Routes", "description": "Item API endpoints"},
                {"path": "app/services/auth.py", "name": "Auth Service", "description": "Password hashing"},
                {"path": "app/services/user_service.py", "name": "User Service", "description": "User business logic"},
                {"path": "app/services/item_service.py", "name": "Item Service", "description": "Item business logic"},
            ],
            data_flow=[
                {"source": "User Routes", "destination": "User Service", "description": "User creation requests"},
                {"source": "Item Routes", "destination": "Item Service", "description": "Item creation requests"},
                {"source": "Auth Service", "destination": "User Service", "description": "Password hashing"},
            ],
            database_schema=[
                {
                    "name": "users",
                    "columns": [
                        {"name": "id", "type": "INTEGER", "constraints": "PRIMARY KEY"},
                        {"name": "email", "type": "TEXT", "constraints": "NOT NULL UNIQUE"},
                    ],
                    "description": "User accounts",
                },
                {
                    "name": "items",
                    "columns": [
                        {"name": "id", "type": "INTEGER", "constraints": "PRIMARY KEY"},
                        {"name": "title", "type": "TEXT", "constraints": "NOT NULL"},
                    ],
                    "description": "User items",
                },
            ],
            test_organization=[
                {"file": "tests/test_users.py", "covers": "User schema validation"},
                {"file": "tests/test_auth.py", "covers": "Password hashing and verification"},
                {"file": "tests/test_items.py", "covers": "Item schema validation"},
            ],
            architecture_notes=[
                {"note": "FastAPI with Jinja2 templates"},
                {"note": "SQLite with raw SQL migrations"},
                {"note": "Service layer pattern for business logic"},
            ],
            quick_reference=[
                {"query": "app factory", "location": "app/main.py"},
                {"query": "user routes", "location": "app/routes/users.py"},
                {"query": "item routes", "location": "app/routes/items.py"},
                {"query": "password hashing", "location": "app/services/auth.py"},
            ],
        )
        _mock_pass2_response(httpx_mock, graph)

        result = generate_graph(
            large_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
            include_tests=True,
        )

        assert len(result.graph.import_edges) == 4
        assert len(result.graph.modules) == 6
        assert len(result.graph.data_flow) == 3
        assert len(result.graph.database_schema) == 2
        assert len(result.graph.test_organization) == 3
        assert len(result.graph.architecture_notes) == 3
        assert len(result.graph.quick_reference) == 4

    def test_invalid_pass1_json_raises(self, httpx_mock, small_project):
        """If pass 1 returns invalid JSON, should raise."""
        httpx_mock.add_response(
            json={"choices": [{"message": {"content": "not json"}, "index": 0}]}
        )
        with pytest.raises(GraphLLError) as exc_info:
            generate_graph(
                small_project,
                base_url="http://test.local/v1",
                api_key="test-key",
                model="test-model",
            )
        assert "not valid JSON" in str(exc_info.value).lower() or "Could not extract JSON" in str(exc_info.value)

    def test_incomplete_config_raises(self, small_project):
        """If only some config args are provided, should raise."""
        with pytest.raises(ValueError) as exc_info:
            generate_graph(
                small_project,
                base_url="http://test.local/v1",
                api_key="test-key",
                # model is missing
            )
        assert "all three must be provided" in str(exc_info.value)

    def test_ast_dry_run_attaches_edges_and_cycles(self):
        """ast=True dry-run keeps parser edges and detects the fixture cycle."""
        cyclic_project = Path(__file__).parent / "fixtures" / "cyclic_project"
        result = generate_graph(cyclic_project, dry_run=True)

        assert result.graph.deterministic_edges is not None
        assert len(result.graph.deterministic_edges) > 0
        assert result.graph.import_cycles
        cycle_nodes = {
            n.replace("\\", "/")
            for cycle in result.graph.import_cycles
            for n in cycle.nodes
        }
        for rel in ("app/main.py", "app/routes.py", "app/services.py"):
            assert rel in cycle_nodes or any(n.endswith(rel) for n in cycle_nodes)

    def test_edge_prompt_cap_does_not_shrink_graph_edges_or_cycles(self):
        """The pass-2 prompt caps the edge table under a tight budget, but the
        graph's deterministic_edges and cycle detection must use the FULL list
        regardless of max_context (the cap is a prompt-only concern — #12).
        """
        cyclic_project = Path(__file__).parent / "fixtures" / "cyclic_project"
        full = generate_graph(cyclic_project, dry_run=True, max_context=120000)
        capped = generate_graph(cyclic_project, dry_run=True, max_context=8000)

        assert full.graph.deterministic_edges  # sanity
        assert len(capped.graph.deterministic_edges or []) == len(
            full.graph.deterministic_edges or []
        )
        assert len(capped.graph.import_cycles) == len(full.graph.import_cycles)

    def test_max_context_precedence_env_then_arg(self, large_project, monkeypatch):
        """max_context resolves flag/arg > GRAPHLM_MAX_CONTEXT env > 120000.

        Uses large_project with a *binding* env budget — just above the
        output/instruction floor but below what the project's content needs — so
        the reported pass-2 total tracks the resolved budget. The budget is
        derived from the reserve so it stays binding when the reserve changes;
        smaller fixtures fit entirely at every budget and can't show precedence
        (#17/#18).
        """
        from graphlm.context import MESSAGE_OVERHEAD_TOKENS

        # Above the true floor (overhead + ~1k instruction block ≈ 2.5k) but
        # below what large_project's content needs, so it binds.
        reserve = MESSAGE_OVERHEAD_TOKENS
        env_budget = reserve + 2000

        # env only: the env var is honored (was previously ignored).
        monkeypatch.setenv("GRAPHLM_MAX_CONTEXT", str(env_budget))
        env_result = generate_graph(large_project, dry_run=True)

        # explicit arg wins over the env var (much larger, non-binding).
        arg_result = generate_graph(
            large_project, dry_run=True, max_context=reserve + 20000
        )

        # default when neither is set.
        monkeypatch.delenv("GRAPHLM_MAX_CONTEXT", raising=False)
        default_result = generate_graph(large_project, dry_run=True)

        # The binding env cap must produce a smaller pass-2 total than the larger
        # explicit arg or the 120000 default — i.e. the env var takes effect (it
        # was previously ignored entirely).
        assert env_result.pass2_context_tokens < arg_result.pass2_context_tokens
        assert env_result.pass2_context_tokens < default_result.pass2_context_tokens
        # The env cap binds the reported total at or below its budget.
        assert env_result.pass2_context_tokens <= env_budget

    def test_include_html_false_skips_html(self, httpx_mock, small_project, tmp_path):
        """generate_graph with include_html=False must not write GRAPH.html."""
        _mock_pass1_response(httpx_mock, ["main.py", "mylib/helpers.py"])
        _mock_pass2_response(httpx_mock, _make_graph())

        generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
            include_html=False,
        )

        assert (tmp_path / "GRAPH.md").exists()
        assert (tmp_path / "GRAPH.json").exists()
        assert not (tmp_path / "GRAPH.html").exists()

    def test_ast_full_pipeline_attaches_deterministic_edges(self, httpx_mock):
        """Mocked pipeline with ast=True attaches parser edges to the graph."""
        cyclic_project = Path(__file__).parent / "fixtures" / "cyclic_project"
        _mock_pass1_response(
            httpx_mock, ["app/main.py", "app/routes.py", "app/services.py"]
        )
        _mock_pass2_response(httpx_mock, _make_graph())

        result = generate_graph(
            cyclic_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
        )

        assert result.graph.deterministic_edges is not None

    def test_ast_false_skips_deterministic_edges(self, small_project):
        result = generate_graph(small_project, dry_run=True, ast=False)
        assert result.graph.deterministic_edges is None

    def test_write_accepts_str_and_returns_three_paths(self, small_project, tmp_path):
        """GraphResult.write accepts str paths and unpacks to md, json, html."""
        result = generate_graph(small_project, dry_run=True)
        md_path, json_path, html_path = result.write(str(tmp_path))
        assert md_path.exists()
        assert json_path.exists()
        assert html_path is not None
        assert html_path.exists()
        _, _, no_html = result.write(str(tmp_path / "plain"), include_html=False)
        assert no_html is None
        assert md_path.name == "GRAPH.md"
        assert json_path.name == "GRAPH.json"
        assert html_path.name == "GRAPH.html"
        assert not (tmp_path / "plain" / "GRAPH.html").exists()

    def test_show_cycles_false_leaves_cycles_empty(self):
        cyclic_project = Path(__file__).parent / "fixtures" / "cyclic_project"
        result = generate_graph(
            cyclic_project, dry_run=True, ast=True, show_cycles=False
        )
        assert result.graph.deterministic_edges
        assert result.graph.import_cycles == []

    def test_show_cycles_false_after_llm(self, httpx_mock, small_project):
        _mock_pass1_response(httpx_mock, ["main.py"])
        _mock_pass2_response(httpx_mock, _make_graph())
        result = generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            show_cycles=False,
        )
        assert result.graph.import_cycles == []


class TestProvenanceStamp:
    """The self-refreshing provenance stamp (meta + rendered directive)."""

    def test_full_pipeline_stamps_meta_with_real_sha(
        self, httpx_mock, tmp_path
    ):
        # Build an ISOLATED git repo (don't depend on graphLM's own ambient
        # .git, which is absent from an sdist/wheel test run). The stamped SHA
        # must equal this repo's HEAD exactly, whatever its object format.
        import re
        import subprocess

        repo = tmp_path / "proj"
        repo.mkdir()
        (repo / "main.py").write_text("def run():\n    return 1\n")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c",
             "user.name=t", "add", "-A"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c",
             "user.name=t", "-c", "commit.gpgsign=false", "commit", "-q",
             "-m", "init"], check=True,
        )
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        out = tmp_path / "out"
        _mock_pass1_response(httpx_mock, ["main.py"])
        _mock_pass2_response(httpx_mock, _make_graph())
        result = generate_graph(
            repo,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=out,
        )
        meta = result.graph.meta
        assert meta is not None
        # Exact match to the isolated repo's HEAD, not a length guess (the
        # module supports SHA-256 too, so don't hardcode SHA-1's 40).
        assert meta.commit_sha == head
        assert re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", meta.commit_sha)
        assert meta.created_at.endswith("Z")
        assert meta.schema_version == 1
        # And the directive reached GRAPH.md ("generated [by graphlm X] against
        # commit …" — the version prefix is present when installed).
        md = (out / "GRAPH.md").read_text()
        assert "against commit" in md
        assert meta.commit_sha[:8] in md

    def test_llm_hallucinated_meta_is_overwritten(self, httpx_mock, small_project):
        # If the model emits its own meta, generate_graph overwrites it locally
        # (like directory_tree) — never trusts LLM-provided provenance.
        _mock_pass1_response(httpx_mock, ["main.py"])
        _mock_pass2_response(
            httpx_mock,
            _make_graph(
                meta={
                    "schema_version": 99,
                    "created_at": "1999-01-01T00:00:00Z",
                    "commit_sha": "deadbeef" * 5,
                    "graphlm_version": "hacked",
                }
            ),
        )
        result = generate_graph(
            small_project,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
        )
        meta = result.graph.meta
        assert meta is not None
        assert meta.created_at != "1999-01-01T00:00:00Z"
        assert meta.commit_sha != "deadbeef" * 5
        assert meta.schema_version == 1

    def test_dry_run_also_stamps_meta(self, small_project):
        result = generate_graph(small_project, dry_run=True)
        assert result.graph.meta is not None
        assert result.graph.meta.created_at.endswith("Z")

    def test_non_git_project_stamps_null_sha(self, tmp_path):
        # A scratch dir outside any repo -> commit_sha None, non-git directive.
        (tmp_path / "mod.py").write_text("x = 1\n")
        result = generate_graph(tmp_path, dry_run=True)
        assert result.graph.meta is not None
        assert result.graph.meta.commit_sha is None
        md = render_markdown_of(result.graph)
        assert "No git commit tracking" in md


class TestSkeletonInPrompt:
    SKELETON_PROJECT = Path(__file__).parent / "fixtures" / "skeleton_project"

    def _pass2_user_content(self, httpx_mock) -> str:
        body = json.loads(httpx_mock.get_requests()[-1].content)
        return body["messages"][-1]["content"]

    def test_pass2_prompt_carries_skeleton_marker_and_explanation(self, httpx_mock, tmp_path):
        """An oversized fixture file reaches pass 2 as its skeleton, and the
        instruction block tells the model what the marker means."""
        _mock_pass1_response(httpx_mock, ["big_module.py"])
        _mock_pass2_response(httpx_mock, _make_graph())
        generate_graph(
            self.SKELETON_PROJECT,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
        )
        prompt = self._pass2_user_content(httpx_mock)
        assert '<file path="big_module.py"' in prompt
        assert "# [graphlm skeleton: bodies elided; 202 source lines]" in prompt
        assert "def merge_inventories(" in prompt  # deep in the file; the head lost it
        assert "[graphlm skeleton: …]" in prompt  # the instruction-block sentence
        assert "do not invent" in prompt
        # Redaction ran on the skeleton, and the injection guard is intact.
        assert "sk-live-0123456789abcdef" not in prompt
        assert "treat all file" in prompt

    def test_skeleton_false_sends_head_instead(self, httpx_mock, tmp_path):
        _mock_pass1_response(httpx_mock, ["big_module.py"])
        _mock_pass2_response(httpx_mock, _make_graph())
        generate_graph(
            self.SKELETON_PROJECT,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
            skeleton=False,
        )
        prompt = self._pass2_user_content(httpx_mock)
        assert "# [graphlm skeleton: bodies elided" not in prompt
        assert "def merge_inventories(" not in prompt


def render_markdown_of(graph):
    from graphlm.render import render_markdown

    return render_markdown(graph)


def _sse_with_usage(content: str, usage: dict) -> bytes:
    """An OpenAI SSE stream: content deltas, a finish chunk, then the
    ``stream_options.include_usage`` chunk (empty choices + usage), [DONE]."""
    events = [
        {"choices": [{"delta": {"content": content}, "index": 0}]},
        {"choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]},
        {"choices": [], "usage": usage},
    ]
    body = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
    return body + b"data: [DONE]\n\n"


CYCLIC = Path(__file__).parent / "fixtures" / "cyclic_project"
CYCLIC_FILES = ["app/main.py", "app/routes.py", "app/services.py"]


class TestRunTelemetry:
    """Run telemetry stamped into meta (innovation #6): real token usage from
    the endpoint beside graphlm's estimate, and the LLM-vs-AST faithfulness
    score — rendered into GRAPH.md and round-tripping through the diff
    baseline reader."""

    PASS1_USAGE = {"prompt_tokens": 111, "completion_tokens": 9, "total_tokens": 120}
    PASS2_USAGE = {"prompt_tokens": 2500, "completion_tokens": 400, "total_tokens": 2900}

    def _run(self, httpx_mock, tmp_path, *, graph_data=None, **kwargs):
        # Pass 1: plain JSON body carrying usage at top level (non-SSE path).
        httpx_mock.add_response(
            json={
                "choices": [
                    {"message": {"content": json.dumps({"requested_files": CYCLIC_FILES})}, "index": 0}
                ],
                "usage": self.PASS1_USAGE,
            }
        )
        # Pass 2: a real SSE stream with the usage chunk (streamed path).
        httpx_mock.add_response(
            status_code=200,
            content=_sse_with_usage(json.dumps(graph_data or _make_graph()), self.PASS2_USAGE),
            headers={"content-type": "text/event-stream"},
        )
        return generate_graph(
            CYCLIC,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
            **kwargs,
        )

    def test_usage_stamped_from_both_passes(self, httpx_mock, tmp_path):
        result = self._run(httpx_mock, tmp_path)
        meta = result.graph.meta
        assert meta is not None and meta.usage is not None
        assert meta.usage.pass1 is not None and meta.usage.pass2 is not None
        assert meta.usage.pass1.prompt_tokens == 111
        assert meta.usage.pass1.completion_tokens == 9
        assert meta.usage.pass2.prompt_tokens == 2500
        assert meta.usage.pass2.completion_tokens == 400
        # The estimate is graphlm's own figure for the same prompt, so the
        # real-vs-estimated ratio is derivable from the stamp alone.
        from graphlm.context import (
            MESSAGE_OVERHEAD_TOKENS,
            assemble_pass1_prompt,
            estimate_tokens,
        )

        assert meta.usage.pass1.estimated_prompt_tokens == result.pass1_context_tokens
        assert meta.usage.pass1.estimated_prompt_tokens == (
            estimate_tokens(assemble_pass1_prompt(result.graph.directory_tree))
            + MESSAGE_OVERHEAD_TOKENS
        )
        assert meta.usage.pass2.estimated_prompt_tokens == result.pass2_context_tokens
        assert meta.usage.pass2.estimated_prompt_tokens > 0

    def test_faithfulness_scored_against_ast_edges(self, httpx_mock, tmp_path):
        # Seed the LLM's edge list with one real AST edge, one invented edge,
        # and one non-Python edge the parser could never have seen.
        dry = generate_graph(CYCLIC, dry_run=True)
        ast_edges = dry.graph.deterministic_edges
        assert ast_edges  # the cyclic fixture has parser edges
        real = ast_edges[0]
        llm_edges = [
            {"from_path": real.from_path, "to_path": real.to_path, "kind": "import"},
            {"from_path": "app/main.py", "to_path": "app/nonexistent.py", "kind": "import"},
            {"from_path": "web/app.ts", "to_path": "web/util.ts", "kind": "import"},
        ]
        result = self._run(httpx_mock, tmp_path, graph_data=_make_graph(import_edges=llm_edges))
        f = result.graph.meta.faithfulness
        assert f is not None
        assert f.ast_edges == len({(e.from_path, e.to_path) for e in ast_edges})
        assert f.llm_edges == 2  # the .ts edge is excluded, not penalised
        assert f.matched == 1
        assert f.precision == pytest.approx(0.5)
        assert f.recall == pytest.approx(1 / f.ast_edges)

    def test_telemetry_line_rendered_in_graph_md(self, httpx_mock, tmp_path):
        self._run(httpx_mock, tmp_path)
        md = (tmp_path / "GRAPH.md").read_text()
        directive_at = md.index("Provenance & refresh directive")
        telemetry_at = md.index("**Run telemetry.**")
        assert directive_at < telemetry_at < md.index("# Codebase Graph")
        assert "pass 2 prompt: 2500 tokens (graphlm estimated" in md
        assert "output: 400 tokens" in md
        assert "LLM import edges vs parser ground truth: precision" in md

    def test_graph_json_round_trips_as_normal_baseline(self, httpx_mock, tmp_path):
        from graphlm.diff import BaselineState, load_baseline

        result = self._run(httpx_mock, tmp_path)
        graph, state = load_baseline(tmp_path / "GRAPH.json")
        assert state is BaselineState.NORMAL
        assert graph is not None and graph.meta is not None
        assert graph.meta.schema_version == 1  # additive fields, no bump
        assert graph.meta.usage == result.graph.meta.usage
        assert graph.meta.faithfulness == result.graph.meta.faithfulness

    def test_endpoint_without_usage_leaves_counts_null_but_estimate_set(
        self, httpx_mock, small_project
    ):
        _mock_pass1_response(httpx_mock, ["main.py"])
        _mock_pass2_response(httpx_mock, _make_graph())
        result = generate_graph(
            small_project, base_url="http://test.local/v1", api_key="k", model="m"
        )
        usage = result.graph.meta.usage
        assert usage is not None and usage.pass2 is not None
        assert usage.pass2.prompt_tokens is None
        assert usage.pass2.completion_tokens is None
        assert usage.pass2.estimated_prompt_tokens == result.pass2_context_tokens
        # Null counts render as "not reported", never as a fake number.
        assert "pass 2 prompt: not reported by endpoint" in render_markdown_of(result.graph)

    def test_malformed_usage_values_read_as_not_reported(self, httpx_mock, small_project):
        _mock_pass1_response(httpx_mock, ["main.py"])
        httpx_mock.add_response(
            json={
                "choices": [{"message": {"content": json.dumps(_make_graph())}, "index": 0}],
                "usage": {"prompt_tokens": "lots", "completion_tokens": True},
            }
        )
        result = generate_graph(
            small_project, base_url="http://test.local/v1", api_key="k", model="m"
        )
        p2 = result.graph.meta.usage.pass2
        assert p2.prompt_tokens is None
        assert p2.completion_tokens is None  # bool is not accepted as an int

    def test_no_ast_leaves_faithfulness_none(self, httpx_mock, tmp_path):
        result = self._run(httpx_mock, tmp_path, ast=False)
        assert result.graph.meta.faithfulness is None
        md = (tmp_path / "GRAPH.md").read_text()
        # Usage still rendered; the faithfulness half is simply absent.
        assert "**Run telemetry.**" in md
        assert "parser ground truth" not in md

    def test_dry_run_has_no_telemetry(self):
        result = generate_graph(CYCLIC, dry_run=True)
        assert result.graph.meta is not None
        assert result.graph.meta.usage is None
        assert result.graph.meta.faithfulness is None
        assert "**Run telemetry.**" not in render_markdown_of(result.graph)


class TestEvidenceIntegration:
    """The evidence-support fill-site in generate_graph — the behaviors nothing
    else catches (the wizard lesson: test the wiring, not just the module).

    generate_graph calls graphlm.evidence.score after the paid pass-2. These
    tests spy on that call to assert gating and resilience at the integration
    boundary, without any TypeSafe network."""

    def _summ_graph(self):
        return _make_graph(
            file_summaries=[
                {"path": "app/main.py", "summary": "entry", "symbols": []},
                {"path": "app/routes.py", "summary": "routes", "symbols": []},
            ]
        )

    def _run(self, httpx_mock, tmp_path, **kwargs):
        _mock_pass1_response(httpx_mock, CYCLIC_FILES)
        _mock_pass2_response(httpx_mock, self._summ_graph())
        return generate_graph(
            CYCLIC,
            base_url="http://test.local/v1",
            api_key="test-key",
            model="test-model",
            output_dir=tmp_path,
            **kwargs,
        )

    def test_score_called_and_result_stamped(self, httpx_mock, tmp_path, monkeypatch):
        from graphlm.models import EvidenceSupport, FileScore

        seen = {}

        def fake_score(summaries, pass2_files, *, redact_secrets, api_key):
            seen["summaries"] = [s.path for s in summaries]
            seen["redact"] = redact_secrets
            return EvidenceSupport(
                mean=0.8, scored=2, skipped=0,
                low=[FileScore(path="app/routes.py", score=0.3)],
            )

        monkeypatch.setattr("graphlm.evidence.score", fake_score)
        result = self._run(httpx_mock, tmp_path)
        assert result.graph.meta.evidence_support is not None
        assert result.graph.meta.evidence_support.mean == 0.8
        assert seen["summaries"] == ["app/main.py", "app/routes.py"]
        assert seen["redact"] is True  # redaction on by default
        # Rendered into GRAPH.md telemetry line, including the low-outlier clause
        # (the only link between a low score and what a reader sees).
        md = (tmp_path / "GRAPH.md").read_text()
        assert "summary evidence support" in md
        assert "weakest: `app/routes.py` 0.30" in md

    def test_no_evidence_flag_skips_scoring(self, httpx_mock, tmp_path, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "graphlm.evidence.score",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        result = self._run(httpx_mock, tmp_path, include_evidence=False)
        assert called["n"] == 0
        assert result.graph.meta.evidence_support is None

    def test_no_redact_gates_off_in_scorer(self, httpx_mock, tmp_path, monkeypatch):
        # With --no-redact, generate_graph still CALLS score, but score returns
        # None because redact_secrets is False (the gate lives in the scorer).
        seen = {}

        def fake_score(summaries, pass2_files, *, redact_secrets, api_key):
            seen["redact"] = redact_secrets
            # Mirror the real gate: no scoring without redaction.
            return None if not redact_secrets else object()

        monkeypatch.setattr("graphlm.evidence.score", fake_score)
        result = self._run(httpx_mock, tmp_path, redact_secrets=False)
        assert seen["redact"] is False
        assert result.graph.meta.evidence_support is None

    def test_scorer_raising_does_not_discard_graph(self, httpx_mock, tmp_path, monkeypatch):
        # The scorer runs after the paid pass-2. If it somehow raises, the graph
        # must still be returned and written — the score is best-effort telemetry.
        def boom(*a, **k):
            raise RuntimeError("typesafe exploded")

        monkeypatch.setattr("graphlm.evidence.score", boom)
        # generate_graph must not propagate — the real score() never raises, but
        # this guards the fill-site against a future scorer that does.
        try:
            result = self._run(httpx_mock, tmp_path)
        except RuntimeError:
            import pytest
            pytest.fail("scorer exception discarded the paid graph")
        assert isinstance(result.graph, CodebaseGraph)
        assert (tmp_path / "GRAPH.json").exists()

    def test_dry_run_never_scores(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "graphlm.evidence.score",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        result = generate_graph(CYCLIC, dry_run=True)
        assert called["n"] == 0
        assert result.graph.meta.evidence_support is None


class TestImportanceIntegration:
    """Module-importance fill-site in generate_graph (LLM mocked, Jev faked)."""

    def _graph(self):
        return _make_graph(modules=[
            {"path": "app/main.py", "name": "main", "description": "entry point"},
            {"path": "app/routes.py", "name": "routes", "description": "routing"},
        ])

    def _run(self, httpx_mock, tmp_path, **kwargs):
        _mock_pass1_response(httpx_mock, CYCLIC_FILES)
        _mock_pass2_response(httpx_mock, self._graph())
        return generate_graph(
            CYCLIC, base_url="http://test.local/v1", api_key="test-key",
            model="test-model", output_dir=tmp_path, **kwargs,
        )

    def test_role_and_degree_filled(self, httpx_mock, tmp_path, monkeypatch):
        seen = {}

        def fake_score_importance(modules, edges, *, api_key, client=None, timeout=60, summaries_by_path=None):
            seen["paths"] = [m.path for m in modules]
            return {"app/main.py": 2.9, "app/routes.py": 1.5}

        monkeypatch.setattr("graphlm.evidence.score_importance", fake_score_importance)
        result = self._run(httpx_mock, tmp_path)
        by = {m.path: m for m in result.graph.modules}
        assert by["app/main.py"].role == 2.9
        assert by["app/routes.py"].role == 1.5
        # degree filled from AST edges (deterministic), non-None for scored modules.
        assert by["app/main.py"].degree is not None

    def test_no_importance_flag_leaves_fields_none(self, httpx_mock, tmp_path, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "graphlm.evidence.score_importance",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        result = self._run(httpx_mock, tmp_path, include_importance=False)
        assert called["n"] == 0
        assert all(m.role is None and m.degree is None for m in result.graph.modules)
        # Rendered Modules table falls back to the old format (no Importance column).
        md = (tmp_path / "GRAPH.md").read_text()
        assert "| Importance |" not in md.split("## Modules")[1].split("\n##")[0]

    def test_no_redact_does_not_gate_importance(self, httpx_mock, tmp_path, monkeypatch):
        # Unlike evidence, importance still runs under --no-redact (no source sent).
        seen = {"called": False}

        def fake(modules, edges, **k):
            seen["called"] = True
            return {"app/main.py": 2.0}

        monkeypatch.setattr("graphlm.evidence.score_importance", fake)
        self._run(httpx_mock, tmp_path, redact_secrets=False)
        assert seen["called"] is True

    def test_scorer_raising_keeps_graph(self, httpx_mock, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "graphlm.evidence.score_importance",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("jev down")),
        )
        try:
            result = self._run(httpx_mock, tmp_path)
        except RuntimeError:
            import pytest
            pytest.fail("importance scorer exception discarded the paid graph")
        assert isinstance(result.graph, CodebaseGraph)
        assert all(m.role is None for m in result.graph.modules)

    def test_dry_run_never_scores(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "graphlm.evidence.score_importance",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        result = generate_graph(CYCLIC, dry_run=True)
        assert called["n"] == 0
