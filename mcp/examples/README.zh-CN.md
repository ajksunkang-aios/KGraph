[English](README.md) | [中文](README.zh-CN.md)

# KGraph MCP — 各 AI Agent 配置示例

手动注册 KGraph MCP 服务到各 AI agent 的配置示例。
每个文件包含**配置文件路径**、**格式说明**和**可直接复制的配置块**。

> 推荐用 `kgraph install` 自动配置（见根目录 README）。
> 本目录用于手动配置，或自动配置失败时参考。

## 快速索引

| Agent | 示例文件 | 配置文件路径 | 格式 |
|---|---|---|---|
| **Claude Code** | [`claude.example.json`](claude.example.json) | `~/.claude.json`（全局）/ `./.mcp.json`（项目） | JSON `mcpServers` |
| → 权限（可选） | [`claude-permissions.example.json`](claude-permissions.example.json) | `~/.claude/settings.json` | JSON `permissions.allow` |
| **Cursor** | [`cursor.example.json`](cursor.example.json) | `~/.cursor/mcp.json`（全局）/ `./.cursor/mcp.json`（项目） | JSON `mcpServers` |
| **Codex CLI** | [`codex.example.toml`](codex.example.toml) | `~/.codex/config.toml`（仅全局） | TOML `[mcp_servers.kgraph]` |
| **opencode** | [`opencode.example.jsonc`](opencode.example.jsonc) | `~/.config/opencode/opencode.json`（全局）/ `./opencode.json`（项目） | JSONC `mcp.kgraph` |
| **Hermes Agent** | [`hermes.example.yaml`](hermes.example.yaml) | `$HERMES_HOME/config.yaml`（默认 `~/.hermes/config.yaml`，仅全局） | YAML `mcp_servers` + `platform_toolsets` |

## 通用步骤

1. 找到你的 agent 对应的示例文件
2. 把示例中的占位路径改成你机器上的真实路径：
   - `command` → KGraph venv python（`/path/to/KGraph/.venv/bin/python`）
   - `args` → `KGraph/mcp/server.py` 的路径
   - `KGRAPH_ROOT` → 你索引的内核源码树（`.kgraph/` 所在目录）
   - `KGRAPH_DB` → 该树的 `.kgraph/kgraph.db`
3. 把配置块**合并**进对应的配置文件（不要覆盖你已有的其他 MCP server）
4. 重启 agent，使 MCP 服务加载

## 两种启动方式

**方式一：直接用 venv python 启动 server.py（开发 / 未装 PATH）**
```
command: /path/to/KGraph/.venv/bin/python
args:    [/path/to/KGraph/mcp/server.py]
```

**方式二：用 PATH 上的 kgraph 命令（已通过 install.sh 安装）**
```
command: kgraph
args:    [serve, --mcp]
```

两种方式都需要 `KGRAPH_ROOT` / `KGRAPH_DB` 环境变量指向你的内核索引。

## 配置形态差异速查

| | Claude / Cursor | Codex | opencode | Hermes |
|---|---|---|---|---|
| 顶层键 | `mcpServers` | `[mcp_servers.x]` | `mcp.x` | `mcp_servers.x` |
| command | 字符串 | 字符串 | **字符串数组**（含 args） | 字符串 |
| 环境变量键 | `env` | `env` | **`environment`** | `env` |
| 额外要求 | 权限文件（可选） | 仅全局 | `enabled: true` | 需加 `platform_toolsets.cli: - mcp-kgraph` |

## 验证

配置后，在 agent 里问一个结构性问题确认 MCP 工具已加载：

```
> 用 kgraph 查 ext4_file_read_iter 的所有调用者
> kgraph 里 read_iter 有哪些实现？
```

或直接调 `index_status` 工具看索引统计。
