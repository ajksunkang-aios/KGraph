[English](DESIGN.md) | [中文](DESIGN.zh-CN.md)

# KGraph — Compiler-Aware Kernel Graph Engine

> Extract kernel source into a **compiler-semantic** code knowledge graph via
> `compile_commands.json + scip-clang`, persist to SQLite, expose precise, compact,
> trimmable graph queries through MCP — so LLM agents analyzing kernel issues
> (crash root-cause, patch impact analysis) get the most relevant context with
> **minimal token / tool-call overhead**.

## 1. Technical Brand & Differentiation

**Core positioning**: **compiler-aware kernel graph engine** — index only what the compiler actually sees.

### 1.1 Competitive Landscape

| Dimension | **codegraph** (colbymchenry) | **semcode** (facebookexperimental) | **KGraph** |
|---|---|---|---|
| Parsing backend | tree-sitter (syntax-level) | tree-sitter (syntax-level) | **scip-clang (compiler-semantic)** |
| Storage backend | SQLite + FTS5 | LanceDB (vector DB) | SQLite + FTS5 |
| Language scope | 20+ languages, general | C/C++/Rust | **C (kernel), MVP: Linux** |
| Target scenario | AI agent general efficiency | Kernel engineering (git/lore/vectors) | **Kernel crash/patch root-cause — token×tool-call efficiency** |
| Call graph | heuristic name matching | tree-sitter AST name matching | **clang precise symbol resolution + ops_bind indirect calls** |
| Highlight UX | multi-agent install, framework routes, iOS/RN bridging | git range, lore/LKML, vector search, overlay | config-aware, macro-resolved, function-pointer-callable |
| Implementation | TypeScript/Node | Rust | **C (ingest) + Python (query/MCP)** |

### 1.2 Five Core Differentiators

> codegraph = breadth (20+ languages, syntax-level, fast install)
> semcode = kernel engineering (git/lore/vectors, still syntax-level)
> **KGraph = compilation truth (config-aware, macro-resolved, function-pointer-callable)** —
> purpose-built for kernel crash/patch root-cause with optimal token×tool-call efficiency.

1. **Config-aware**: Based on `compile_commands.json`, KGraph indexes only the code that this build actually compiles.
   tree-sitter treats all `#ifdef CONFIG_X` branches as code, producing massive dead-branch noise and false call edges;
   KGraph sees only what `x86_64 defconfig` activates. In the kernel, the same function behaves completely differently under different configs.

2. **Macro-resolved**: `EXPORT_SYMBOL`, `SYSCALL_DEFINE`, `container_of`, per-CPU macros, tracepoint macros —
   these form the kernel's skeleton. scip-clang indexes after preprocessing, yielding accurate symbol positions.
   tree-sitter can only heuristic-guess kernel macros (semcode itself says it keeps only function-like macros "for better signal-to-noise").

3. **Precise type/symbol resolution**: clang knows `f_op`'s real type is `struct file_operations *`,
   knows that same-named `static` functions across TUs are distinct symbols. tree-sitter relies on name matching —
   the kernel's many same-named static helpers cause misattribution.

4. **Indirect call / ops_bind (killer feature)**: `.read_iter = ext4_file_read_iter` in ops table initialization —
   clang can precisely attribute field→implementation function, deriving `ops_bind` edges.
   90% of the kernel's core control flow uses function-pointer tables (VFS/driver/net) —
   **pure tree-sitter tools effectively break here**. This is the most valuable edge for crash root-cause.

5. **SYSCALL_DEFINE → syscall entry reachable**: After expansion, clang connects macro-generated entries like `sys_read` into the call graph.
   Crash stacks typically walk down from syscall entry points.

**Candid cost acknowledgment**: Requires successful kernel compilation upfront; indexing is slower/heavier; per-commit indexing is expensive.
semcode's git/lore/overlay capabilities we don't have short-term — but these are peripheral, not graph quality, and can be added later.

---

## 2. Architecture

Streamlined three-layer + two domain abstractions, no unnecessary abstraction layers:

