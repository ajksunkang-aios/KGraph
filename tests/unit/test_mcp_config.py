"""
Unit tests for installer/mcp_config.py — JSONC-safe read_json.

Regression guard for the opencode overwrite bug: read_json must tolerate
JSONC (// comments, /* */ block comments, trailing commas) so an annotated
agent config is read correctly instead of as ``{}`` — which previously
caused ``kgraph install`` to clobber the whole file with only the kgraph
entry.
"""

from __future__ import annotations

import json
from pathlib import Path

from installer.mcp_config import _strip_jsonc, read_json


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


class TestReadJsonMissing:
    def test_missing_file_returns_empty(self, tmp_path):
        assert read_json(tmp_path / "nope.json") == {}


class TestJsoncTolerance:
    def test_line_comment(self, tmp_path):
        p = _write(tmp_path / "a.json", '{\n  "x": 1,  // a comment\n  "y": 2\n}\n')
        assert read_json(p) == {"x": 1, "y": 2}

    def test_block_comment(self, tmp_path):
        p = _write(tmp_path / "a.json", '{/* block */ "x": 1}')
        assert read_json(p) == {"x": 1}

    def test_trailing_comma_object(self, tmp_path):
        p = _write(tmp_path / "a.json", '{"x": 1, "y": 2,}')
        assert read_json(p) == {"x": 1, "y": 2}

    def test_trailing_comma_array(self, tmp_path):
        p = _write(tmp_path / "a.json", '{"y": [1, 2, 3,]}')
        assert read_json(p) == {"y": [1, 2, 3]}

    def test_string_with_double_slash_preserved(self, tmp_path):
        # '//' inside a string literal must NOT be treated as a comment
        p = _write(tmp_path / "a.json", '{"url": "http://example.com/path", "a": 1}')
        assert read_json(p)["url"] == "http://example.com/path"

    def test_escaped_quote_in_string(self, tmp_path):
        # a "// ..." sequence after an escaped quote is still inside the string
        p = _write(tmp_path / "a.json", '{"msg": "she said \\"hi\\" // not a comment"}')
        assert read_json(p)["msg"] == 'she said "hi" // not a comment'

    def test_plain_json_unchanged(self, tmp_path):
        p = _write(tmp_path / "a.json", '{"plain": true}')
        assert read_json(p) == {"plain": True}


class TestPreservesExistingConfig:
    """Core of the opencode overwrite bug: an existing MCP server + $schema
    in a commented config must survive read, so install merges instead of
    overwriting."""

    def test_existing_mcp_server_preserved(self, tmp_path):
        opencode_jsonc = (
            '{\n'
            '  "$schema": "https://opencode.ai/config.json",\n'
            '  // my existing filesystem server\n'
            '  "mcp": {\n'
            '    "filesystem": {"type": "local", "command": ["npx", "-y", "fs"], "enabled": true,},\n'
            '  }\n'
            '}\n'
        )
        p = _write(tmp_path / "opencode.jsonc", opencode_jsonc)
        r = read_json(p)
        assert r["$schema"] == "https://opencode.ai/config.json"
        assert r["mcp"]["filesystem"]["command"] == ["npx", "-y", "fs"]


class TestStripJsoncDirect:
    def test_output_is_valid_json(self):
        stripped = _strip_jsonc('{// c\n"a":1,/* b */"b":2,}')
        assert json.loads(stripped) == {"a": 1, "b": 2}

    def test_no_comment_markers_remain(self):
        stripped = _strip_jsonc('{// c\n"a":1/* b */}')
        assert "//" not in stripped
        assert "/*" not in stripped

    def test_passes_through_plain_json(self):
        assert _strip_jsonc('{"a":1}') == '{"a":1}'
