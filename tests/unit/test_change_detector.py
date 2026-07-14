"""
Tests for src/sync/change_detector.py — the P2 change detection for lazy-indexing.

Validates: parse compile_commands, find rebuilt TUs by .o mtime, write filtered
compdb, path rebasing (absolute container paths).
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

# Make src/ importable
_HERE = Path(__file__).resolve()
_SRC = _HERE.parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sync.change_detector import (
    detect_and_filter,
    extract_obj_path,
    find_rebuilt_tus,
    parse_compile_commands,
    write_filtered_compdb,
)


def _make_compdb(entries: list[dict], path: Path) -> Path:
    path.write_text(json.dumps(entries))
    return path


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    os.utime(path, (mtime, mtime))


class TestExtractObjPath:
    def test_from_command_string(self):
        e = {"command": "clang -c -o fs/ext4/file.o -Iinclude fs/ext4/file.c"}
        assert extract_obj_path(e) == "fs/ext4/file.o"

    def test_from_arguments_list(self):
        e = {"arguments": ["clang", "-c", "-o", "fs/ext4/file.o", "fs/ext4/file.c"]}
        assert extract_obj_path(e) == "fs/ext4/file.o"

    def test_missing_o(self):
        e = {"command": "clang -c fs/ext4/file.c"}
        assert extract_obj_path(e) is None

    def test_empty(self):
        assert extract_obj_path({}) is None


class TestFindRebuiltTus:
    def test_finds_new_not_old(self, tmp_path):
        now = time.time()
        old_ts, new_ts = now - 100, now + 10
        baseline = now

        _touch(tmp_path / "fs/ext4/inode.o", old_ts)   # old: not rebuilt
        _touch(tmp_path / "fs/ext4/file.o", new_ts)     # new: rebuilt

        cc = _make_compdb([
            {"directory": str(tmp_path),
             "command": f"clang -c -o fs/ext4/inode.o fs/ext4/inode.c",
             "file": "fs/ext4/inode.c"},
            {"directory": str(tmp_path),
             "command": f"clang -c -o fs/ext4/file.o fs/ext4/file.c",
             "file": "fs/ext4/file.c"},
        ], tmp_path / "compile_commands.json")

        entries = parse_compile_commands(cc)
        targets = find_rebuilt_tus(entries, tmp_path, baseline)
        assert len(targets) == 1
        assert "file.o" in targets[0]["command"]

    def test_missing_o_skipped(self, tmp_path):
        """A TU whose .o doesn't exist (not built under this config) is skipped."""
        now = time.time()
        _touch(tmp_path / "fs/ext4/file.o", now + 10)

        cc = _make_compdb([
            {"directory": str(tmp_path),
             "command": "clang -c -o fs/ext4/file.o fs/ext4/file.c",
             "file": "fs/ext4/file.c"},
            {"directory": str(tmp_path),
             "command": "clang -c -o drivers/missing.o drivers/missing.c",
             "file": "drivers/missing.c"},  # .o doesn't exist
        ], tmp_path / "compile_commands.json")

        entries = parse_compile_commands(cc)
        targets = find_rebuilt_tus(entries, tmp_path, now)
        assert len(targets) == 1  # only file.o, not missing.o

    def test_all_up_to_date(self, tmp_path):
        """No .o newer than baseline → empty targets."""
        now = time.time()
        _touch(tmp_path / "fs/ext4/file.o", now - 100)

        cc = _make_compdb([
            {"directory": str(tmp_path),
             "command": "clang -c -o fs/ext4/file.o fs/ext4/file.c",
             "file": "fs/ext4/file.c"},
        ], tmp_path / "compile_commands.json")

        targets = find_rebuilt_tus(parse_compile_commands(cc), tmp_path, now)
        assert targets == []

    def test_absolute_o_path_rebase(self, tmp_path):
        """Absolute .o path (build-container like /workspace/...) is rebased onto
        the real kernel_root by stripping leading components."""
        now = time.time()
        # .o on disk at tmp_path/fs/ext4/file.o
        _touch(tmp_path / "fs/ext4/file.o", now + 10)

        # compdb says -o /workspace/fs/ext4/file.o (container path)
        cc = _make_compdb([
            {"directory": "/workspace",
             "command": "clang -c -o /workspace/fs/ext4/file.o /workspace/fs/ext4/file.c",
             "file": "/workspace/fs/ext4/file.c"},
        ], tmp_path / "compile_commands.json")

        targets = find_rebuilt_tus(parse_compile_commands(cc), tmp_path, now)
        assert len(targets) == 1


class TestWriteFilteredCompdb:
    def test_writes_json_subset(self, tmp_path):
        targets = [
            {"directory": "/k", "command": "clang -o a.o a.c", "file": "a.c"},
            {"directory": "/k", "command": "clang -o b.o b.c", "file": "b.c"},
        ]
        out = tmp_path / "filtered.json"
        write_filtered_compdb(targets, out)
        loaded = json.loads(out.read_text())
        assert loaded == targets
        assert len(loaded) == 2


class TestDetectAndFilter:
    def test_end_to_end(self, tmp_path):
        now = time.time()
        _touch(tmp_path / "a.o", now - 100)     # old
        _touch(tmp_path / "b.o", now + 10)      # new

        cc = _make_compdb([
            {"directory": str(tmp_path), "command": "clang -o a.o a.c", "file": "a.c"},
            {"directory": str(tmp_path), "command": "clang -o b.o b.c", "file": "b.c"},
        ], tmp_path / "compile_commands.json")

        out = tmp_path / "filtered.json"
        targets = detect_and_filter(cc, tmp_path, now, out_path=out)
        assert len(targets) == 1
        assert "b.o" in targets[0]["command"]
        # filtered compdb written
        loaded = json.loads(out.read_text())
        assert len(loaded) == 1
        assert "b.o" in loaded[0]["command"]

    def test_no_targets_no_write(self, tmp_path):
        """If nothing changed, no filtered compdb is written."""
        now = time.time()
        _touch(tmp_path / "a.o", now - 100)

        cc = _make_compdb([
            {"directory": str(tmp_path), "command": "clang -o a.o a.c", "file": "a.c"},
        ], tmp_path / "compile_commands.json")

        out = tmp_path / "filtered.json"
        targets = detect_and_filter(cc, tmp_path, now, out_path=out)
        assert targets == []
        assert not out.exists()
