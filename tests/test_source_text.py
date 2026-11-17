"""graphlm's own source stays readable: the control-character class matches
exactly what it documents, and no source file hides characters a reviewer
cannot see."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from graphlm.md_safe import _CONTROL

ROOT = Path(__file__).resolve().parent.parent

# What md_safe documents: every C0 control but tab, DEL, NEL, soft hyphen,
# zero-width and bidi marks, line/paragraph separators, bidi embeddings and
# isolates, word joiner and invisible operators, BOM, and the tag block.
_EXPECTED = (
    {c for c in range(0x20) if c != 0x09}
    | {0x7F, 0x85, 0xAD, 0xFEFF}
    | set(range(0x200B, 0x2010))
    | set(range(0x2028, 0x202F))
    | set(range(0x2060, 0x2065))
    | set(range(0x2066, 0x206A))
    | set(range(0xE0000, 0xE0080))
)

# Characters that render as nothing (or reorder text) in an editor or a diff.
_INVISIBLE = re.compile(
    "[\u00ad\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\U000e0000-\U000e007f]"
)


def test_control_class_matches_exactly_the_documented_set():
    matched = {c for c in range(sys.maxunicode + 1) if _CONTROL.fullmatch(chr(c))}
    assert matched == _EXPECTED


def test_tab_is_not_a_control():
    assert _CONTROL.search("a\tb") is None


def test_source_files_have_no_literal_invisible_characters():
    files = [
        *ROOT.joinpath("graphlm").rglob("*.py"),
        *ROOT.joinpath("graphlm").rglob("*.html"),
        *ROOT.joinpath("tests").glob("*.py"),
    ]
    assert files
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in files
        for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if _INVISIBLE.search(line)
    ]
    # Write such characters as escapes (``"\u2028"``) so a reviewer can see them.
    assert offenders == []
