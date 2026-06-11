"""
KGraph installer — shared MCP server config + JSON helpers.

`get_mcp_server_config()` is the single source of truth for how every
agent launches the kgraph MCP server. Two launch modes:

  1. `kgraph` on PATH (installed via install.sh):
        command="kgraph", args=["serve", "--mcp"]
  2. dev / not-on-PATH fallback:
        command=<venv python>, args=[<server.py path>]

The kernel project root is passed via the KGRAPH_ROOT env var so the
server resolves ./.kgraph/kgraph.db relative to it — making `kgraph
install` per-project (run it inside the kernel source dir).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

# Repo layout: src/installer/mcp_config.py → repo root is parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_PYTHON = _REPO_ROOT / ".venv" / "bin" / "python"
_SERVER_PY = _REPO_ROOT / "mcp" / "server.py"


def get_mcp_server_config(project_root: Path | None = None) -> dict[str, Any]:
    """
    Build the kgraph MCP server launch config.

    project_root: the kernel source dir whose .kgraph/kgraph.db to serve.
        Defaults to the current working directory (where `kgraph install`
        is run). Passed to the server via KGRAPH_ROOT.

    Returns a dict with: command, args, env.
    """
    root = (project_root or Path.cwd()).resolve()
    env = {
        "KGRAPH_ROOT": str(root),
        "KGRAPH_DB": str(root / ".kgraph" / "kgraph.db"),
    }

    # Prefer a `kgraph` binary on PATH (post install.sh).
    kgraph_bin = shutil.which("kgraph")
    if kgraph_bin:
        return {
            "command": "kgraph",
            "args": ["serve", "--mcp"],
            "env": env,
        }

    # Dev fallback: launch the server module directly with the venv python.
    python = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable
    return {
        "command": python,
        "args": [str(_SERVER_PY)],
        "env": env,
    }


def get_command_array(project_root: Path | None = None) -> list[str]:
    """Flatten config to a single command array (opencode-style)."""
    cfg = get_mcp_server_config(project_root)
    return [cfg["command"], *cfg["args"]]


# Claude permission entries to auto-allow (one per MCP tool).
KGRAPH_TOOLS = [
    "search_symbols", "get_symbol", "get_function_body",
    "find_callers", "find_callees", "call_path",
    "find_references", "find_type_definition", "get_struct_layout",
    "find_ops_impls", "get_neighborhood", "index_status",
]


def get_claude_permissions() -> list[str]:
    """Permission strings for Claude settings.json permissions.allow."""
    return [f"mcp__kgraph__{t}" for t in KGRAPH_TOOLS]


# ──────────────────────────────────────────────
# JSON file helpers
# ──────────────────────────────────────────────

def read_json(path: Path) -> dict:
    """Read a JSON file, returning {} if absent or unparseable."""
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def write_json(path: Path, data: dict) -> None:
    """Write a JSON file atomically, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)