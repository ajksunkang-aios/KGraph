"""
Codex CLI target.

  - MCP server → ~/.codex/config.toml as [mcp_servers.kgraph].
  - Global only (Codex has no project-local config concept).
  - No permissions concept.

We write/remove the TOML table by text manipulation (no toml writer in
the 3.10 stdlib). The table block is delimited by its header line
`[mcp_servers.kgraph]` and runs until the next top-level table header
or EOF.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..base import (
    AgentTarget, DetectionResult, FileChange, Location, WriteResult,
)
from ..mcp_config import get_mcp_server_config

_TABLE_HEADER = "[mcp_servers.kgraph]"


def _config_dir() -> Path:
    return Path.home() / ".codex"


def _toml_path() -> Path:
    return _config_dir() / "config.toml"


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _build_table() -> str:
    """Render the [mcp_servers.kgraph] TOML block."""
    cfg = get_mcp_server_config()
    lines = [_TABLE_HEADER]
    lines.append(f'command = "{_toml_escape(cfg["command"])}"')
    args = ", ".join(f'"{_toml_escape(a)}"' for a in cfg["args"])
    lines.append(f"args = [{args}]")
    if cfg.get("env"):
        # Inline env table
        env_items = ", ".join(
            f'"{_toml_escape(k)}" = "{_toml_escape(v)}"'
            for k, v in cfg["env"].items()
        )
        lines.append(f"env = {{ {env_items} }}")
    return "\n".join(lines)


def _find_table_range(content: str) -> tuple[int, int] | None:
    """Return (start, end) char offsets of the kgraph table block, or None."""
    lines = content.splitlines(keepends=True)
    start_line = None
    offset = 0
    offsets = []
    for ln in lines:
        offsets.append(offset)
        offset += len(ln)
    for i, ln in enumerate(lines):
        if ln.strip() == _TABLE_HEADER:
            start_line = i
            break
    if start_line is None:
        return None
    # Table runs until next top-level header [..] or EOF
    end_line = len(lines)
    for j in range(start_line + 1, len(lines)):
        if re.match(r"^\s*\[", lines[j]):
            end_line = j
            break
    start_off = offsets[start_line]
    end_off = offsets[end_line] if end_line < len(lines) else len(content)
    return (start_off, end_off)


class CodexTarget(AgentTarget):
    id = "codex"
    display_name = "Codex CLI"
    docs_url = "https://github.com/openai/codex"

    def supports_location(self, loc: Location) -> bool:
        return loc == "global"

    def detect(self, loc: Location) -> DetectionResult:
        if loc != "global":
            return DetectionResult(False, False)
        path = _toml_path()
        already = False
        if path.exists():
            try:
                already = _TABLE_HEADER in path.read_text()
            except OSError:
                pass
        return DetectionResult(_config_dir().exists(), already, str(path))

    def install(self, loc: Location) -> WriteResult:
        if loc != "global":
            return WriteResult(notes=[
                "Codex CLI has no project-local config — use --location=global."
            ])
        path = _toml_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        created = not existing
        block = _build_table()
        rng = _find_table_range(existing)
        if rng:
            new = existing[:rng[0]] + block + "\n" + existing[rng[1]:]
            action = "updated"
        else:
            sep = "" if not existing or existing.endswith("\n\n") else \
                ("\n" if existing.endswith("\n") else "\n\n")
            new = existing + sep + block + "\n"
            action = "created" if created else "updated"
        if new == existing:
            return WriteResult(files=[FileChange(str(path), "unchanged")])
        path.write_text(new)
        return WriteResult(files=[FileChange(str(path), action)])

    def uninstall(self, loc: Location) -> WriteResult:
        if loc != "global":
            return WriteResult()
        path = _toml_path()
        if not path.exists():
            return WriteResult(files=[FileChange(str(path), "not-found")])
        content = path.read_text()
        rng = _find_table_range(content)
        if not rng:
            return WriteResult(files=[FileChange(str(path), "not-found")])
        new = (content[:rng[0]] + content[rng[1]:]).strip()
        if new:
            path.write_text(new + "\n")
        else:
            path.unlink()
        return WriteResult(files=[FileChange(str(path), "removed")])

    def describe_paths(self, loc: Location) -> list[str]:
        return [str(_toml_path())] if loc == "global" else []


codex_target = CodexTarget()