```
┌──────────────────────────────────────────────────────┐
│              MCP Server (Python)                       │
│  Tool set + token budget control (limit/depth/summary)│
│  search / definition / callers / callees / neighborhood│
│  call_path / struct_layout / ops_impls / subsystem     │
└───────────────┬──────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────┐
│              Query Engine (Python)                     │
│  Graph algorithms: k-hop / reverse call / shortest    │
│  path / rank-and-trim                                 │
│  Direct SQLite recursive CTE (no GraphStore layer)    │
└───────────────┬──────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────┐
│          libkgraph Ingest Core (C)                     │
│  scip.proto parse → normalize → bulk write SQLite     │
│  Derive: call graph (enclosing) + ops_bind + includes │
│  Direct SCIP integration (no IndexAdapter layer)      │
└───────────────┬──────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────┐
│          KernelProfile (Python)   ★retained           │
│                                                        │
│  ├── BuildPipeline                                     │
│  │     Mono-repo: build_cmd, config_cmd, post_build    │
│  │     Multi-repo: manifest_parser, repo_sync, cc_merge│
│  │     No CC support: intercept_build (bear/scan-build)│
│  │                                                     │
│  └── DomainEnrichment                                  │
│  │     Linux: MAINTAINERS→subsystem, Kconfig filtering,│
│  │           syscall table, ops struct field mapping    │
│  │     Android: .hal→HIDL, selinux, binder (future)    │
│  │     Zephyr: Kconfig(different syntax), DT, west     │
│  │     FreeBSD: __FreeBSD_version, sys/kconf           │
└──────────────────────────────────────────────────────┘

Pipeline (Python orchestration):
  kernel src → KernelProfile.BuildPipeline → compile_commands.json
            → scip-clang → index.scip
            → libkgraph ingest → SQLite
            → KernelProfile.DomainEnrichment → write subsystem/config metadata
```

**Design decision log**:

| Decision | Choice | Reason |
|---|---|---|
| IndexAdapter layer | **Removed** — SCIP directly | YAGNI; brand = compiler-aware, only scip-clang today |
| GraphStore layer | **Removed** — SQLite directly | Deployment simplicity (single .db), WAL batch-write throughput sufficient, recursive CTE read perf sufficient; codegraph validates feasibility; future Neo4j = refactor Query Engine internals SQL→Cypher |
| KernelProfile | **Retained** | Build system differences (mono/multi-repo) and domain enrichment are kernel-identity-bound knowledge; new kernel = new Profile only |
| C/Python split | C for ingest hot path, Python for query/MCP/orchestration | 10M+ record bulk write needs C; iterative logic uses Python |

---

## 3. Data Model

### 3.1 Node and Edge Types

**Node types**: `function / struct / field / macro / typedef / global_var / file`

**Edge types**:

| Edge type | Meaning | Source |
|---|---|---|
| `calls` | A directly calls B | enclosing_range derivation |
| `references` | A references B (non-call) | occurrence role derivation |
| `defines` | file/macro defines symbol | SCIP definition occurrence |
| `contains` | struct contains field; file contains symbol | SCIP relationship |
| `includes` | file #include file | SCIP relationship |
| `ops_bind` | ops variable binds field→implementation (indirect call) | enclosing + initialization pattern derivation, ★core differentiator |
| `type_of` | variable/parameter's type is a type | SCIP signature info |
| `macro_expands` | macro expands at position | SCIP occurrence |

### 3.2 SQLite Schema

