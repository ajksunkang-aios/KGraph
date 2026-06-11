"""
Cursor target.

  - MCP server → ~/.cursor/mcp.json (global) or ./.cursor/mcp.json
    (local), shape {mcpServers: {kgraph: {...}}}
  - Detect via ~/.cursor dir.
  - No permissions concept.
"""

from __future__ import annotations

from pathlib import Path

from ..base import (
    AgentTarget, DetectionResult, FileChange, Location, WriteResult,
)
from ..mcp_config import get_mcp_server_config, read_json, write_json


def _mcp_path(loc: Location) -> Path:
    return (Path.home() / ".cursor" / "mcp.json") if loc == "global" \
        else (Path.cwd() / ".cursor" / "mcp.json")


class CursorTarget(AgentTarget):
    id = "cursor"
    display_name = "Cursor"
    docs_url = "https://docs.cursor.com/context/model-context-protocol"

    def supports_location(self, loc: Location) -> bool:
        return True

    def detect(self, loc: Location) -> DetectionResult:
        path = _mcp_path(loc)
        config = read_json(path)
        already = bool(config.get("mcpServers", {}).get("kgraph"))
        base = (Path.home() / ".cursor") if loc == "global" else (Path.cwd() / ".cursor")
        return DetectionResult(base.exists(), already, str(path))

    def install(self, loc: Location) -> WriteResult:
        return WriteResult(
            files=[self._write_mcp(loc)],
            notes=["Restart Cursor for MCP changes to take effect."],
        )

    def uninstall(self, loc: Location) -> WriteResult:
        path = _mcp_path(loc)
        config = read_json(path)
        if config.get("mcpServers", {}).get("kgraph"):
            del config["mcpServers"]["kgraph"]
            if not config["mcpServers"]:
                del config["mcpServers"]
            write_json(path, config)
            return WriteResult(files=[FileChange(str(path), "removed")])
        return WriteResult(files=[FileChange(str(path), "not-found")])

    def describe_paths(self, loc: Location) -> list[str]:
        return [str(_mcp_path(loc))]

    def _write_mcp(self, loc: Location) -> FileChange:
        path = _mcp_path(loc)
        config = read_json(path)
        before = config.get("mcpServers", {}).get("kgraph")
        after = get_mcp_server_config()
        if before == after:
            return FileChange(str(path), "unchanged")
        action = "updated" if (before or path.exists()) else "created"
        config.setdefault("mcpServers", {})["kgraph"] = after
        write_json(path, config)
        return FileChange(str(path), action)


cursor_target = CursorTarget()