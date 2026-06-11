"""
KGraph MCP — Source code reader.

Reads the actual source body of a symbol from disk, given its
definition location (file path + line range) from the graph DB.

The graph stores definition line ranges; this module joins that
with the on-disk source tree to return real code text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def read_source_range(project_root: Path, rel_path: str,
                      start_line: int, end_line: int,
                      context: int = 0) -> Optional[str]:
    """
    Read a source-line range from disk.

    Args:
        project_root: kernel source tree root (where rel_path is relative to)
        rel_path: file path relative to project_root (e.g. "fs/ext4/file.c")
        start_line: 0-based start line (SCIP convention)
        end_line: 0-based end line (inclusive)
        context: extra lines before/after to include

    Returns the source text, or None if the file can't be read.
    Line numbers in the returned text are NOT prefixed — caller adds them.
    """
    if start_line < 0:
        return None

    abs_path = project_root / rel_path
    if not abs_path.is_file():
        return None

    try:
        with open(abs_path, "r", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    # SCIP lines are 0-based; convert to list indices
    start = max(0, start_line - context)
    end = min(len(lines), end_line + 1 + context)
    if start >= len(lines):
        return None

    return "".join(lines[start:end])


def read_source_with_lineno(project_root: Path, rel_path: str,
                            start_line: int, end_line: int,
                            context: int = 0) -> Optional[str]:
    """
    Like read_source_range, but prefixes each line with its 1-based number,
    matching the convention agents expect (file:line references).
    """
    text = read_source_range(project_root, rel_path, start_line, end_line, context)
    if text is None:
        return None

    out = []
    lineno = max(1, start_line + 1 - context)  # 1-based display
    for line in text.splitlines():
        out.append(f"{lineno:6d}\t{line}")
        lineno += 1
    return "\n".join(out)