```sql
-- ===== Symbol nodes =====
CREATE TABLE symbols(
  id              INTEGER PRIMARY KEY,
  scip_symbol     TEXT UNIQUE,            -- SCIP globally-unique symbol string (cross-TU resolution)
  name            TEXT NOT NULL,
  kind            TEXT NOT NULL,           -- function / struct / field / macro / typedef / global_var
  signature       TEXT,                   -- function signature / struct declaration
  documentation   TEXT,                   -- SCIP documentation
  def_file_id     INTEGER REFERENCES files(id),
  def_start_line  INTEGER,
  def_end_line    INTEGER,                -- from enclosing_range
  is_external     INTEGER DEFAULT 0,      -- 1 = definition not found in this index (header-only symbols etc.)
  subsystem       TEXT                    -- written by KernelProfile.DomainEnrichment
);

CREATE TABLE files(
  id          INTEGER PRIMARY KEY,
  path        TEXT UNIQUE NOT NULL,
  language    TEXT,                        -- C / header
  subsystem   TEXT,                        -- written by KernelProfile (MAINTAINERS parsing)
  sha         TEXT                         -- content hash (incremental update detection)
);

-- ===== Definition/reference occurrences =====
CREATE TABLE occurrences(
  id                  INTEGER PRIMARY KEY,
  symbol_id           INTEGER NOT NULL REFERENCES symbols(id),
  file_id             INTEGER NOT NULL REFERENCES files(id),
  start_line          INTEGER NOT NULL,
  start_col           INTEGER NOT NULL,
  end_line            INTEGER NOT NULL,
  end_col             INTEGER NOT NULL,
  role                INTEGER NOT NULL,    -- SCIP SymbolRole bitmask
  enclosing_symbol_id INTEGER REFERENCES symbols(id)  -- ★derived: which function body this reference sits in
);

CREATE INDEX idx_occ_symbol ON occurrences(symbol_id);
CREATE INDEX idx_occ_file ON occurrences(file_id, start_line);
CREATE INDEX idx_occ_enclosing ON occurrences(enclosing_symbol_id);

-- ===== Generic edge table =====
CREATE TABLE edges(
  src_id      INTEGER NOT NULL REFERENCES symbols(id),
  dst_id      INTEGER NOT NULL REFERENCES symbols(id),
  type        TEXT NOT NULL,               -- calls / references / defines / contains / includes / ops_bind / type_of / macro_expands
  file_id     INTEGER REFERENCES files(id),
  line        INTEGER,
  weight      INTEGER DEFAULT 1,           -- call frequency (merged same-location edges >1)
  confidence  REAL DEFAULT 1.0,            -- indirect/macro edges use low confidence (0.3-0.7)
  metadata    TEXT,                         -- JSON: ops_bind has field_name; macro_expands has expansion_text
  PRIMARY KEY(src_id, dst_id, type, file_id, line)
);

CREATE INDEX idx_edge_src ON edges(src_id, type);
CREATE INDEX idx_edge_dst ON edges(dst_id, type);     -- ★reverse call graph relies on this
CREATE INDEX idx_edge_type ON edges(type);

-- ===== Full-text search (optional FTS5) =====
-- CREATE VIRTUAL TABLE symbols_fts USING fts5(name, signature, documentation, content=symbols, content_rowid=id);

-- ===== Metadata =====
CREATE TABLE meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Stores: kernel_name, kernel_version, config_name, index_timestamp, scip_version, etc.
```

### 3.3 Key Derivation Steps (C-side ingest)

**Call graph derivation**:
For each non-definition occurrence (function B at position P in file F), find the function definition A whose `enclosing_range` covers P
→ write `A --calls--> B` edge.

**ops_bind derivation (core differentiator)**:
```
static struct file_operations ext4_file_operations = {
    .read_iter      = ext4_file_read_iter,    ← SCIP records reference to ext4_file_read_iter
    .write_iter     = ext4_file_write_iter,
};
```
SCIP's reference to `ext4_file_read_iter` encloses into global variable `ext4_file_operations`
→ write `ext4_file_operations --ops_bind{field=read_iter}--> ext4_file_read_iter`.
Later, `f_op->read_iter()` can resolve candidates via "field name → all ops_bind bindings" (confidence=0.5, low-confidence annotation).

---

## 4. MCP Tool Set

Designed for token efficiency; every tool carries budget parameters.

| Tool | Purpose | Key params | Token-saving mechanism |
|---|---|---|---|
| `search_symbols(q)` | Find symbols by name / regex / FTS | `kind, limit` | limit truncation |
| `get_definition(sym)` | Definition location + signature (no full file) | — | Only location + signature |
| `find_callers(sym)` | Reverse call graph (who calls me) | `depth, limit` | depth controls explosion |
| `find_callees(sym)` | Forward call graph | `depth, limit` | ditto |
| `get_neighborhood(sym)` | N-hop subgraph | `depth, edge_types, summary` | summary=true → names + file:line only |
| `call_path(a, b)` | Call path between two functions | `max_len` | Only path, no source |
| `get_struct_layout(t)` | Struct fields and layout | — | Compact table format |
| `find_ops_impls(field)` | Function-pointer field → candidate implementations | `struct_type` | ★indirect call core tool |
| `which_subsystem(sym/file)` | MAINTAINERS subsystem ownership | — | Single-value return |
| `expand_macro(name)` | Macro definition body | — | Only macro body |

