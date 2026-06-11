"""
Hermes Agent target.

  - MCP server → $HERMES_HOME/config.yaml (default ~/.hermes/config.yaml)
    under the top-level `mcp_servers` key, plus a `platform_toolsets.cli`
    entry `mcp-kgraph` so the tools aren't filtered out of CLI sessions.
  - Global only.

We manipulate the YAML as text lines (no pyyaml dependency) so we touch
only our own block and preserve everything else.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..base import (
    AgentTarget, DetectionResult, FileChange, Location, WriteResult,
)
from ..mcp_config import get_mcp_server_config


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    return Path(env).resolve() if env else (Path.home() / ".hermes")


def _config_path() -> Path:
    return _hermes_home() / "config.yaml"


def _render_server_child() -> list[str]:
    cfg = get_mcp_server_config()
    lines = [
        "  kgraph:",
        f"    command: {cfg['command']}",
        "    args:",
    ]
    for a in cfg["args"]:
        lines.append(f"      - {a}")
    if cfg.get("env"):
        lines.append("    env:")
        for k, v in cfg["env"].items():
            lines.append(f"      {k}: {v}")
    lines.append("    enabled: true")
    return lines


def _split(content: str) -> list[str]:
    return content.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _join(lines: list[str]) -> str:
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _top_range(lines: list[str], key: str) -> tuple[int, int] | None:
    """Find [start, end) line range of a top-level `key:` block."""
    start = next((i for i, ln in enumerate(lines) if ln.strip() == f"{key}:"), -1)
    if start == -1:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() == "":
            continue
        # Next top-level key (no indent, ends with colon)
        if ln and not ln[0].isspace() and ln.rstrip().endswith(":"):
            end = i
            break
    return (start, end)


def _child_range(lines: list[str], parent: tuple[int, int], child: str) -> tuple[int, int] | None:
    """Find a 2-space-indented `  child:` block within parent range."""
    ps, pe = parent
    start = -1
    for i in range(ps + 1, pe):
        if lines[i].rstrip() == f"  {child}:":
            start = i
            break
    if start == -1:
        return None
    end = pe
    for i in range(start + 1, pe):
        ln = lines[i]
        if ln.strip() == "":
            continue
        # Next 2-space sibling key
        if ln.startswith("  ") and not ln.startswith("    ") and not ln.lstrip().startswith("- "):
            end = i
            break
    return (start, end)


def _prune_empty_containers(content: str) -> str:
    """
    Drop top-level containers that became empty (or only hold the default
    seed line) after we removed our entry:

      - `mcp_servers:` with no indented children → removed.
      - `platform_toolsets:` whose `cli` holds only `hermes-cli` (the seed
        line our install writes alongside `mcp-kgraph`) → removed. A user
        who added other toolsets keeps the block.

    Conservative: only acts when the container is empty / seed-only.
    """
    lines = _split(content)

    # mcp_servers: empty header
    rng = _top_range(lines, "mcp_servers")
    if rng:
        start, end = rng
        body = [ln for ln in lines[start + 1:end] if ln.strip() != ""]
        if not body:
            del lines[start:end]

    # platform_toolsets: only the seeded `cli: [hermes-cli]`
    rng = _top_range(lines, "platform_toolsets")
    if rng:
        start, end = rng
        body = [ln.strip() for ln in lines[start + 1:end] if ln.strip() != ""]
        if body == ["cli:", "- hermes-cli"]:
            del lines[start:end]

    return _join(lines) if any(ln.strip() for ln in lines) else ""


class HermesTarget(AgentTarget):
    id = "hermes"
    display_name = "Hermes Agent"
    docs_url = "https://hermes-agent.nousresearch.com"

    def supports_location(self, loc: Location) -> bool:
        return loc == "global"

    def detect(self, loc: Location) -> DetectionResult:
        if loc != "global":
            return DetectionResult(False, False)
        path = _config_path()
        content = path.read_text() if path.exists() else ""
        installed = _hermes_home().exists() or path.exists()
        lines = _split(content)
        parent = _top_range(lines, "mcp_servers")
        already = bool(parent and _child_range(lines, parent, "kgraph"))
        return DetectionResult(installed, already, str(path))

    def install(self, loc: Location) -> WriteResult:
        if loc != "global":
            return WriteResult(notes=[
                "Hermes uses $HERMES_HOME/config.yaml — use --location=global."
            ])
        path = _config_path()
        before = path.read_text() if path.exists() else ""
        existed = path.exists()
        after = self._upsert_toolset(self._upsert_server(before))
        if after == before:
            return WriteResult(files=[FileChange(str(path), "unchanged")])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after if after.endswith("\n") else after + "\n")
        return WriteResult(
            files=[FileChange(str(path), "updated" if existed else "created")],
            notes=["Start a new Hermes session for MCP changes to take effect."],
        )

    def uninstall(self, loc: Location) -> WriteResult:
        if loc != "global":
            return WriteResult()
        path = _config_path()
        if not path.exists():
            return WriteResult(files=[FileChange(str(path), "not-found")])
        before = path.read_text()
        after = self._remove_toolset(self._remove_server(before))
        after = _prune_empty_containers(after)
        if after == before:
            return WriteResult(files=[FileChange(str(path), "not-found")])
        if after.strip() == "":
            path.unlink()
        else:
            path.write_text(after if after.endswith("\n") else after + "\n")
        return WriteResult(files=[FileChange(str(path), "removed")])

    def describe_paths(self, loc: Location) -> list[str]:
        return [str(_config_path())] if loc == "global" else []

    # ── YAML block ops ──

    def _upsert_server(self, content: str) -> str:
        lines = _split(content)
        parent = _top_range(lines, "mcp_servers")
        repl = _render_server_child()
        if not parent:
            if lines and lines[-1] == "":
                lines.pop()
            if lines:
                lines.append("")
            lines.append("mcp_servers:")
            lines.extend(repl)
            return _join(lines)
        child = _child_range(lines, parent, "kgraph")
        if child:
            lines[child[0]:child[1]] = repl
        else:
            lines[parent[1]:parent[1]] = repl
        return _join(lines)

    def _remove_server(self, content: str) -> str:
        lines = _split(content)
        parent = _top_range(lines, "mcp_servers")
        if not parent:
            return content
        child = _child_range(lines, parent, "kgraph")
        if not child:
            return content
        del lines[child[0]:child[1]]
        return _join(lines)

    def _upsert_toolset(self, content: str) -> str:
        lines = _split(content)
        parent = _top_range(lines, "platform_toolsets")
        if not parent:
            if lines and lines[-1] == "":
                lines.pop()
            if lines:
                lines.append("")
            lines.extend(["platform_toolsets:", "  cli:",
                          "    - hermes-cli", "    - mcp-kgraph"])
            return _join(lines)
        cli = _child_range(lines, parent, "cli")
        if not cli:
            lines[parent[1]:parent[1]] = ["  cli:", "    - hermes-cli", "    - mcp-kgraph"]
            return _join(lines)
        if any(lines[i].strip() == "- mcp-kgraph" for i in range(cli[0] + 1, cli[1])):
            return _join(lines)
        lines[cli[1]:cli[1]] = ["    - mcp-kgraph"]
        return _join(lines)

    def _remove_toolset(self, content: str) -> str:
        lines = _split(content)
        parent = _top_range(lines, "platform_toolsets")
        if not parent:
            return content
        cli = _child_range(lines, parent, "cli")
        if not cli:
            return content
        kept = [ln for idx, ln in enumerate(lines)
                if not (cli[0] < idx < cli[1] and ln.strip() == "- mcp-kgraph")]
        return _join(kept)


hermes_target = HermesTarget()