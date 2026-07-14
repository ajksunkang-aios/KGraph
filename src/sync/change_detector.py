"""
Change detection for lazy-indexing (sync flow, P2).

Given a compile_commands.json and a baseline timestamp, finds which translation
units (TUs) were rebuilt after the baseline — by checking the mtime of each TU's
output `.o` file. Also generates a filtered compile_commands.json containing only
those TUs, ready to feed to scip-clang for a partial index.

Key insight: make already computed the dependency closure. The `.o` files whose
mtime changed ARE the set of TUs that need re-indexing. No separate dependency
analysis needed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

# Extract the -o <path>.o token from a compile command string
_O_RE = re.compile(r"\s-o\s+(\S+\.o)\b")


def parse_compile_commands(cc_path: Path) -> list[dict]:
    """Load and return the compile_commands.json entries."""
    with open(cc_path) as f:
        return json.load(f)


def extract_obj_path(entry: dict) -> Optional[str]:
    """Extract the `-o <path>.o` token from a compdb entry.

    Handles both formats: `command` (string) and `arguments` (list).
    Returns the .o path (relative to the build directory), or None.
    """
    if "command" in entry:
        m = _O_RE.search(entry["command"])
        return m.group(1) if m else None
    if "arguments" in entry:
        args = entry["arguments"]
        for i, arg in enumerate(args):
            if arg == "-o" and i + 1 < len(args):
                return args[i + 1]
    return None


def _try_stat_obj(kernel_root: Path, o_rel: str) -> Optional[float]:
    """Try to find the .o on disk and return its mtime.

    The compdb `directory` may be a stale build-container path (e.g. /workspace);
    we ignore it and rebase onto the real kernel_root. For absolute paths, we try
    stripping leading components until the remainder exists under kernel_root.
    Returns None if the .o can't be found (e.g. not built under current config).
    """
    o = Path(o_rel)
    candidates: list[Path] = []
    if not o.is_absolute():
        candidates.append(kernel_root / o)
    else:
        # Absolute (container path like /workspace/fs/ext4/file.o) — try stripping
        # leading components until the remainder exists under kernel_root.
        parts = o.parts[1:]  # strip leading /
        for i in range(min(len(parts), 5)):
            candidates.append(kernel_root / Path(*parts[i:]))
    for c in candidates:
        try:
            return os.path.getmtime(c)
        except OSError:
            continue
    return None


def find_rebuilt_tus(entries: list[dict], kernel_root: Path,
                     baseline_ts: float) -> list[dict]:
    """Find compdb entries whose .o was rebuilt after the baseline timestamp.

    Args:
        entries: compile_commands.json entries (from parse_compile_commands).
        kernel_root: the real kernel source root on disk.
        baseline_ts: the last index timestamp (meta.index_timestamp).

    Returns the subset of entries whose .o mtime > baseline_ts.
    """
    targets = []
    for entry in entries:
        o_rel = extract_obj_path(entry)
        if not o_rel:
            continue
        mtime = _try_stat_obj(kernel_root, o_rel)
        if mtime is not None and mtime > baseline_ts:
            targets.append(entry)
    return targets


def write_filtered_compdb(targets: list[dict], out_path: Path) -> None:
    """Write a filtered compile_commands.json containing only the target entries.

    The entries are written verbatim (same {directory, command, file} structure),
    ready to be fed to `scip-clang --compdb-path`.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(targets, f, indent=2)


def detect_and_filter(
    cc_path: Path,
    kernel_root: Path,
    baseline_ts: float,
    out_path: Optional[Path] = None,
) -> list[dict]:
    """One-call: parse compdb → find rebuilt TUs → optionally write filtered compdb.

    Returns the target entries (subset of compdb whose .o was rebuilt).
    """
    entries = parse_compile_commands(cc_path)
    targets = find_rebuilt_tus(entries, kernel_root, baseline_ts)
    if out_path and targets:
        write_filtered_compdb(targets, out_path)
    return targets