**Token-saving三板斧**:
1. `summary` mode: return only name + file:line, omit signature/docs/source
2. `depth / limit`: control graph traversal explosion
3. Results sorted by "graph distance + call frequency" then truncated — most relevant first

---

## 5. C / Python Division of Labor

| Component | Language | Reason |
|---|---|---|
| `libkgraph` ingest: SCIP protobuf parsing, bulk SQLite write, call graph / ops_bind derivation | **C** | 10M+ record hot path; efficient protobuf-c; standalone CLI or CFFI to Python |
| Pipeline orchestration, KernelProfile, Query Engine, MCP Server | **Python** | Fast iteration; MCP SDK ecosystem; algorithm layer isn't hot path |

**Contract = SQLite Schema**. C and Python collaborate through the same .db file, fully decoupled.

---

## 6. KernelProfile Design

### 6.1 BuildPipeline

```
KernelProfile.BuildPipeline
  │
  ├── LinuxBuildPipeline (MVP)
  │     config_cmd:  make CC=clang LLVM=1 x86_64_defconfig
  │     build_cmd:   make CC=clang LLVM=1 -j$(nproc)
  │     cc_cmd:      scripts/clang-tools/gen_compile_commands.py
  │     scip_cmd:    scip-clang --compilation-database compile_commands.json
  │
  ├── AndroidBuildPipeline (future)
  │     manifest:    repo manifest.xml
  │     sync:        repo sync
  │     build:       soong_ui --makecode
  │     cc_merge:    merge multi-repo compile_commands.json
  │
  ├── ZephyrBuildPipeline (future)
  │     manifest:    west manifest.yml
  │     sync:        west update
  │     build:       west build -b <board>
  │     cc_cmd:      auto-generated in build/zephyr/
  │     cc_merge:    merge multi-repo
  │
  └── FreeBSDBuildPipeline (future)
        build:      bear make buildworld
        cc_cmd:     bear intercept
```

### 6.2 DomainEnrichment

```
KernelProfile.DomainEnrichment
  │
  ├── LinuxEnrichment (MVP)
  │     MAINTAINERS  → files.subsystem, symbols.subsystem
  │     Kconfig      → config-aware annotation (which symbols activate under which CONFIG)
  │     syscall table → syscall entry → kernel function mapping
  │     ops struct registry → semantic mapping for common function-pointer table fields
  │
  ├── AndroidEnrichment (future)
  │     .hal → HIDL/AIDL interface definitions
  │     selinux policy → permission constraint annotations
  │     binder → IPC cross-process call edges
  │
  ├── ZephyrEnrichment (future)
  │     devicetree → hardware topology nodes
  │     Kconfig (Zephyr syntax) → config annotations
  │     west manifest → multi-repo topology
  │
  └── FreeBSDEnrichment (future)
        __FreeBSD_version → version annotations
        sys/kconf → config annotations
```

### 6.3 Multi-Repo Merge Strategy

```
manifest_parser(repo_xml / west_yml)
  → repo list + version locks + path mappings

per_repo_build()
  → each sub-repo independently generates compile_commands.json

cc_merge(cc_list[])
  → merge multiple compile_commands.json files
  → fix relative paths to unified root directory paths
  → deduplicate (same file may appear in multiple sub-repo CCs)
  → output: merged_compile_commands.json

Then: scip-clang → index.scip → libkgraph ingest (single DB)
```

---

## 7. Processing Pipeline

```
P0 Env check    : clang / scip-clang / protobuf-c toolchain, kernel compilable
P1 Build output : KernelProfile.BuildPipeline → compile_commands.json
P2 Index output : scip-clang --compilation-database compile_commands.json → index.scip
P3 Parse+load   : libkgraph ingest → SQLite (symbols / occurrences / edges)
P4 Graph derive : calls (enclosing) + ops_bind + includes
P5 Enrichment   : KernelProfile.DomainEnrichment → subsystem / config / syscall metadata
P6 Query engine : k-hop / reverse call / path / rank-and-trim (recursive CTE)
P7 MCP service  : tool exposure + token budget control
P8 KBench       : kernel_bench_data.json integration, quantify token/tool-call/precision-recall
```

---

## 8. Expected Results

### 8.1 Walkthrough

**Scenario**: KBench provides a crash, top-of-stack function `ext4_file_read_iter`.

