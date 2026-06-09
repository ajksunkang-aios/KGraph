<div align="center">

# KGraph

### Compiler-Aware Kernel Graph Engine · MCP Tool Service

**Config-aware · Macro-resolved · Function-pointer-callable · SQLite-native**

### [Design Document →](DESIGN.md)

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

### 1. Download & Register

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/ajksunkang/KGraph/main/install.sh | bash
```

This downloads `kgraph` to `~/.local/bin` (or your preferred prefix) and registers it on your `PATH`.
Open a **new terminal** so the command resolves.

<sub>Already have the repo cloned? You can also run `./install.sh` from the project root.</sub>

### 2. Install MCP Tools into Your Code Agent

```bash
kgraph install
```

Detects and auto-configures MCP server integration for installed agents:

- **Claude Code** — writes MCP server config + auto-allow permissions
- **Cursor** — writes `.cursor/mcp.json`
- **Codex CLI** — writes MCP config
- **Other MCP-compatible agents** — prints the config snippet for manual wiring

<sub>This is the step that actually connects KGraph to your agent. Step 1 only puts the CLI on your PATH.</sub>

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

### 3. Initialize a Kernel Project

```bash
cd /path/to/linux        # any kernel source tree with compile_commands.json support
kgraph init .            # build index and persist into SQLite
```

`kgraph init` does three things:

1. **Build** — runs `make CC=clang LLVM=1 <defconfig>` + `make` to produce `compile_commands.json`
   (skipped if `compile_commands.json` already exists)
2. **Index** — runs `scip-clang` against the compilation database → `index.scip`
3. **Ingest** — runs `libkgraph` to parse SCIP, derive the call graph + ops bindings, write into `.kgraph/kgraph.db`

```bash
# Skip build (you already have compile_commands.json):
kgraph init . --skip-build

# Index only a subsystem (faster for MVP / testing):
kgraph init . --subsystem fs/ext4

# Force full re-index:
kgraph init . --force
```

<details>
<summary><strong>Indexing scope</strong></summary>

By default, `kgraph init` indexes the entire compilation database.
For large kernels (30M+ lines), this can take **20–60 min** depending on hardware.

```bash
# Targeted indexing — only compile & index files under fs/ext4/
kgraph init . --subsystem fs/ext4

# Targeted indexing — custom file list
kgraph init . --files fs/ext4/*.c,mm/*.c

# Multiple subsystems
kgraph init . --subsystem fs/ext4,net/ipv4,mm
```

Targeted mode compiles only the relevant objects and indexes only their SCIP output.
Full mode compiles the entire kernel. Both produce a single `.kgraph/kgraph.db`.

</details>

### 4. Use Your Agent with KGraph

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
3. **Ingest** — `libkgraph` (C) parses SCIP protobuf, derives call edges from `enclosing_range`,
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
See [DESIGN.md §6](DESIGN.md) for the profile architecture.

---

## Project Structure

```
KGraph/
├── DESIGN.md              # Full architecture & rationale
├── README.md              # This file
├── install.sh             # One-line installer
├── src/
│   ├── libkgraph/         # C: SCIP parser, SQLite bulk ingest, graph derivation
│   ├── kgraph/            # Python: CLI, KernelProfile, QueryEngine, MCP server
│   └── mcp/               # Python: MCP tool definitions & server
├── tests/
└── scripts/
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