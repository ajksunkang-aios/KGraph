"""
Claude Code target.

  - MCP server → ~/.claude.json (global) or ./.mcp.json (local),
    shape {mcpServers: {kgraph: {...}}}
  - Permissions → ~/.claude/settings.json (global) or
    ./.claude/settings.json (local), permissions.allow array
  - Detect via ~/.claude dir or the MCP file existing.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..base import (
    AgentTarget, DetectionResult, FileChange, Location, WriteResult,
)
from ..mcp_config import (
    get_claude_permissions, get_mcp_server_config, read_json, write_json,
)


def _config_dir(loc: Location) -> Path:
    return (Path.home() / ".claude") if loc == "global" else (Path.cwd() / ".claude")


def _mcp_path(loc: Location) -> Path:
    # global → ~/.claude.json (user scope); local → ./.mcp.json (project scope).
    return (Path.home() / ".claude.json") if loc == "global" else (Path.cwd() / ".mcp.json")


def _settings_path(loc: Location) -> Path:
    return _config_dir(loc) / "settings.json"


class ClaudeTarget(AgentTarget):
    id = "claude"
    display_name = "Claude Code"
    docs_url = "https://docs.claude.com/en/docs/claude-code"

    def supports_location(self, loc: Location) -> bool:
        return True

    def detect(self, loc: Location) -> DetectionResult:
        mcp_path = _mcp_path(loc)
        config = read_json(mcp_path)
        already = bool(config.get("mcpServers", {}).get("kgraph"))
        installed = _config_dir(loc).exists() or mcp_path.exists()
        return DetectionResult(installed, already, str(mcp_path))

    def install(self, loc: Location) -> WriteResult:
        files = [self._write_mcp(loc), self._write_permissions(loc)]
        return WriteResult(files=files)

    def uninstall(self, loc: Location) -> WriteResult:
        files = []
        # 1. MCP entry
        mcp_path = _mcp_path(loc)
        config = read_json(mcp_path)
        if config.get("mcpServers", {}).get("kgraph"):
            del config["mcpServers"]["kgraph"]
            if not config["mcpServers"]:
                del config["mcpServers"]
            write_json(mcp_path, config)
            files.append(FileChange(str(mcp_path), "removed"))
        else:
            files.append(FileChange(str(mcp_path), "not-found"))
        # 2. Permissions
        sp = _settings_path(loc)
        settings = read_json(sp)
        allow = settings.get("permissions", {}).get("allow")
        if isinstance(allow, list):
            kept = [p for p in allow if not p.startswith("mcp__kgraph__")]
            if len(kept) != len(allow):
                if kept:
                    settings["permissions"]["allow"] = kept
                else:
                    del settings["permissions"]["allow"]
                    if not settings["permissions"]:
                        del settings["permissions"]
                write_json(sp, settings)
                files.append(FileChange(str(sp), "removed"))
            else:
                files.append(FileChange(str(sp), "not-found"))
        else:
            files.append(FileChange(str(sp), "not-found"))
        return WriteResult(files=files)

    def describe_paths(self, loc: Location) -> list[str]:
        return [str(_mcp_path(loc)), str(_settings_path(loc))]

    # ── internal ──

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

    def _write_permissions(self, loc: Location) -> FileChange:
        path = _settings_path(loc)
        settings = read_json(path)
        created = not path.exists()
        perms = settings.setdefault("permissions", {})
        allow = perms.setdefault("allow", [])
        before = list(allow)
        for p in get_claude_permissions():
            if p not in allow:
                allow.append(p)
        if allow == before and not created:
            return FileChange(str(path), "unchanged")
        write_json(path, settings)
        return FileChange(str(path), "created" if created else "updated")


claude_target = ClaudeTarget()