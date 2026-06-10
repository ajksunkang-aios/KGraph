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

## Get Started

### Step 1: Prepare a Kernel Build Environment (Docker recommended)

KGraph is compiler-aware — it indexes what the compiler actually sees. You need a kernel build environment
that can produce `compile_commands.json` with **clang**.

The recommended approach is Docker, which gives you a clean, reproducible build environment
without polluting your host system:

```bash
# Use the provided Dockerfile (or your own kernel build image)
docker build -t kgraph-linux-build -f Dockerfile .

# Or pull a pre-built image (if available)
# docker pull ajksunkang/kgraph-linux-build:latest

# Run the build container with kernel source mounted
docker run -it -v /path/to/linux:/kernel kgraph-linux-build bash
```

<details>
<summary><strong>Dockerfile example for Linux x86_64 defconfig</strong></summary>

```dockerfile
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    clang llvm gcc make bc flex bison libelf-dev \
    libssl-dev libncurses-dev python3 python3-pip \
    git curl protobuf-compiler && \
    rm -rf /var/lib/apt/lists/*

# Install scip-clang
RUN curl -fsSL https://github.com/sourcegraph/scip-clang/releases/latest/download/scip-clang-linux-x64.tar.gz \
    | tar xz -C /usr/local/bin/

WORKDIR /kernel
```

</details>

**Inside the container** (or any environment with `clang` + `make`):

```bash
cd /kernel

# Generate x86_64 defconfig
make CC=clang LLVM=1 x86_64_defconfig

# Build kernel (produces compile_commands.json)
make CC=clang LLVM=1 -j$(nproc)

# Or generate compile_commands.json without full build:
make CC=clang LLVM=1 prepare
scripts/clang-tools/gen_compile_commands.py
```

<details>
<summary><strong>Without Docker — native build</strong></summary>

If you prefer building natively, ensure these are installed:

- **clang** (≥ 14) and **LLVM** tools
- **kernel build dependencies**: `bc flex bison libelf-dev libssl-dev`
- **scip-clang**: download from [github.com/sourcegraph/scip-clang](https://github.com/sourcegraph/scip-clang)

```bash
# macOS (Homebrew)
brew install clang llvm protobuf

# Ubuntu/Debian
sudo apt install clang llvm protobuf-compiler \
    bc flex bison libelf-dev libssl-dev

# Then build as above
cd /path/to/linux
make CC=clang LLVM=1 x86_64_defconfig
make CC=clang LLVM=1 -j$(nproc)
```

</details>

### Step 2: Build `compile_commands.json`

This step produces the compilation database — the list of exactly which `.c` files get compiled
under your chosen config, with the exact compiler flags.

```bash
# Inside the build environment (Docker or native):
cd /kernel

# Full build + compile_commands.json
make CC=clang LLVM=1 -j$(nproc)
scripts/clang-tools/gen_compile_commands.py

# Verify it exists
ls -lh compile_commands.json
# Should be ~5-50MB depending on config scope
```

**Key point**: `compile_commands.json` is config-aware — it only lists the files that your `defconfig`
actually compiles. This is what makes KGraph different from syntax-only tools.

### Step 3: Install KGraph

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/ajksunkang/KGraph/main/install.sh | bash
```

This downloads `kgraph` to `~/.local/bin` and registers it on your `PATH`.
Open a **new terminal** so the command resolves.

<sub>Already have the repo cloned? You can also run `./install.sh` from the project root.</sub>

### Step 4: Initialize KGraph in the Kernel Source

```bash
# Inside the build environment, in the kernel source directory:
cd /kernel
kgraph init .
```

`kgraph init` does the following automatically:

1. **Create venv** — sets up a Python 3.10+ virtual environment at `.venv/` within the KGraph project
2. **Install protobuf** — installs `protobuf>=7.35.0` (upb format) into the venv,
   matching the protoc version used to generate `scip_pb2.py`
3. **Index** — runs `scip-clang --compilation-database compile_commands.json` → `index.scip`
4. **Ingest** — parses SCIP protobuf, derives call graph + ops bindings, writes into `.kgraph/kgraph.db`
5. **Enrich** — maps MAINTAINERS → subsystem labels

<details>
<summary><strong>Manual venv setup (if kgraph init fails)</strong></summary>

If the automatic venv setup doesn't work (e.g. no python3.10+ on the system),
set it up manually:

```bash
# Find python3.10+ on your system
# macOS (Homebrew): /opt/homebrew/bin/python3.10
# Linux: /usr/bin/python3.10 or python3.12

PYTHON3=/opt/homebrew/bin/python3.10   # adjust to your system

# Create venv in KGraph project
$PYTHON3 -m venv /path/to/KGraph/.venv

# Activate and install protobuf
source /path/to/KGraph/.venv/bin/activate
pip install "protobuf>=7.35.0,<8"

# Verify
python -c "import google.protobuf; print(google.protobuf.__version__)"
# Should print: 7.35.0
```

</details>

<details>
<summary><strong>Indexing scope options</strong></summary>

```bash
# Skip build step (compile_commands.json already exists):
kgraph init . --skip-build

# Index only a subsystem (faster for MVP / testing):
kgraph init . --subsystem fs/ext4

# Force full re-index:
kgraph init . --force

# Multiple subsystems
kgraph init . --subsystem fs/ext4,net/ipv4,mm
```

</details>

### Step 5: Install MCP Tools into Your Code Agent

```bash
kgraph install
```

Detects and auto-configures MCP server integration for installed agents:

- **Claude Code** — writes MCP server config + auto-allow permissions
- **Cursor** — writes `.cursor/mcp.json`
- **Codex CLI** — writes MCP config
- **Other MCP-compatible agents** — prints the config snippet for manual wiring

<details>
<summary><strong>Non-interactive (scripting / CI)</strong></summary>

```bash
kgraph install --yes                          # auto-detect agents, accept defaults
kgraph install --target=claude,cursor --yes    # explicit agent list
kgraph install --print-config claude           # print snippet, no file writes
```

| Flag | Values | Default |
|---|---|---|
| `--target` | `auto`, `all`, `none`, or csv (`claude,cursor,...`) | prompt |
| `--yes` | (boolean) accept defaults | prompt every step |
| `--print-config <id>` | dump snippet for one agent and exit | — |

</details>

### Step 6: Use Your Agent with KGraph

Restart your agent so the MCP server loads. Then ask questions:

```
> What functions call ext4_file_read_iter?
> How does a read request flow from VFS down to ext4?
> Which subsystem owns mm/page_alloc.c?
> Find all implementations of file_operations.read_iter
```

Your agent will call KGraph MCP tools automatically when `.kgraph/` exists in the project root.

---

## MCP Tools

| Tool | Purpose | Key params |
|---|---|---|
| `search_symbols(q)` | Find symbols by name / regex / FTS | `kind, limit` |
| `get_definition(sym)` | Definition location + signature (no full file) | — |
| `find_callers(sym)` | Who calls this function (reverse call graph) | `depth, limit` |
| `find_callees(sym)` | What this function calls (forward call graph) | `depth, limit` |
| `get_neighborhood(sym)` | N-hop subgraph — the most token-efficient context pack | `depth, edge_types, summary` |
| `call_path(a, b)` | Call path between two functions | `max_len` |
| `get_struct_layout(type)` | Struct fields and layout | — |
| `find_ops_impls(field)` | Function-pointer field → all candidate implementations **★** | `struct_type` |
| `which_subsystem(sym)` | MAINTAINERS subsystem ownership | — |
| `expand_macro(name)` | Macro definition body | — |

**★ `find_ops_impls` is the killer tool** — it resolves indirect calls through kernel function-pointer
tables (VFS ops, driver ops, net proto ops) that syntax-based tools cannot follow.

### Token Budget Control

Every tool accepts `summary=true` to return only names + file:line (no signatures, no docs, no source).
Combined with `depth` and `limit`, agents stay within budget instead of exploding into full subgraphs.

---

## How It Works

```
┌───────────────────────────────────────────────────────────────┐
│                        Your Code Agent                         │
│                                                               │
│  "How does a read reach ext4_file_read_iter?"                 │
│         calls KGraph tools directly                           │
│                         │                                     │
└─────────────────────────┼─────────────────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────────────────┐
│                     KGraph MCP Server                          │
│                                                               │
│   search / callers / callees / neighborhood / ops_impls / ..  │
│                         │                                     │
│                         ▼                                     │
│              SQLite knowledge graph (.kgraph/kgraph.db)       │
│   symbols · occurrences · edges · ops_bind · subsystem        │
└───────────────────────────────────────────────────────────────┘
```

1. **Build** — `make CC=clang LLVM=1` produces `compile_commands.json` (what the compiler actually compiles).
2. **Index** — `scip-clang` emits `index.scip` with full semantic symbol information per compilation unit.
3. **Ingest** — Python (protobuf 7.x / upb) parses SCIP, derives call edges from `enclosing_range`,
   derives `ops_bind` edges from function-pointer table initializations, writes into SQLite.
4. **Enrich** — `KernelProfile` maps MAINTAINERS → subsystem labels, tags config-gated symbols.
5. **Serve** — MCP server exposes graph queries via recursive CTE on SQLite.

---

## CLI Reference

```bash
kgraph install                     # Register MCP server into code agents
kgraph init <path>                 # Build + index + ingest (--skip-build, --subsystem, --force)
kgraph index <path>                # Re-index SCIP only (no rebuild)
kgraph ingest <path>               # Re-ingest from existing index.scip
kgraph serve --mcp                 # Start MCP server (usually auto-launched by agent)
kgraph query <search>              # CLI symbol search (--kind, --limit, --json)
kgraph callers <symbol>            # CLI reverse call graph
kgraph callees <symbol>            # CLI forward call graph
kgraph status <path>               # Show index statistics and health
kgraph uninstall                   # Remove MCP config from all agents
kgraph uninit <path>               # Remove .kgraph/ from a project
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
├── docs/
│   ├── DESIGN.md          # Full architecture & rationale
├── README.md              # This file (English)
├── thirdparty/
│   └── scip.proto         # Canonical SCIP protobuf schema
├── scripts/
│   └── scip_pb2.py        # Generated Python protobuf bindings (protobuf 7.x / upb)
├── .venv/                 # Python 3.10+ venv with protobuf>=7.35
├── src/
│   ├── libkgraph/         # C: SCIP parser, SQLite bulk ingest, graph derivation
│   ├── kgraph/            # Python: CLI, KernelProfile, QueryEngine, MCP server
│   └── mcp/               # Python: MCP tool definitions & server
├── tests/
└── docs/
    └── scip-parser-design.md  # SCIP parser design notes
```

---

## Development Setup

If you're developing KGraph (not just using it as an end-user):

```bash
# Clone the repo
git clone https://github.com/ajksunkang/KGraph.git
cd KGraph

# Create and activate venv
/opt/homebrew/bin/python3.10 -m venv .venv   # or any python3.10+ on your system
source .venv/bin/activate

# Install protobuf 7.x (upb format, matches protoc 35)
pip install "protobuf>=7.35.0,<8"

# Verify
python -c "import google.protobuf; print(google.protobuf.__version__)"
# → 7.35.0

# Regenerate scip_pb2.py (only if you change thirdparty/scip.proto)
protoc --proto_path=thirdparty --python_out=scripts thirdparty/scip.proto

# Verify generated bindings
python -c "import sys; sys.path.insert(0,'scripts'); import scip_pb2; print('OK')"
```

---

## Uninstall

```bash
kgraph uninstall               # Remove MCP config from all agents
kgraph uninit /path/to/linux   # Remove .kgraph/ from a project
```

---

## License

MIT

---

<div align="center">

_Made for kernel developers and AI agents who need to see what the compiler sees._

[Report Bug](https://github.com/ajksunkang/KGraph/issues) · [Request Feature](https://github.com/ajksunkang/KGraph/issues)

</div>