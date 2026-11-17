"""Make repository- and model-derived text inert in the Markdown map.

``GRAPH.md`` is read by coding agents and rendered by GitHub, IDEs and agent
UIs. Almost everything in it comes from the scanned repository (file names) or
from an LLM that read that repository (descriptions, notes, paths, symbol
names, edge endpoints) — so a hostile repository can try to write Markdown
structure into it: close a code fence, start a heading, add an image that
fetches an attacker URL when rendered, or inject raw HTML.

Every renderer passes such text through one of these helpers. Each one returns
its input **unchanged** when the input holds nothing that could change the
document's structure, so ordinary maps stay byte-identical; only hostile or
unusual values are altered. The helpers make text *inert*, they do not make it
*trustworthy*: an instruction written in plain words is still just words, which
is why the map also says, where an agent reads it, that its text is data.
"""

from __future__ import annotations

import re

# Characters that end or split a line for CommonMark, a tokenizer or an editor,
# plus every other control character.
_CONTROL = re.compile(r"[\x00-\x1f\x7f\x85  ]")
# Inline syntax that turns cell or bullet text into something active: an image
# (fetched when rendered), an inline link target, raw HTML / an autolink, and a
# table-cell separator.
_ACTIVE = re.compile(r"!\[|\]\(|<[A-Za-z/!?]|\|")


def _visible_control(match: re.Match[str]) -> str:
    """A control character as visible text (``\\n``, ``\\x1b``, ``\\u2028``)."""
    ch = match.group(0)
    if ch == "\n":
        return "\\n"
    if ch == "\r":
        return "\\r"
    if ch == "\t":
        return "\\t"
    code = ord(ch)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def no_controls(text: str) -> str:
    """``text`` with every control / line-separator character made visible."""
    return _CONTROL.sub(_visible_control, text)


def md_text(value: object) -> str:
    """Inline prose for a table cell or a bullet: one line, no active syntax.

    Control characters become visible escapes (so a value cannot start a new
    line — a heading, a list item, a fence), ``|`` is escaped (it would end the
    cell), and images, link targets and HTML/autolinks are neutralised by
    escaping their opening character. Plain text, including code spans in
    backticks, is returned as is.
    """
    text = str(value)
    if not _CONTROL.search(text) and not _ACTIVE.search(text):
        return text
    text = _CONTROL.sub(_visible_control, text)
    text = text.replace("|", "\\|")
    text = text.replace("![", "!\\[").replace("](", "]\\(")
    return re.sub(r"<(?=[A-Za-z/!?])", "&lt;", text)


def md_code(value: object) -> str:
    """``value`` as an inline code span that it cannot close.

    The fence is a run of backticks longer than any run inside the value
    (CommonMark closes a span only on an equal run), padded with spaces when
    the value starts or ends with a backtick. Control characters become
    visible escapes, and ``|`` is escaped because GFM splits table cells
    before parsing code spans. A plain path gets the usual single backticks.
    """
    text = _CONTROL.sub(_visible_control, str(value)).replace("|", "\\|")
    runs = [len(r) for r in re.findall(r"`+", text)]
    fence = "`" * (max(runs) + 1 if runs else 1)
    if text.startswith("`") or text.endswith("`"):
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def md_fence(body: str, info: str = "") -> tuple[str, str]:
    """Opening and closing fence lines for a fenced block holding ``body``.

    A fence one backtick longer than the longest backtick run in ``body`` (at
    least three): a line of backticks inside the body — a file named ``````` ``,
    a model-written path — can then never close the block, because CommonMark
    requires the closing fence to be at least as long as the opening one.
    """
    runs = [len(r) for r in re.findall(r"`+", body)]
    width = max(3, max(runs) + 1 if runs else 3)
    fence = "`" * width
    return fence + info, fence


def block_lines(text: str) -> str:
    """Text for inside a fenced block: line breaks kept as real ``\\n``, every
    other control/line-separator character made visible (``\\r``, ``\\u2028``)
    so it cannot split a line in a renderer that treats it as a break."""
    if not _CONTROL.search(text.replace("\n", "")):
        return text
    return "\n".join(_CONTROL.sub(_visible_control, line) for line in text.split("\n"))
