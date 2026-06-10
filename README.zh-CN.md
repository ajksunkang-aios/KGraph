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

## 快速开始

### 第一步：准备内核构建环境（推荐 Docker）

KGraph 是编译器感知的——它索引的是编译器真正看到的代码。你需要一个能产出
`compile_commands.json`（使用 **clang**）的内核构建环境。

推荐用 Docker，干净、可复现、不污染宿主机：

```bash
# 使用提供的 Dockerfile（或自己的内核构建镜像）
docker build -t kgraph-linux-build -f Dockerfile .

# 或拉取预构建镜像（如有）
# docker pull ajksunkang/kgraph-linux-build:latest

# 运行构建容器，挂载内核源码
docker run -it -v /path/to/linux:/kernel kgraph-linux-build bash
```

<details>
<summary><strong>Dockerfile 示例（Linux x86_64 defconfig）</strong></summary>

```dockerfile
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    clang llvm gcc make bc flex bison libelf-dev \
    libssl-dev libncurses-dev python3 python3-pip \
    git curl protobuf-compiler && \
    rm -rf /var/lib/apt/lists/*

# 安装 scip-clang
RUN curl -fsSL https://github.com/sourcegraph/scip-clang/releases/latest/download/scip-clang-linux-x64.tar.gz \
    | tar xz -C /usr/local/bin/

WORKDIR /kernel
```

</details>

**在容器内**（或有 `clang` + `make` 的任何环境）：

```bash
cd /kernel

# 生成 x86_64 defconfig
make CC=clang LLVM=1 x86_64_defconfig

# 编译内核（产出 compile_commands.json）
make CC=clang LLVM=1 -j$(nproc)

# 或不完整编译、只生成 compile_commands.json：
make CC=clang LLVM=1 prepare
scripts/clang-tools/gen_compile_commands.py
```

<details>
<summary><strong>不使用 Docker —— 本地构建</strong></summary>

如果你更愿意本地构建，确保已安装：

