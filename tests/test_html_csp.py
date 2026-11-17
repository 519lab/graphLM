"""GRAPH.html runs only its own inline script and the pinned D3 build."""

from __future__ import annotations

import base64
import hashlib
import re

from graphlm.html_render import render_html
from graphlm.models import CodebaseGraph, ModuleDescription

_CSP = re.compile(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">')


def _inline_script(html: str) -> str:
    start = html.index("<script>") + len("<script>")
    return html[start:html.index("</script>", start)]


def _graph(desc: str = "entry") -> CodebaseGraph:
    return CodebaseGraph(
        directory_tree="root/\n",
        modules=[ModuleDescription(name="main", path="main.py", description=desc)],
    )


def test_csp_hash_matches_the_inline_script():
    html = render_html(_graph())
    policy = _CSP.search(html).group(1)
    digest = hashlib.sha256(_inline_script(html).encode("utf-8")).digest()
    assert f"'sha256-{base64.b64encode(digest).decode()}'" in policy
    assert "{CSP_SCRIPT_HASH}" not in html


def test_csp_is_restrictive():
    policy = _CSP.search(render_html(_graph())).group(1)
    assert "default-src 'none'" in policy
    assert "script-src 'sha256-" in policy and "https://cdn.jsdelivr.net/npm/d3@7.9.2/" in policy
    assert "'unsafe-inline'" not in policy.split("script-src", 1)[1].split(";", 1)[0]
    assert "base-uri 'none'" in policy and "form-action 'none'" in policy


def test_hash_tracks_the_embedded_data():
    a, b = render_html(_graph("one")), render_html(_graph("two"))
    assert _CSP.search(a).group(1) != _CSP.search(b).group(1)


def test_graph_data_cannot_steal_the_placeholder():
    html = render_html(_graph("{CSP_SCRIPT_HASH} </script><script>x()</script>"))
    policy = _CSP.search(html).group(1)
    assert "{CSP_SCRIPT_HASH}" not in policy
    # Data stays inside the one inline script (escaped), so the hash covers it.
    assert html.count("<script>") == 1


def test_template_without_inline_script_allows_no_inline_script():
    from graphlm.html_render import _with_csp_hash

    html = """<meta http-equiv="Content-Security-Policy" content="script-src '{CSP_SCRIPT_HASH}' https://x/">"""
    assert _with_csp_hash(html) == """<meta http-equiv="Content-Security-Policy" content="script-src https://x/">"""
