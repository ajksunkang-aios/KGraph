[English](README.md) | [中文](README.zh-CN.md)

<div align="center">

# KGraph

### 编译器感知的内核图谱引擎 · MCP 工具服务

**配置感知 · 宏展开 · 函数指针可达 · SQLite 原生**

### [设计文档 →](docs/DESIGN.zh-CN.md)

<br>

_KGraph 索引的是**编译器**看到的真相——不是解析器猜的语法。_

</div>

---

## 为什么需要 KGraph？

内核代码不是普通代码。它藏在 `#ifdef CONFIG_*` 后面，嵌在 `SYSCALL_DEFINE*` 宏里，
挂在 `file_operations` 函数指针表上——这些 tree-sitter 跟不了。
现有工具（codegraph、semcode）只解析语法——KGraph 解析的是**编译真相**。

| 别人看不到的 | KGraph 能捕获的 |
|---|---|
| 所有 `#ifdef` 分支（你的 config 下大部分是死代码） | 只有**你的 defconfig 实际编译**的代码 |
| `EXPORT_SYMBOL` / `SYSCALL_DEFINE*` 当成普通文本 | 宏展开后的真实符号名和位置 |
| `f_op->read_iter()` → "无法解析" | `ops_bind` 边：`.read_iter = ext4_file_read_iter` → 具体实现函数 |
| 跨 TU 同名 `static` helper → 名字冲突 | clang 符号解析消歧 |

**结果**：LLM agent 用 **3 次 tool-call / ~1.5k token** 找到根因路径，
而 grep 式工作流烧掉 **10k+ token** 还散落在无关分支上。

---

## 工作流概览

只要你的内核有了 `compile_commands.json`，整个配置就是三条命令：

```bash
# 1. 安装 kgraph CLI
curl -fsSL https://raw.githubusercontent.com/ajksunkang/KGraph/main/install.sh | bash

# 2. 把 kgraph 的 MCP 服务接入你的 AI agent（自动检测已安装的 agent）
kgraph install

# 3. 为这个内核构建代码图谱（在内核源码目录内运行）
cd /path/to/linux
kgraph init .
```

就这样。重启 agent，问它内核代码的结构性问题——它会调用 KGraph 的 MCP 工具，而不是 grep。

```
> 哪些函数调用了 ext4_file_read_iter？
> 内核里有哪些 ->read_iter 的实现？
> 给我看 generic_file_read_iter 的函数体。
```

```
  curl install.sh          kgraph install            kgraph init .
 ┌──────────────┐        ┌──────────────────┐      ┌────────────────────┐
 │ kgraph CLI   │   →    │   配置 agent      │  →   │ scip-clang → SQLite │
 │ 装到 PATH    │        │ (claude/cursor/  │      │ .kgraph/kgraph.db   │
 │              │        │  codex/opencode/ │      │ 可供查询            │
 │              │        │  hermes)         │      │                     │
 └──────────────┘        └──────────────────┘      └────────────────────┘
```

