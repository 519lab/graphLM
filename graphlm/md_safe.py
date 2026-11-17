"""Make repository- and model-derived text inert in the Markdown map.

``GRAPH.md`` is read by coding agents and rendered by GitHub, IDEs and agent
UIs. Almost everything in it comes from the scanned repository (file names) or
from an LLM that read that repository (descriptions, notes, paths, symbol
names, edge endpoints) — so a hostile repository can try to write Markdown
structure into it: close a code fence, start a heading or a list, add an image
that fetches an attacker URL when rendered, inject raw HTML, or hide text from
a human reader with invisible characters.

Every renderer passes such text through one of these helpers. Each one returns
its input **unchanged** when the input holds nothing that could change the
document's structure, so ordinary maps stay byte-identical; only hostile or
unusual values are altered. The helpers make text *inert*, they do not make it
*trustworthy*: an instruction written in plain words is still just words, which
is why the map also says, where an agent reads it, that its text is data.
"""

from __future__ import annotations

import re

# Characters that end or split a line (CommonMark, tokenizers, editors), other
# C0/C1 controls, and invisible/bidi characters that hide or reorder text for a
# human reader (zero-width, soft hyphen, BOM, bidi embeddings/isolates, Unicode
# tag characters — a known invisible prompt-injection channel). Tab is allowed:
# it neither breaks a line nor hides text.
_CONTROL = re.compile(
    # Every C0 control except tab. One range from \x00 rather than two around
    # \t, so the class reads as intended (CodeQL py/overly-large-range).
    r"(?!\t)[\x00-\x1f\x7f\x85\xad\u200b-\u200f\u2028-\u202e"
    r"\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]"
)
# Inline syntax that turns text into something active: an image (fetched when
# rendered), an inline or reference link, raw HTML / an autolink.
_ACTIVE_INLINE = re.compile(r"!\[|\]\(|\]\[|<[A-Za-z/!?]")
# Block syntax that is live at the start of a paragraph or bullet: ATX heading,
# block quote, list item, ordered list item, thematic break / setext underline,
# fence, link reference definition, HTML block.
# Indentation that starts an indented code block (4+ columns; a tab is 4).
_CODE_INDENT = re.compile(r"^(?: {4,}| {0,3}\t)")
_BLOCK_START = re.compile(
    r"^( {0,3})(?:"
    r"(#{1,6})(?=[ \t]|$)"
    r"|(>)"
    r"|([-+*])(?=[ \t]|$)"
    r"|\d{1,9}([.)])(?=[ \t]|$)"
    r"|(`{3,}|~{3,})"
    r"|([-*_=])(?=(?:[ \t]*[-*_=]){2,}[ \t]*$)"
    r"|(\[)[^\]]*\]:"
    r")"
)


def _visible_control(match: re.Match[str]) -> str:
    """A control character as visible text (``\\n``, ``\\x1b``, ``\\u2028``)."""
    ch = match.group(0)
    if ch == "\n":
        return "\\n"
    if ch == "\r":
        return "\\r"
    code = ord(ch)
    if code < 0x100:
        return f"\\x{code:02x}"
    if code < 0x10000:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def no_controls(text: str) -> str:
    """``text`` with every control, line-separator and invisible character
    replaced by a visible escape."""
    return _CONTROL.sub(_visible_control, text)


def _escape_block_start(text: str) -> str:
    """Backslash-escape a block marker at the very start of ``text``."""
    m = _BLOCK_START.match(text)
    if m is None:
        return text
    # The marker group that matched; escape its first character.
    for group in range(2, 9):
        if m.group(group) is not None:
            at = m.start(group)
            return text[:at] + "\\" + text[at:]
    return text


def md_text(value: object, *, cell: bool = False) -> str:
    """Inline prose for a bullet, paragraph or (``cell=True``) table cell.

    One line (control/invisible characters become visible escapes), no block
    marker at the start (a ``#``, ``>``, ``-``, ``1.``, fence, rule or
    ``[ref]:`` is escaped), and no active inline syntax (images, inline and
    reference links, HTML/autolinks are neutralised). In a table cell ``|`` is
    escaped too — elsewhere it is plain text. Ordinary text, including code
    spans in backticks, is returned as is.
    """
    text = str(value)
    if _CODE_INDENT.match(text):
        # Leading indentation means nothing in one-line prose, but 4+ columns
        # would open an indented code block (or hide a heading inside a
        # bullet). Drop it; the block-marker check then applies to the rest.
        text = text.lstrip(" \t")
    if (
        not _CONTROL.search(text)
        and not _ACTIVE_INLINE.search(text)
        and not _BLOCK_START.match(text)
        and not (cell and "|" in text)
    ):
        return text
    text = no_controls(text)
    if cell:
        text = text.replace("|", "\\|")
    text = text.replace("![", "!\\[").replace("](", "]\\(").replace("][", "]\\[")
    text = re.sub(r"<(?=[A-Za-z/!?])", "&lt;", text)
    return _escape_block_start(text)


def md_code(value: object, *, cell: bool = False) -> str:
    """``value`` as an inline code span that it cannot close.

    The fence is a run of backticks longer than any run inside the value
    (CommonMark closes a span only on an equal run), padded with spaces when
    the value starts or ends with a backtick. Control/invisible characters
    become visible escapes. In a table cell (``cell=True``) ``|`` is escaped,
    because GFM splits cells before parsing code spans. A plain path gets the
    usual single backticks.
    """
    text = no_controls(str(value))
    if cell:
        text = text.replace("|", "\\|")
    runs = [len(r) for r in re.findall(r"`+", text)]
    fence = "`" * (max(runs) + 1 if runs else 1)
    if text.startswith("`") or text.endswith("`"):
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def md_fence(body: str, info: str = "") -> tuple[str, str]:
    """Opening and closing fence lines for a fenced block holding ``body``.

    A fence one backtick longer than the longest backtick run in ``body`` (at
    least three): a line of backticks inside the body — a file named
    ``` ``` ```, a model-written path — can then never close the block,
    because CommonMark requires the closing fence to be at least as long as
    the opening one.
    """
    runs = [len(r) for r in re.findall(r"`+", body)]
    width = max(3, max(runs) + 1 if runs else 3)
    fence = "`" * width
    return fence + info, fence


def block_lines(text: str) -> str:
    """Text for inside a fenced block: real ``\\n`` line breaks kept, every
    other control/line-separator/invisible character made visible."""
    if not _CONTROL.search(text.replace("\n", "")):
        return text
    return "\n".join(no_controls(line) for line in text.split("\n"))
