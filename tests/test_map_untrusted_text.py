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
    f"x # {MARK}",
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
    md = render_markdown(CodebaseGraph(directory_tree=f"root/\n  a # {MARK}\r\n  b"))
    assert " " not in md and "\\u2028" in md
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
