[English](README.md) | [中文](README.zh-CN.md)

# KGraph MCP — Configuration Examples for AI Agents

Manual configuration examples for registering the KGraph MCP server with each AI
agent. Every file documents the **config file path**, **format**, and a
**ready-to-copy config block**.

> Prefer auto-configuration via `kgraph install` (see the root README).
> Use this directory for manual setup, or as a reference when auto-config fails.

## Quick Index

| Agent | Example file | Config file path | Format |
|---|---|---|---|
| **Claude Code** | [`claude.example.json`](claude.example.json) | `~/.claude.json` (global) / `./.mcp.json` (project) | JSON `mcpServers` |
| → Permissions (optional) | [`claude-permissions.example.json`](claude-permissions.example.json) | `~/.claude/settings.json` | JSON `permissions.allow` |
| **Cursor** | [`cursor.example.json`](cursor.example.json) | `~/.cursor/mcp.json` (global) / `./.cursor/mcp.json` (project) | JSON `mcpServers` |
| **Codex CLI** | [`codex.example.toml`](codex.example.toml) | `~/.codex/config.toml` (global only) | TOML `[mcp_servers.kgraph]` |
| **opencode** | [`opencode.example.jsonc`](opencode.example.jsonc) | `~/.config/opencode/opencode.json` (global) / `./opencode.json` (project) | JSONC `mcp.kgraph` |
| **Hermes Agent** | [`hermes.example.yaml`](hermes.example.yaml) | `$HERMES_HOME/config.yaml` (default `~/.hermes/config.yaml`, global only) | YAML `mcp_servers` + `platform_toolsets` |

## General Steps

1. Find the example file for your agent
2. Replace the placeholder paths with the real paths on your machine:
   - `command` → KGraph venv python (`/path/to/KGraph/.venv/bin/python`)
   - `args` → path to `KGraph/mcp/server.py`
   - `KGRAPH_ROOT` → your indexed kernel source tree (the dir containing `.kgraph/`)
   - `KGRAPH_DB` → that tree's `.kgraph/kgraph.db`
3. **Merge** the config block into the corresponding config file (don't overwrite
   your other MCP servers)
4. Restart your agent so the MCP server loads

## Two Launch Modes

**Mode 1: launch `server.py` directly with the venv python (dev / not on PATH)**
```
command: /path/to/KGraph/.venv/bin/python
args:    [/path/to/KGraph/mcp/server.py]
```

**Mode 2: use the `kgraph` command on PATH (installed via install.sh)**
```
command: kgraph
args:    [serve, --mcp]
```

Both modes require the `KGRAPH_ROOT` / `KGRAPH_DB` env vars to point at your
kernel index.

## Config Shape Cheat Sheet

| | Claude / Cursor | Codex | opencode | Hermes |
|---|---|---|---|---|
| Top-level key | `mcpServers` | `[mcp_servers.x]` | `mcp.x` | `mcp_servers.x` |
| command | string | string | **string array** (incl. args) | string |
| Env-var key | `env` | `env` | **`environment`** | `env` |
| Extra requirements | permissions file (optional) | global only | `enabled: true` | add `platform_toolsets.cli: - mcp-kgraph` |

## Verify

After configuring, ask your agent a structural question to confirm the MCP tools
loaded:

```
> Use kgraph to list all callers of ext4_file_read_iter
> What implementations of read_iter are there in kgraph?
```

Or call the `index_status` tool directly to see index statistics.
