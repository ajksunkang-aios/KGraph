[English](README.md) | [中文](README.zh-CN.md)

<div align="center">

# KGraph

### Compiler-Aware Kernel Graph Engine · MCP Tool Service

**Config-aware · Macro-resolved · Function-pointer-callable · SQLite-native**

### [Design Document →](docs/DESIGN.md)

<br>

_KGraph indexes what the **compiler** sees — not what the parser guesses._

</div>

---

## Why KGraph?

Kernel code is not regular code. It lives behind `#ifdef CONFIG_*`, inside `SYSCALL_DEFINE*` macros,
behind `file_operations` function-pointer tables that tree-sitter can't follow.
Existing tools (codegraph, semcode) parse syntax — KGraph parses **compilation truth**.

| What others miss | What KGraph captures |
|---|---|
| All `#ifdef` branches (most are dead under your config) | Only the code **your defconfig actually compiles** |
| `EXPORT_SYMBOL` / `SYSCALL_DEFINE*` as opaque text | Macro-expanded symbols with real names and positions |
| `f_op->read_iter()` → "can't resolve" | `ops_bind` edge: `.read_iter = ext4_file_read_iter` → concrete function |
| Same-named `static` helpers across TU → name collision | Per-TU disambiguation via clang symbol resolution |

**Result**: LLM agents find root-cause paths in **3 tool-calls / ~1.5k tokens** where grep-based workflows burn **10k+ tokens** and scatter across irrelevant branches.

---

## Workflow at a Glance

Once your kernel has a `compile_commands.json`, the whole setup is three commands:

```bash
# 1. Install the kgraph CLI
curl -fsSL https://raw.githubusercontent.com/ajksunkang/KGraph/main/install.sh | bash

# 2. Wire kgraph's MCP server into your AI agents (auto-detects what's installed)
kgraph install

# 3. Build the code graph for this kernel (run inside the kernel source dir)
cd /path/to/linux
kgraph init .
```

That's it. Restart your agent and ask it structural questions about the kernel —
it calls KGraph's MCP tools instead of grepping.

```
> What functions call ext4_file_read_iter?
> What implements ->read_iter across the kernel?
> Show me the body of generic_file_read_iter.
```

```
  curl install.sh          kgraph install            kgraph init .
 ┌──────────────┐        ┌──────────────────┐      ┌────────────────────┐
 │ kgraph CLI   │   →    │ configure agents │  →   │ scip-clang → SQLite │
 │ on your PATH │        │ (claude/cursor/  │      │ .kgraph/kgraph.db   │
 │              │        │  codex/opencode/ │      │ ready for queries   │
 │              │        │  hermes)         │      │                     │
 └──────────────┘        └──────────────────┘      └────────────────────┘
```