```
Agent: find_callers("ext4_file_read_iter", depth=2)
  → ~10 callers (including VFS indirect entry via ops_bind reverse), names + file:line only

Agent: get_neighborhood("ext4_file_read_iter", depth=1, summary=true)
  → 1-hop subgraph: inode lock / page cache related functions, compact structured

Agent: find_ops_impls("read_iter", struct_type="file_operations")
  → all file_operations.read_iter bound implementations, including ext4_file_read_iter

→ Agent gets root-cause relevant function set in 3 tool-calls, ~1.5k tokens
  Baseline (grep + full-file reads) typically 10k+ tokens, scattered hits
```

### 8.2 Quantifiable expected gains (pending KBench validation)

Same root-cause identification quality:
- **Token cost: one order of magnitude lower**
- **Tool-calls: single-digit count**
- **Recall against oracle_methods: improved via ops_bind indirect edges**

---

## 9. Feasibility Analysis

| Dimension | Assessment | Key risk | Mitigation |
|---|---|---|---|
| compile_commands generation | ✅ Mature | Kernel must compile successfully | MVP: `CC=clang LLVM=1` x86_64 defconfig, most stable |
| scip-clang indexing | ✅ Feasible | Kernel GCC extensions / inline asm; full index slow | MVP: subsystem/directory-scope indexing (e.g. only `fs/`) |
| Direct call graph | ✅ Strong | — | SCIP Occurrence with enclosing_range → direct caller→callee derivation |
| Indirect calls (function pointers / ops) | ⚠️ Partially solvable | `file->f_op->read_iter()` SCIP can't resolve | ops_bind derivation + field-name→impl heuristic (low-confidence annotation) |
| Macros | ⚠️ Moderate | Kernel macros extremely heavy | scip-clang indexes after preprocessing, most resolved; complex macros annotated low-confidence |
| Per-base_commit indexing | ⚠️ High cost | KBench: each case uses a different commit | Tiered: (commit,config) cache + dependency-closure scoped indexing; MVP: fixed snapshot |
| Storage scale | ✅ SQLite handles it | Million-level occurrences | WAL + batch transactions + proper indexes + FTS5 |

---

## 10. KBench Efficiency Evaluation Design

Reuses `kernel_bench_data.json` (contains `crash_report_data / base_commit / oracle_methods`).

**Metrics**:
- **Efficiency**: total tokens, tool-call count, wall-clock per case
- **Quality**: predicted method set vs `oracle_methods` — file-level / method-level **precision / recall / F1 / IoU**
- **Control groups**: A=plain text (grep/read) B=semcode MCP C=KGraph MCP

**Evaluation script**: Python — read bench data → construct prompt per case → call Claude + MCP → collect results → compute metrics.

---

## 11. MVP Milestones

| Milestone | Delivery | Acceptance |
|---|---|---|
| M1 Build pass | `fs/ext4` subsystem compile_commands.json | clang compile: zero fatal errors |
| M2 Index output | `fs/ext4` index.scip | scip-clang: zero fatal errors |
| M3 Load+Schema | libkgraph C loader + SQLite | symbol/occurrence/edge counts reasonable |
| M4 Graph derivation | calls + ops_bind | sampled function caller/callee correct |
| M5 Kernel enrichment | MAINTAINERS→subsystem | subsystem fields populated correctly |
| M6 Query engine | k-hop / reverse / path (recursive CTE) | query performance <100ms |
| M7 MCP service | Tool set + budget control | Claude can invoke all tools |
| M8 KBench integration | Evaluation script + report | N cases run with metrics output |

---

## 12. Open Decision Points (pending)

| # | Decision | Current leaning | Notes |
|---|---|---|---|
| D1 | Index scope | Start with `fs/ext4` subsystem, expand later | Full defconfig expensive but closer to KBench real needs |
| D2 | Per-base_commit indexing strategy | MVP: fixed snapshot, incremental at KBench stage | Accept drift vs benchmark commit |
| D3 | C boundary | Python-only first pass, then swap ingest to C | Faster iteration; you specified C+Python |
| D4 | ops_bind in MVP | Recommended: do in MVP | Core differentiator — skip = no advantage |
| D5 | Semcode-capability backlog | Roadmap: git-range / overlay / lore | Doesn't block MVP, but must be planned |