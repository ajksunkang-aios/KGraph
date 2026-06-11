"""
opencode target.

  - MCP server → ~/.config/opencode/opencode.json (global, XDG) or
    ./opencode.json (local).
  - Shape differs from Claude/Cursor — uses `mcp.<name>` with a
    string-array `command` and an explicit `enabled` flag:
        {
          "$schema": "https://opencode.ai/config.json",
          "mcp": { "kgraph": {
            "type": "local",
            "command": ["kgraph", "serve", "--mcp"],
            "environment": {...},
            "enabled": true
          }}
        }
  - Detect via ~/.config/opencode dir.

We read/write .json (not .jsonc) — comment preservation is out of scope
for the MVP. Prefers an existing .jsonc/.json file if present.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..base import (
    AgentTarget, DetectionResult, FileChange, Location, WriteResult,
)
from ..mcp_config import get_command_array, get_mcp_server_config, read_json, write_json

_SCHEMA = "https://opencode.ai/config.json"


def _global_dir() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "opencode"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "opencode"


def _config_base(loc: Location) -> Path:
    return _global_dir() if loc == "global" else Path.cwd()


def _config_path(loc: Location) -> Path:
    """Prefer existing .jsonc/.json, default to .json for new installs."""
    d = _config_base(loc)
    jsonc = d / "opencode.jsonc"
    js = d / "opencode.json"
    if jsonc.exists():
        return jsonc
    if js.exists():
        return js
    return js


def _server_entry() -> dict:
    cfg = get_mcp_server_config()
    return {
        "type": "local",
        "command": get_command_array(),
        "environment": cfg.get("env", {}),
        "enabled": True,
    }


class OpencodeTarget(AgentTarget):
    id = "opencode"
    display_name = "opencode"
    docs_url = "https://opencode.ai/docs/config"

    def supports_location(self, loc: Location) -> bool:
        return True

    def detect(self, loc: Location) -> DetectionResult:
        path = _config_path(loc)
        config = read_json(path)
        already = bool(config.get("mcp", {}).get("kgraph"))
        installed = _global_dir().exists() if loc == "global" else path.exists()
        return DetectionResult(installed, already, str(path))

    def install(self, loc: Location) -> WriteResult:
        path = _config_path(loc)
        config = read_json(path)
        existed = path.exists()
        before = config.get("mcp", {}).get("kgraph")
        after = _server_entry()
        if before == after:
            return WriteResult(files=[FileChange(str(path), "unchanged")])
        config.setdefault("$schema", _SCHEMA)
        config.setdefault("mcp", {})["kgraph"] = after
        write_json(path, config)
        return WriteResult(files=[FileChange(str(path), "updated" if existed else "created")])

    def uninstall(self, loc: Location) -> WriteResult:
        path = _config_path(loc)
        config = read_json(path)
        if not config.get("mcp", {}).get("kgraph"):
            return WriteResult(files=[FileChange(str(path), "not-found")])
        del config["mcp"]["kgraph"]
        if not config["mcp"]:
            del config["mcp"]
        # If nothing but our seeded $schema remains, the file is ours — delete it.
        if set(config.keys()) <= {"$schema"}:
            path.unlink()
            return WriteResult(files=[FileChange(str(path), "removed")])
        write_json(path, config)
        return WriteResult(files=[FileChange(str(path), "removed")])

    def describe_paths(self, loc: Location) -> list[str]:
        return [str(_config_path(loc))]


opencode_target = OpencodeTarget()