- **clang**（≥ 14）和 **LLVM** 工具
- **内核构建依赖**：`bc flex bison libelf-dev libssl-dev`
- **scip-clang**：从 [github.com/sourcegraph/scip-clang](https://github.com/sourcegraph/scip-clang) 下载

```bash
# macOS (Homebrew)
brew install clang llvm protobuf

# Ubuntu/Debian
sudo apt install clang llvm protobuf-compiler \
    bc flex bison libelf-dev libssl-dev

# 然后按上面的步骤构建
cd /path/to/linux
make CC=clang LLVM=1 x86_64_defconfig
make CC=clang LLVM=1 -j$(nproc)
```

</details>

### 第二步：构建 `compile_commands.json`

这一步产出编译数据库——列出在你的 config 下实际编译的 `.c` 文件及其精确编译器标志。

```bash
# 在构建环境内（Docker 或本地）：
cd /kernel

# 完整编译 + compile_commands.json
make CC=clang LLVM=1 -j$(nproc)
scripts/clang-tools/gen_compile_commands.py

# 确认文件存在
ls -lh compile_commands.json
# 约 5-50MB，视 config 覆盖范围而定
```

**关键**：`compile_commands.json` 是配置感知的——只列出你的 `defconfig` 实际编译的文件。
这正是 KGraph 与语法级工具的本质区别。

### 第三步：安装 KGraph

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/ajksunkang/KGraph/main/install.sh | bash
```

下载 `kgraph` 到 `~/.local/bin` 并注册到 `PATH`。
打开**新终端**让命令生效。

<sub>已经克隆了仓库？也可以从项目根目录直接运行 `./install.sh`。</sub>

### 第四步：在内核源码中初始化 KGraph

```bash
# 在构建环境的内核源码目录内：
cd /kernel
kgraph init .
```

`kgraph init` 自动执行以下步骤：

1. **创建 venv** — 在 KGraph 项目内创建 Python 3.10+ 虚拟环境 `.venv/`
2. **安装 protobuf** — 安装 `protobuf>=7.35.0`（upb 格式）到 venv，
   版本与生成 `scip_pb2.py` 的 protoc 版本匹配
3. **索引** — 运行 `scip-clang --compilation-database compile_commands.json` → `index.scip`
4. **灌库** — 解析 SCIP protobuf，派生调用图 + ops 绑定，写入 `.kgraph/kgraph.db`
5. **富化** — 映射 MAINTAINERS → 子系统标签

<details>
<summary><strong>手动 venv 设置（如果 kgraph init 失败）</strong></summary>

如果自动 venv 设置失败（如系统没有 python3.10+），手动设置：

```bash
# 找到系统上的 python3.10+
# macOS (Homebrew): /opt/homebrew/bin/python3.10
# Linux: /usr/bin/python3.10 或 python3.12

PYTHON3=/opt/homebrew/bin/python3.10   # 根据系统调整

# 在 KGraph 项目中创建 venv
$PYTHON3 -m venv /path/to/KGraph/.venv

# 激活并安装 protobuf
source /path/to/KGraph/.venv/bin/activate
pip install "protobuf>=7.35.0,<8"

# 验证
python -c "import google.protobuf; print(google.protobuf.__version__)"
# 应输出: 7.35.0
```

</details>

<details>
<summary><strong>索引范围选项</strong></summary>

```bash
# 跳过构建步骤（已有 compile_commands.json）：
kgraph init . --skip-build

# 只索引一个子系统（MVP / 测试更快）：
kgraph init . --subsystem fs/ext4

# 强制全量重建：
kgraph init . --force

# 多子系统
kgraph init . --subsystem fs/ext4,net/ipv4,mm
```

</details>

### 第五步：安装 MCP 工具到你的 Code Agent

```bash
kgraph install
```

自动检测并配置 MCP 服务端集成到已安装的 agent：

- **Claude Code** — 写入 MCP 服务端配置 + 自动授权权限
- **Cursor** — 写入 `.cursor/mcp.json`
- **Codex CLI** — 写入 MCP 配置
- **其他 MCP 兼容 agent** — 打印配置片段供手动接入

<details>
<summary><strong>非交互模式（脚本 / CI）</strong></summary>

```bash
kgraph install --yes                          # 自动检测 agent，接受默认
kgraph install --target=claude,cursor --yes    # 指定 agent 列表
kgraph install --print-config claude           # 只打印配置片段，不写文件
```

| 参数 | 取值 | 默认 |
|---|---|---|
| `--target` | `auto`, `all`, `none`, 或逗号分隔 (`claude,cursor,...`) | 提示 |
| `--yes` | (布尔) 接受默认 | 每步提示 |
| `--print-config <id>` | 打印指定 agent 的配置片段 | — |

</details>

### 第六步：使用 Agent + KGraph

重启 agent 让 MCP 服务端加载。然后提问：

```
> 哪些函数调用了 ext4_file_read_iter？
> 一个 read 请求从 VFS 到 ext4 是怎么流的？
> mm/page_alloc.c 属于哪个子系统？
> 找出 file_operations.read_iter 的所有实现
```

项目根目录存在 `.kgraph/` 时，你的 agent 会自动调用 KGraph MCP 工具。

---

## MCP 工具集

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `search_symbols(q)` | 按名称/正则/全文检索符号 | `kind, limit` |
| `get_definition(sym)` | 定义位置 + 签名（不返回整文件） | — |
| `find_callers(sym)` | 谁调用了这个函数（反向调用图） | `depth, limit` |
| `find_callees(sym)` | 这个函数调用了谁（正向调用图） | `depth, limit` |
| `get_neighborhood(sym)` | N-hop 子图——最省 token 的上下文打包 | `depth, edge_types, summary` |
| `call_path(a, b)` | 两函数间的调用路径 | `max_len` |
| `get_struct_layout(type)` | 结构体字段和布局 | — |
| `find_ops_impls(field)` | 函数指针字段→所有候选实现 **★** | `struct_type` |
| `which_subsystem(sym)` | MAINTAINERS 子系统归属 | — |
| `expand_macro(name)` | 宏定义体 | — |

**★ `find_ops_impls` 是杀手锏**——解析内核函数指针表（VFS ops、驱动 ops、net proto ops）
的间接调用，语法级工具跟不了。

### Token 预算控制

每个工具接受 `summary=true` 只返回名字+文件:行（不含签名、文档、源码）。
配合 `depth` 和 `limit`，agent 可以控制预算而不被子图爆炸淹没。

---

## 工作原理

```
┌───────────────────────────────────────────────────────────────┐
│                        你的 Code Agent                         │
│                                                               │
│  "一个 read 请求怎么到达 ext4_file_read_iter？"                │
│         直接调用 KGraph 工具                                   │
│                         │                                     │
└─────────────────────────┼─────────────────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────────────────┐
│                     KGraph MCP 服务端                          │
│                                                               │
│   search / callers / callees / neighborhood / ops_impls / ..  │
│                         │                                     │
│                         ▼                                     │
│              SQLite 知识图谱 (.kgraph/kgraph.db)              │
│   symbols · occurrences · edges · ops_bind · subsystem        │
└───────────────────────────────────────────────────────────────┘
```

1. **构建** — `make CC=clang LLVM=1` 产出 `compile_commands.json`（编译器真正编译的内容）
2. **索引** — `scip-clang` 产出 `index.scip`，含完整语义符号信息
3. **灌库** — Python（protobuf 7.x / upb）解析 SCIP，从 `enclosing_range` 派生调用边，
   从函数指针表初始化派生 `ops_bind` 边，写入 SQLite
4. **富化** — `KernelProfile` 映射 MAINTAINERS→子系统标签、标注 config 门控符号
5. **服务** — MCP 服务端通过 SQLite 递归 CTE 暴露图查询

---

## CLI 参考

```bash
kgraph install                     # 注册 MCP 服务端到 code agent
kgraph init <path>                 # 构建 + 索引 + 灌库（--skip-build, --subsystem, --force）
kgraph index <path>                # 只重建 SCIP 索引（不重编译）
kgraph ingest <path>               # 从已有 index.scip 重新灌库
kgraph serve --mcp                 # 启动 MCP 服务端（通常由 agent 自动拉起）
kgraph query <search>              # CLI 符号搜索（--kind, --limit, --json）
kgraph callers <symbol>            # CLI 反向调用图
kgraph callees <symbol>            # CLI 正向调用图
kgraph status <path>               # 查看索引统计与健康
kgraph uninstall                   # 从所有 agent 移除 MCP 配置
kgraph uninit <path>               # 从项目移除 .kgraph/
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
├── docs/
│   ├── DESIGN.md          # 架构与设计理念（英文）
│   ├── DESIGN.zh-CN.md    # 架构与设计理念（中文）
├── README.md              # 英文
├── README.zh-CN.md        # 中文
├── thirdparty/
│   └── scip.proto         # SCIP protobuf 规范（Sourcegraph 官方）
├── scripts/
│   └── scip_pb2.py        # 生成的 Python protobuf 绑定（protobuf 7.x / upb）
├── .venv/                 # Python 3.10+ venv，含 protobuf>=7.35
├── src/
│   ├── libkgraph/         # C：SCIP 解析、SQLite 批量灌库、图派生
│   ├── kgraph/            # Python：CLI、KernelProfile、QueryEngine、MCP 服务端
│   └── mcp/               # Python：MCP 工具定义与服务端
├── tests/
└── docs/
    └── scip-parser-design.md  # SCIP 解析器设计笔记
```

---

## 开发环境设置

如果你在开发 KGraph（不仅是作为用户使用）：

```bash
# 克隆仓库
git clone https://github.com/ajksunkang/KGraph.git
cd KGraph

# 创建并激活 venv
/opt/homebrew/bin/python3.10 -m venv .venv   # 或系统上的任何 python3.10+
source .venv/bin/activate

# 安装 protobuf 7.x（upb 格式，匹配 protoc 35）
pip install "protobuf>=7.35.0,<8"

# 验证
python -c "import google.protobuf; print(google.protobuf.__version__)"
# → 7.35.0

# 重新生成 scip_pb2.py（仅当你修改 thirdparty/scip.proto 时需要）
protoc --proto_path=thirdparty --python_out=scripts thirdparty/scip.proto

# 验证生成的绑定
python -c "import sys; sys.path.insert(0,'scripts'); import scip_pb2; print('OK')"
```

---

## 卸载

```bash
kgraph uninstall               # 从所有 agent 移除 MCP 配置
kgraph uninit /path/to/linux   # 从项目移除 .kgraph/
```

---

## 许可证

MIT

---

<div align="center">

_为内核开发者和 AI agent 而做——看见编译器看到的真相。_

[报告问题](https://github.com/ajksunkang/KGraph/issues) · [功能建议](https://github.com/ajksunkang/KGraph/issues)

</div>