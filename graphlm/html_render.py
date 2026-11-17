"""Interactive HTML visualization for codebase graphs using D3.js."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from graphlm.models import CodebaseGraph
from graphlm.view import PALETTE, graph_view


def _load_template() -> str:
    """Load the HTML template from the companion file."""
    _template_path = Path(__file__).parent / "_html_template.html"
    return _template_path.read_text(encoding="utf-8")


def _json_for_script(value: object) -> str:
    """JSON that is safe to splice into an HTML ``<script>`` body.

    ``json.dumps`` does not escape ``<``, so a path or description containing
    ``</script>`` would close the inline script tag and execute whatever
    followed. U+2028/U+2029 break JS string literals in the same way.
    """
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL)


def _with_csp_hash(html: str) -> str:
    """Fill the CSP meta's script hash with the hash of the inline script.

    The policy allows exactly two scripts: the pinned, integrity-checked D3
    build and this file's own inline script — by its SHA-256, computed here
    because the script embeds this graph's data. An injected ``<script>`` or
    ``<script src=…>`` cannot run, and nothing can be fetched or posted. The
    meta tag precedes every script, so the first ``{CSP_SCRIPT_HASH}`` is the
    placeholder even if graph data repeats the string; ``_json_for_script``
    escapes ``<``, so the data cannot end the inline script early.
    """
    match = _INLINE_SCRIPT.search(html)
    if match is None:  # template without an inline script: allow none
        return html.replace("'{CSP_SCRIPT_HASH}' ", "", 1)
    digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
    token = "sha256-" + base64.b64encode(digest).decode("ascii")
    return html.replace("{CSP_SCRIPT_HASH}", token, 1)


def render_html(graph: CodebaseGraph) -> str:
    """Render a CodebaseGraph as a self-contained HTML file with D3.js.

    The output is a single HTML file with inline CSS, embedded data,
    and inline JavaScript -- no external files needed (except D3 CDN).
    The embedded data is :func:`graphlm.view.graph_view` — the same view
    model host applications draw — so the HTML and any other renderer agree.
    """
    data = _json_for_script(graph_view(graph))
    palette_js = _json_for_script(PALETTE)
    tpl = _load_template()
    # Palette first: graph data containing `{_PALETTE}` must not steal the
    # template placeholder and leave `const _PALETTE = {_PALETTE}` (#141).
    result = tpl.replace("{_PALETTE}", palette_js, 1)
    result = result.replace("{EMBEDDED_JSON}", data, 1)
    return _with_csp_hash(result)
