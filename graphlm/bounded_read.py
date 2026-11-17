"""Bounded reads of scanned files.

Scanned files are untrusted input: their size is whatever the repository
author chose. The scanner keeps only a ``max_file_chars`` slice of each file
and the AST pass needs at most a source file's worth of bytes, so neither
reads past :data:`graphlm.limits.MAX_SOURCE_READ` — a multi-gigabyte log or
dump in the tree costs a bounded amount of memory, not its size.
"""

from __future__ import annotations

from pathlib import Path

_STREAM_CHUNK = 1 << 20


def read_text_bounded(path: Path, limit: int) -> tuple[str, int, bool]:
    """Read at most ``limit`` characters of ``path``: ``(text, lines, complete)``.

    Same decoding as ``Path.read_text(encoding="utf-8", errors="replace")``
    (universal newlines), so a file within the bound gives the identical text
    and ``lines`` (newlines + 1). Past it, ``text`` is the first ``limit``
    characters, ``complete`` is False, and the rest is streamed only to finish
    the physical line count — memory stays bounded by ``limit``.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        head = fh.read(limit + 1)
        if len(head) <= limit:
            return head, head.count("\n") + 1, True
        newlines = head.count("\n")
        while chunk := fh.read(_STREAM_CHUNK):
            newlines += chunk.count("\n")
    return head[:limit], newlines + 1, False


def read_bytes_bounded(path: Path, limit: int) -> bytes:
    """The first ``limit`` bytes of ``path`` (the whole file when smaller)."""
    with open(path, "rb") as fh:
        return fh.read(limit)