> **Prerequisite**: a kernel tree with `compile_commands.json` built using **clang**
> (`make CC=clang LLVM=1`). See [Detailed Setup](#detailed-setup) below for how to
> produce it, Docker included.

---

## Detailed Setup

### Step 0 (prerequisite): Build `compile_commands.json` with clang

KGraph is compiler-aware — it indexes what the compiler actually sees, so it needs a
clang compilation database. Docker gives you a clean, reproducible build environment:

```bash
docker run --platform linux/amd64 -it --rm \
  -v "$(pwd):/workspace" -w /workspace ubuntu:latest

# Inside the container:
apt-get update && apt-get install -y clang llvm make bc flex bison libelf-dev libssl-dev
make CC=clang LLVM=1 x86_64_defconfig
make CC=clang LLVM=1 -j$(nproc)
./scripts/clang-tools/gen_compile_commands.py
```

This produces `compile_commands.json` (~5–50 MB) listing exactly the `.c` files your
`defconfig` compiles, with the exact compiler flags. **This config-awareness is what
makes KGraph different from syntax-only tools.**

<details>
<summary><strong>Generate index.scip with scip-clang</strong></summary>

`kgraph init` runs this for you, but you can also run it directly. `scip-clang` is a
Linux x86-64 binary — run it in the same Docker/Linux environment:

```bash
# Inside the container, with scip-clang available:
./scip-tools/scip-clang --compdb-path ./compile_commands.json
# → produces index.scip (~hundreds of MB for a full defconfig)
```

</details>

### Step 1: Install the kgraph CLI

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/ajksunkang/KGraph/main/install.sh | bash
```

Downloads `kgraph` to `~/.local/bin` and registers it on your `PATH`.
Open a **new terminal** so the command resolves.

<sub>Already cloned the repo? Run `./install.sh` from the project root instead.</sub>

### Step 2: Configure your AI agents

```bash
kgraph install
```

`kgraph install` runs a `detect()` pass that reads each agent's config file/dir,
identifies which AI agents are present on your system, and **auto-configures the
detected ones**. Supported agents and where they're configured:

| Agent | Config file | Format |
|---|---|---|
| **Claude Code** | `~/.claude.json` + `~/.claude/settings.json` | JSON `mcpServers` + permissions |
| **Cursor** | `~/.cursor/mcp.json` | JSON `mcpServers` |
| **Codex CLI** | `~/.codex/config.toml` | TOML `[mcp_servers.kgraph]` |
| **opencode** | `~/.config/opencode/opencode.json` | JSONC `mcp.kgraph` |
| **Hermes Agent** | `~/.hermes/config.yaml` | YAML `mcp_servers` + toolsets |

```bash
kgraph detect                          # show what's detected, write nothing
kgraph install                         # auto-detect & configure installed agents
kgraph install --target claude,cursor  # configure specific agents
kgraph install --location local        # per-project config (./.mcp.json etc.)
kgraph uninstall                       # remove kgraph config from all agents
```

<sub>Prefer manual setup? See [`mcp/examples/`](mcp/examples/) — ready-to-edit config
snippets for every agent.</sub>

### Step 3: Build the code graph

```bash
cd /path/to/linux        # the kernel source dir (where compile_commands.json lives)
kgraph init .
```

`kgraph init` does the following automatically:

1. **venv** — sets up a Python 3.10+ virtual environment with `protobuf>=7.35` (upb)
2. **Index** — runs `scip-clang --compdb-path compile_commands.json` → `index.scip`
   (skipped if `index.scip` already exists)
3. **Ingest** — parses the SCIP protobuf, derives the call graph + `ops_bind` edges,
   writes everything into `./.kgraph/kgraph.db`
4. **Enrich** — maps MAINTAINERS → subsystem labels

Everything stays inside the kernel tree (`index.scip`, `.kgraph/kgraph.db`) — the graph
is per-project, so each kernel you index gets its own database.

```bash
kgraph init . --skip-build                # index.scip already exists, just ingest
kgraph init . --subsystem fs/ext4         # scope to a subsystem (faster)
kgraph init . --force                     # rebuild from scratch
```

<details>
<summary><strong>Manual venv setup (if kgraph init can't find python3.10+)</strong></summary>

```bash
# Find a python3.10+ on your system, then:
python3.10 -m venv /path/to/KGraph/.venv
source /path/to/KGraph/.venv/bin/activate
pip install "protobuf>=7.35.0,<8"
python -c "import google.protobuf; print(google.protobuf.__version__)"   # → 7.35.0
```

</details>

### Step 4: Use your agent

Restart your agent so the MCP server loads. It now has KGraph's tools — ask structural
questions and it queries the graph instead of grepping:

```
> What functions call ext4_file_read_iter?           → find_callers
> What does generic_file_read_iter call?             → find_callees
> Show me the body of ext4_file_read_iter.           → get_function_body
> What implements ->read_iter across the kernel?     → find_ops_impls
> Where is vfs_read referenced?                       → find_references
```

---

## MCP Tools

KGraph exposes **12 tools** — a minimal viable set covering the most common agent
code-indexing needs. Every tool is config-aware and compiler-resolved.

### Symbol lookup

| Tool | Purpose | Key params |
|---|---|---|
| `search_symbols(query)` | Fuzzy full-text search by name (FTS5) | `kind, limit` |
| `get_symbol(name)` | Exact-name lookup → definition + signature | `kind, limit` |
| `get_function_body(name)` | Read the actual source body from disk (with line numbers) | `kind, context` |

### Call graph & references

| Tool | Purpose | Key params |
|---|---|---|
| `find_callers(name)` | Who calls this function — **includes `ops_bind`** | `depth, limit` |
| `find_callees(name)` | What this function calls — **includes `ops_bind`** | `depth, limit` |
| `call_path(source, target)` | Call path between two functions | `max_len` |
| `find_references(name)` | Every use site of a symbol, with enclosing function | `limit` |

### Types & structure

| Tool | Purpose | Key params |
|---|---|---|
| `find_type_definition(name)` | Go-to-type-definition (`type_of` edges) | — |
| `get_struct_layout(name)` | Struct fields (`contains` edges) | — |
| `get_neighborhood(name)` | N-hop subgraph — token-efficient context pack | `depth, edge_types, summary` |

### Kernel-specific & meta

| Tool | Purpose | Key params |
|---|---|---|
| `find_ops_impls(field_name)` | **★** Function-pointer field → all implementations | `struct_type` |
| `index_status()` | Index metadata + statistics | — |

**★ `find_ops_impls` is the killer tool.** It resolves indirect calls through kernel
function-pointer tables (VFS ops, driver ops, net proto ops) that grep and syntax-based
tools cannot follow. One call to `find_ops_impls("read_iter")` returns every filesystem
and driver `read_iter` implementation across the kernel:

```
ext4_file_operations    → ext4_file_read_iter   @ fs/ext4/file.c
shmem_file_operations   → shmem_file_read_iter  @ mm/shmem.c
socket_file_ops         → sock_read_iter        @ net/socket.c
... (16 found)
```

### Token budget control

`find_callers`/`find_callees` accept `depth` and `limit`; `get_neighborhood` returns
compact `name + file:line` by default (`summary=true`). Agents stay within budget instead
of exploding into full subgraphs.

---

## How It Works

```
┌───────────────────────────────────────────────────────────────┐
│                        Your Code Agent                         │
│  "What implements ->read_iter?" → calls KGraph tools directly  │
└─────────────────────────────────┬─────────────────────────────┘
                                  │
                                  ▼
┌───────────────────────────────────────────────────────────────┐
│                     KGraph MCP Server (12 tools)               │
│  search · get_symbol · get_function_body · callers · callees   │
│  call_path · references · type_definition · struct_layout      │
│  neighborhood · ops_impls · index_status                       │
│                                  │                             │
│                                  ▼                             │
│              SQLite knowledge graph (.kgraph/kgraph.db)        │
│   symbols · occurrences · edges · ops_bind · subsystem         │
└───────────────────────────────────────────────────────────────┘
```

1. **Build** — `make CC=clang LLVM=1` produces `compile_commands.json` (what the compiler actually compiles).
2. **Index** — `scip-clang` emits `index.scip` with full semantic symbol information per compilation unit.
3. **Ingest** — Python (protobuf 7.x / upb) parses SCIP into `IngestBatch` objects, derives call edges
   from `enclosing_range`, derives `ops_bind` edges from function-pointer table initializations,
   writes into SQLite via the `GraphStore` interface.
4. **Enrich** — `KernelProfile` maps MAINTAINERS → subsystem labels, tags config-gated symbols.
5. **Serve** — MCP server exposes graph queries via recursive CTE on SQLite.

The `IngestBatch` → `GraphStore` boundary keeps the parser fully decoupled from storage,
so swapping SQLite for another backend (Neo4j, a custom embedded DB) means implementing
one new `GraphStore` — the parser, MCP tools, and agent integration don't change.

---

## CLI Reference

```bash
# Agent integration
kgraph install                     # auto-detect & configure installed agents
kgraph install --target <ids>      # configure specific agents (claude,cursor,codex,opencode,hermes)
kgraph install --location <loc>    # global (default) or local (per-project)
kgraph detect                      # show detected agents, write nothing
kgraph uninstall                   # remove kgraph config from agents

# Index lifecycle (run in the kernel source dir)
kgraph init <path>                 # index + ingest (--skip-build, --subsystem, --force)
kgraph ingest <path>               # re-ingest from an existing index.scip
kgraph serve --mcp                 # start the MCP server (usually auto-launched by the agent)
kgraph status <path>               # show index statistics and health
```

---

## Comparison with Existing Tools

| | **codegraph** | **semcode** | **KGraph** |
|---|---|---|---|
| Parsing backend | tree-sitter | tree-sitter | **SCIP-clang** |
| Semantic depth | syntax-level | syntax-level | **compiler-level** |
| Config awareness | no (all branches) | no (all branches) | **yes (only compiled code)** |
| Macro resolution | heuristic | heuristic | **clang preprocessor** |
| Function pointer calls | heuristic name-match | heuristic name-match | **ops_bind derived edges** |
| Type resolution | name-based | name-based | **clang-precise** |
| Kernel domain knowledge | none | git/lore/vectors | **MAINTAINERS/Kconfig/syscall** |
| Storage | SQLite | LanceDB | **SQLite** |
| Target scope | 20+ languages, general | C/Rust, kernel | **C, kernel-only, deep** |

> codegraph = breadth (many languages, fast install)
> semcode = kernel engineering (git/lore/vectors, syntax-level)
> **KGraph = compilation truth (config-aware, macro-resolved, function-pointer-callable)**

---

## Supported Kernel Profiles

| Kernel | Build system | Status |
|---|---|---|
| **Linux** | Kbuild (`CC=clang LLVM=1`) | **MVP** |
| Android | Soong + repo manifest.xml | Planned |
| Zephyr | CMake + west manifest.yml | Planned |
| FreeBSD | Make + `src.conf` | Planned |

Adding a new kernel profile means writing a `KernelProfile` subclass —
build pipeline + domain enrichment — without touching the ingest or query core.
See [DESIGN.md §6](docs/DESIGN.md) for the profile architecture.

---

## Project Structure

```
KGraph/
├── README.md / README.zh-CN.md     # this file (EN / 中文)
├── install.sh                      # one-line CLI installer
├── docs/
│   ├── DESIGN.md / DESIGN.zh-CN.md  # full architecture & rationale
│   └── scip-parser-design.md        # SCIP parser design notes
├── thirdparty/
│   └── scip.proto                  # canonical SCIP protobuf schema
├── scripts/
│   └── scip_pb2.py                 # generated protobuf bindings (7.x / upb)
├── src/
│   ├── parser/                     # SCIP protobuf → IngestBatch
│   │   ├── models.py               #   data model (the parser↔storage contract)
│   │   ├── scip_parser.py          #   parse + enclosing match + ops_bind derivation
│   │   └── symbol_name.py          #   SCIP symbol-string parser
│   ├── storage/                    # graph persistence
│   │   ├── graph_store.py          #   GraphStore interface (extension point)
│   │   └── sqlite_store.py         #   SQLite backend (WAL · FTS5 · recursive CTE)
│   └── installer/                  # agent auto-config
│       ├── orchestrator.py         #   detect() / install() / uninstall()
│       ├── cli.py                  #   `kgraph install` CLI
│       └── targets/                #   claude · cursor · codex · opencode · hermes
├── mcp/
│   ├── server.py                   # MCP server (12 tools)
│   ├── source_reader.py            # reads function bodies from disk
│   └── examples/                   # per-agent manual config snippets
└── tests/
    ├── test_parser_store.py        # parser → SQLite pipeline
    ├── test_mcp_server.py          # MCP tool smoke test
    └── ingest_real.py              # full-kernel ingestion
```

---

## Development Setup

If you're developing KGraph (not just using it as an end-user):

```bash
git clone https://github.com/ajksunkang/KGraph.git
cd KGraph

# Create venv with any python3.10+ and install protobuf 7.x (upb, matches protoc 35)
python3.10 -m venv .venv
source .venv/bin/activate
pip install "protobuf>=7.35.0,<8"
python -c "import google.protobuf; print(google.protobuf.__version__)"   # → 7.35.0

# Regenerate scip_pb2.py only if you change thirdparty/scip.proto
protoc --proto_path=thirdparty --python_out=scripts thirdparty/scip.proto

# Run tests (point KGRAPH_ROOT at an indexed kernel tree)
KGRAPH_ROOT=/path/to/linux .venv/bin/python tests/test_mcp_server.py
```

---

## Uninstall

```bash
kgraph uninstall               # remove kgraph MCP config from all agents
rm -rf /path/to/linux/.kgraph  # remove the graph database from a project
```

---

## License

MIT

---

<div align="center">

_Made for kernel developers and AI agents who need to see what the compiler sees._

[Report Bug](https://github.com/ajksunkang/KGraph/issues) · [Request Feature](https://github.com/ajksunkang/KGraph/issues)

</div>