> **前提**：一个用 **clang** 构建出 `compile_commands.json` 的内核树
> （`make CC=clang LLVM=1`）。如何产出见下方 [详细配置](#详细配置)，含 Docker。

---

## 详细配置

### 第 0 步（前提）：用 clang 构建 `compile_commands.json`

KGraph 是编译器感知的——它索引编译器真正看到的代码，所以需要 clang 编译数据库。
Docker 提供干净、可复现的构建环境：

```bash
docker run --platform linux/amd64 -it --rm \
  -v "$(pwd):/workspace" -w /workspace ubuntu:latest

# 容器内：
apt-get update && apt-get install -y clang llvm make bc flex bison libelf-dev libssl-dev
make CC=clang LLVM=1 x86_64_defconfig
make CC=clang LLVM=1 -j$(nproc)
./scripts/clang-tools/gen_compile_commands.py
```

产出 `compile_commands.json`（约 5–50 MB），列出你的 `defconfig` 实际编译的 `.c` 文件
及其精确编译器标志。**这种配置感知正是 KGraph 与语法级工具的本质区别。**

<details>
<summary><strong>用 scip-clang 生成 index.scip</strong></summary>

`kgraph init` 会自动做这一步，你也可以直接运行。`scip-clang` 是 Linux x86-64 二进制——
在同一个 Docker/Linux 环境里运行：

```bash
# 容器内，scip-clang 可用时：
./scip-tools/scip-clang --compdb-path ./compile_commands.json
# → 产出 index.scip（全量 defconfig 约数百 MB）
```

</details>

### 第 1 步：安装 kgraph CLI

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/ajksunkang/KGraph/main/install.sh | bash
```

下载 `kgraph` 到 `~/.local/bin` 并注册到 `PATH`。打开**新终端**让命令生效。

<sub>已经克隆了仓库？也可以从项目根目录直接运行 `./install.sh`。</sub>

### 第 2 步：配置你的 AI agent

```bash
kgraph install
```

`kgraph install` 会执行一次 `detect()`——读取各 agent 的配置文件/目录，
识别当前系统装了哪些 AI agent，并**自动配置检测到的那些**。支持的 agent 及配置位置：

| Agent | 配置文件 | 格式 |
|---|---|---|
| **Claude Code** | `~/.claude.json` + `~/.claude/settings.json` | JSON `mcpServers` + 权限 |
| **Cursor** | `~/.cursor/mcp.json` | JSON `mcpServers` |
| **Codex CLI** | `~/.codex/config.toml` | TOML `[mcp_servers.kgraph]` |
| **opencode** | `~/.config/opencode/opencode.json` | JSONC `mcp.kgraph` |
| **Hermes Agent** | `~/.hermes/config.yaml` | YAML `mcp_servers` + toolsets |

```bash
kgraph detect                          # 只显示检测结果，不写文件
kgraph install                         # 自动检测并配置已安装的 agent
kgraph install --target claude,cursor  # 配置指定 agent
kgraph install --location local        # 项目级配置（./.mcp.json 等）
kgraph uninstall                       # 从所有 agent 移除 kgraph 配置
```

<sub>想手动配置？见 [`mcp/examples/`](mcp/examples/)——每个 agent 的可直接修改的配置示例。</sub>

### 第 3 步：构建代码图谱

```bash
cd /path/to/linux        # 内核源码目录（compile_commands.json 所在处）
kgraph init .
```

`kgraph init` 自动执行：

1. **venv** — 创建 Python 3.10+ 虚拟环境并装 `protobuf>=7.35`（upb）
2. **索引** — 运行 `scip-clang --compdb-path compile_commands.json` → `index.scip`
   （若 `index.scip` 已存在则跳过）
3. **灌库** — 解析 SCIP protobuf，派生调用图 + `ops_bind` 边，全部写入 `./.kgraph/kgraph.db`
4. **富化** — 映射 MAINTAINERS → 子系统标签

所有产物都留在内核树内（`index.scip`、`.kgraph/kgraph.db`）——图谱是 per-project 的，
每个你索引的内核都有自己独立的数据库。

```bash
kgraph init . --skip-build                # index.scip 已存在，只灌库
kgraph init . --subsystem fs/ext4         # 限定子系统（更快）
kgraph init . --force                     # 从头重建
```

<details>
<summary><strong>手动 venv 设置（kgraph init 找不到 python3.10+ 时）</strong></summary>

```bash
# 找到系统上的 python3.10+，然后：
python3.10 -m venv /path/to/KGraph/.venv
source /path/to/KGraph/.venv/bin/activate
pip install "protobuf>=7.35.0,<8"
python -c "import google.protobuf; print(google.protobuf.__version__)"   # → 7.35.0
```

</details>

### 第 4 步：使用 agent

重启 agent 让 MCP 服务加载。它现在有了 KGraph 的工具——问结构性问题，
它会查图谱而不是 grep：

```
> 哪些函数调用了 ext4_file_read_iter？           → find_callers
> generic_file_read_iter 调用了什么？             → find_callees
> 给我看 ext4_file_read_iter 的函数体。           → get_function_body
> 内核里有哪些 ->read_iter 的实现？               → find_ops_impls
> vfs_read 在哪些地方被引用？                     → find_references
```

---

## MCP 工具集

KGraph 暴露 **12 个工具**——覆盖大部分 agent 代码索引诉求的最小可行集。
每个工具都是配置感知、编译器解析的。

### 符号查找

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `search_symbols(query)` | 按名称模糊全文搜索（FTS5） | `kind, limit` |
| `get_symbol(name)` | 精确名查找 → 定义 + 签名 | `kind, limit` |
| `get_function_body(name)` | 从磁盘读真实函数体源码（带行号） | `kind, context` |

### 调用图与引用

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `find_callers(name)` | 谁调用了这个函数——**含 `ops_bind`** | `depth, limit` |
| `find_callees(name)` | 这个函数调用了谁——**含 `ops_bind`** | `depth, limit` |
| `call_path(source, target)` | 两函数间的调用路径 | `max_len` |
| `find_references(name)` | 符号的每个使用点，带所在函数 | `limit` |

### 类型与结构

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `find_type_definition(name)` | go-to-type-definition（`type_of` 边） | — |
| `get_struct_layout(name)` | 结构体字段（`contains` 边） | — |
| `get_neighborhood(name)` | N-hop 子图——最省 token 的上下文打包 | `depth, edge_types, summary` |

### 内核专属与元信息

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `find_ops_impls(field_name)` | **★** 函数指针字段 → 所有实现 | `struct_type` |
| `index_status()` | 索引元数据 + 统计 | — |

**★ `find_ops_impls` 是杀手锏。** 它解析内核函数指针表（VFS ops、驱动 ops、net proto ops）
的间接调用——grep 和语法级工具跟不了。一次 `find_ops_impls("read_iter")` 返回内核里
所有文件系统/驱动的 `read_iter` 实现：

```
ext4_file_operations    → ext4_file_read_iter   @ fs/ext4/file.c
shmem_file_operations   → shmem_file_read_iter  @ mm/shmem.c
socket_file_ops         → sock_read_iter        @ net/socket.c
... (16 个)
```

### Token 预算控制

`find_callers`/`find_callees` 接受 `depth` 和 `limit`；`get_neighborhood` 默认返回
紧凑的 `name + file:line`（`summary=true`）。agent 可以控制预算而不被子图爆炸淹没。

---

## 工作原理

```
┌───────────────────────────────────────────────────────────────┐
│                        你的 Code Agent                         │
│  "哪些实现了 ->read_iter？" → 直接调用 KGraph 工具              │
└─────────────────────────────────┬─────────────────────────────┘
                                  │
                                  ▼
┌───────────────────────────────────────────────────────────────┐
│                  KGraph MCP 服务端（12 个工具）                 │
│  search · get_symbol · get_function_body · callers · callees   │
│  call_path · references · type_definition · struct_layout      │
│  neighborhood · ops_impls · index_status                       │
│                                  │                             │
│                                  ▼                             │
│              SQLite 知识图谱 (.kgraph/kgraph.db)              │
│   symbols · occurrences · edges · ops_bind · subsystem         │
└───────────────────────────────────────────────────────────────┘
```

1. **构建** — `make CC=clang LLVM=1` 产出 `compile_commands.json`（编译器真正编译的内容）
2. **索引** — `scip-clang` 产出 `index.scip`，含完整语义符号信息
3. **灌库** — Python（protobuf 7.x / upb）把 SCIP 解析成 `IngestBatch`，从 `enclosing_range`
   派生调用边，从函数指针表初始化派生 `ops_bind` 边，经 `GraphStore` 接口写入 SQLite
4. **富化** — `KernelProfile` 映射 MAINTAINERS → 子系统标签、标注 config 门控符号
5. **服务** — MCP 服务端通过 SQLite 递归 CTE 暴露图查询

`IngestBatch` → `GraphStore` 边界让 parser 与存储完全解耦——换存储后端（Neo4j、自研嵌入式库）
只需实现一个新 `GraphStore`，parser、MCP 工具、agent 接入都不动。

---

## CLI 参考

```bash
# Agent 接入
kgraph install                     # 自动检测并配置已安装 agent
kgraph install --target <ids>      # 配置指定 agent（claude,cursor,codex,opencode,hermes）
kgraph install --location <loc>    # global（默认）或 local（项目级）
kgraph detect                      # 显示检测到的 agent，不写文件
kgraph uninstall                   # 从 agent 移除 kgraph 配置

# 索引生命周期（在内核源码目录内运行）
kgraph init <path>                 # 索引 + 灌库（--skip-build, --subsystem, --force）
kgraph ingest <path>               # 从已有 index.scip 重新灌库
kgraph serve --mcp                 # 启动 MCP 服务端（通常由 agent 自动拉起）
kgraph status <path>               # 查看索引统计与健康
```

---

## 与现有工具对比

| | **codegraph** | **semcode** | **KGraph** |
|---|---|---|---|
| 解析后端 | tree-sitter | tree-sitter | **SCIP-clang** |
| 语义深度 | 语法级 | 语法级 | **编译器级** |
| 配置感知 | 否（所有分支） | 否（所有分支） | **是（只索引编译的代码）** |
| 宏解析 | 启发式 | 启发式 | **clang 预处理器** |
| 函数指针调用 | 启发式名字匹配 | 启发式名字匹配 | **ops_bind 派生边** |
| 类型解析 | 名字匹配 | 名字匹配 | **clang 精确** |
| 内核领域知识 | 无 | git/lore/向量 | **MAINTAINERS/Kconfig/syscall** |
| 存储 | SQLite | LanceDB | **SQLite** |
| 目标范围 | 20+ 语言，通用 | C/Rust，内核 | **C，内核专属，深度** |

> codegraph = 广度（多语言、装得快）
> semcode = 内核工程化（git/lore/向量，语法级）
> **KGraph = 编译真相（配置感知、宏展开、函数指针可达）**

---

## 支持的内核 Profile

| 内核 | 构建系统 | 状态 |
|---|---|---|
| **Linux** | Kbuild (`CC=clang LLVM=1`) | **MVP** |
| Android | Soong + repo manifest.xml | 规划中 |
| Zephyr | CMake + west manifest.yml | 规划中 |
| FreeBSD | Make + `src.conf` | 规划中 |

新增内核 Profile 只需写一个 `KernelProfile` 子类——构建管线 + 领域富化——不动 ingest 或查询核心。
详见 [设计文档 §6](docs/DESIGN.zh-CN.md)。

---

## 项目结构

```
KGraph/
├── README.md / README.zh-CN.md     # 本文件（英文 / 中文）
├── install.sh                      # 一键 CLI 安装脚本
├── docs/
│   ├── DESIGN.md / DESIGN.zh-CN.md  # 架构与设计理念
│   └── scip-parser-design.md        # SCIP 解析器设计笔记
├── thirdparty/
│   └── scip.proto                  # SCIP protobuf 规范
├── scripts/
│   └── scip_pb2.py                 # 生成的 protobuf 绑定（7.x / upb）
├── src/
│   ├── parser/                     # SCIP protobuf → IngestBatch
│   │   ├── models.py               #   数据模型（parser↔storage 契约）
│   │   ├── scip_parser.py          #   解析 + enclosing 匹配 + ops_bind 派生
│   │   └── symbol_name.py          #   SCIP 符号串解析
│   ├── storage/                    # 图谱持久化
│   │   ├── graph_store.py          #   GraphStore 接口（扩展点）
│   │   └── sqlite_store.py         #   SQLite 后端（WAL · FTS5 · 递归 CTE）
│   └── installer/                  # agent 自动配置
│       ├── orchestrator.py         #   detect() / install() / uninstall()
│       ├── cli.py                  #   `kgraph install` CLI
│       └── targets/                #   claude · cursor · codex · opencode · hermes
├── mcp/
│   ├── server.py                   # MCP 服务端（12 个工具）
│   ├── source_reader.py            # 从磁盘读函数体
│   └── examples/                   # 各 agent 手动配置示例
└── tests/
    ├── conftest.py                  # 共享 fixture 与合成 SCIP benchmark
    ├── unit/                        # 单元测试（纯函数，参数化）
    ├── integration/                 # 集成测试（合成数据，不需要真实内核）
    │   ├── test_scip_pipeline.py    #   index.scip → parser → store（41 tests）
    │   └── test_mcp_server.py       #   MCP 工具 → kgraph.db（31 tests）
    └── real/                        # 真实内核案例测试（手动脚本）
        └── ingest_real.py           #   全量内核灌库
```

---

## 开发环境设置

如果你在开发 KGraph（不仅是作为用户使用）：

```bash
git clone https://github.com/ajksunkang/KGraph.git
cd KGraph

# 用任何 python3.10+ 建 venv 并安装依赖
python3.10 -m venv .venv
source .venv/bin/activate
pip install "protobuf>=7.35.0,<8" mcp pytest
python -c "import google.protobuf; print(google.protobuf.__version__)"   # → 7.35.0

# 仅当修改 thirdparty/scip.proto 时才需重新生成 scip_pb2.py
protoc --proto_path=thirdparty --python_out=scripts thirdparty/scip.proto

# 运行测试（全部合成数据，不需要真实内核）
pytest tests/ -v                          # 全部测试
pytest tests/integration/ -v              # 仅集成测试
pytest tests/unit/ -v                     # 仅单元测试

# 运行真实内核灌库（需要内核树的 index.scip）
KGRAPH_ROOT=/path/to/linux python tests/real/ingest_real.py
```

完整测试设计与覆盖范围见 [`docs/TESTING.md`](docs/TESTING.md)。

---

## 卸载

```bash
kgraph uninstall               # 从所有 agent 移除 kgraph MCP 配置
rm -rf /path/to/linux/.kgraph  # 从项目移除图谱数据库
```

---

## 许可证

MIT

---

<div align="center">

_为内核开发者和 AI agent 而做——看见编译器看到的真相。_

[报告问题](https://github.com/ajksunkang/KGraph/issues) · [功能建议](https://github.com/ajksunkang/KGraph/issues)

</div>