[English](lazy-indexing-design.md) | [中文](lazy-indexing-design.zh-CN.md)

# KGraph Lazy-Indexing Design and Evaluation

> An incremental index-update mechanism — build-driven, compiler-aligned, and git-staging-aware.
> This document evaluates the **feasibility / complexity / necessity** of the approach and presents the design.

## 0. TL;DR

| Dimension | Rating | In one sentence |
|---|---|---|
| **Feasibility** | ✅ High | All the parts already exist: file-level DB granularity, the `sha` field, Kbuild dependency tracking, and git state |
| **Complexity** | ⚠️ Medium | The main difficulties are cross-file edge invalidation and the header-file dependency closure — but **the build has already computed these for us** |
| **Necessity** | ✅ High (KBench) / ⚠️ Medium (interactive dev) | A full re-index takes ~16 minutes; for KBench's multi-commit scenario, incremental is a hard requirement |

**Core insight**: codegraph listens to **file saves** (syntactic level — anything stored can be parsed);
KGraph should listen to **builds** (semantic level — only what has compiled counts).
A file that is changed but not built is not yet "compile truth," it's just a draft — so we don't index it and fall back to grep.
This makes lazy-indexing naturally consistent with KGraph's "compiler-aware" identity.

---

## 1. Background: How It Differs Fundamentally from codegraph auto-sync

| | **codegraph auto-sync** | **KGraph lazy-indexing** |
|---|---|---|
| Trigger | File save (FSEvents/inotify watcher + debounce) | Build completion (make pipeline / explicit sync) |
| Parse level | Syntactic (tree-sitter, no compilation needed) | Semantic (scip-clang, requires compilation) |
| Persistent process | Requires a daemon listening continuously | No daemon, triggered on demand |
| Unbuilt files | Immediately re-parsed (syntax can always parse) | **Not indexed** (uncompiled = not truth), grep fallback |
| Freshness | Always newest (including drafts) | Always a stable snapshot of the "last successful build" |

**Why can't KGraph simply copy auto-sync?**
- Indexing a TU with scip-clang requires the **full compile context** (macro expansion, headers, config) —
  a half-saved file that references symbols not yet defined will make scip-clang error out or produce wrong symbols.
- The semantics of a kernel source file are only settled when "compilation passes." Listening to saves means indexing a large number of transient drafts, which is wasteful and unreliable.

**The meaning of "lazy"**: rather than eagerly listening to every save, it **defers index updates lazily until the build naturally re-validates those files**.
The build is that lazy trigger point.

---

## 2. Core Mechanism: The Build Has Already Computed the Dependency Closure for Us

The hardest sub-problem of incremental indexing is **header-file dependencies**:
changing `fs/ext4/inode.c` → affects only this one TU (easy);
changing `include/linux/fs.h` → affects thousands of TUs (the header dependency closure, hard).

**Key insight: we don't need to compute the header → TU mapping ourselves — Kbuild has already done it.**

After `make`:
- The `.o` files that get recompiled have their mtime updated
- Kbuild records each object's complete header dependency set in the `.<obj>.o.cmd` file
- **The set of "which .o files got rebuilt after the build" is exactly "the set of TUs that need re-indexing"**

So the cost of incremental indexing is naturally ∝ the build cost:
- Change a leaf `.c` → rebuild one `.o` → re-index one TU (seconds)
- Change `fs.h` → rebuild thousands of `.o` → re-index thousands of TUs (close to a full index, but a full index is the right thing here anyway)

This is a very nice property — **the cost of lazy-indexing tracks the cost of the build precisely**, no more and no less.

---

## 3. Stable vs Unstable: Git-Staging-Aware

The DB always reflects a **committed, compiled, stable snapshot**. Distinguish three categories of files:

```
┌─────────────────────────────────────────────────────────────┐
│ File-state classification (git + build)                     │
├─────────────────────────────────────────────────────────────┤
│ ① Committed + built + content hash differs from the DB      │
│    → stable but stale → ★ incremental re-index target       │
│                                                             │
│ ② Committed + content hash identical to the DB              │
│    → stable and up to date → skip (already indexed)         │
│                                                             │
│ ③ Modified in the worktree/staging area (git status dirty)  │
│    → unstable (draft, high probability of not building /    │
│      frequent changes)                                      │
│    → ★ not indexed; MCP returns a grep fallback signal      │
└─────────────────────────────────────────────────────────────┘
```

**Why staging-area files are not indexed (the user's core requirement)**:
1. High probability of not building — scip-clang indexing will fail or produce wrong symbols
2. Frequent changes — once indexed they immediately go dirty again, wasteful
3. Even if compiled, they are transient drafts, not worth polluting the stable snapshot

**The grep fallback signal**: the MCP server checks the git status of the file containing the target symbol on every query.
If the file is dirty, it prepends a banner to the result:

```
⚠ fs/ext4/inode.c has uncommitted modifications — the indexed version may be stale; read/grep it directly for live content.
```

This works on the same principle as codegraph's staleness banner, but it is **driven by git status rather than a file watcher**.

---

## 4. Incremental Indexing Flow (7 Steps)

```
Trigger: kgraph sync  (or a post-hook wrapped by kgraph build inside the make pipeline)

P1. Read the baseline
    Read last_index_timestamp T and last_index_commit C from the meta table

P2. Find rebuilt TUs
    Scan every .o listed in compile_commands.json
    Keep those with mtime > T → these TUs were recompiled after the last index

P3. git stability filtering
    For the .c source of each candidate TU:
      - git status dirty (worktree/staging modifications) → mark unstable, drop
      - committed and content-hash differs from files.sha → keep as incremental target
      - hash identical → drop (mtime changed but content didn't, e.g. git checkout)

P4. Localized scip-clang
    Generate filtered_compile_commands.json (only the incremental target TUs)
    scip-clang --compdb-path filtered_compile_commands.json → partial.scip

P5. Localized ingestion (inside a transaction)
    BEGIN TRANSACTION
    For each incremental target file F:
      - delete F's old records: occurrences WHERE file_id=F, edges WHERE file_id=F,
        symbols WHERE def_file_id=F (see §5 edge invalidation)
    Parse partial.scip → IngestBatch → write new records
    COMMIT

P6. Update unstable markers
    Write the paths of the dirty files from P3 into meta (or a dedicated unstable_files table)
    for the MCP server to consult when issuing the grep fallback signal

P7. Update the baseline
    meta.last_index_timestamp = now
    meta.last_index_commit = git HEAD
```

---

## 5. Cross-File Edge Invalidation (the Core of the Complexity)

Invalidation of the edge table `edges(src_id, dst_id, type, file_id, ...)` falls into two categories:

**① The edge's "source" is inside the changed file (`edges.file_id = F`) — easy**
Delete `edges WHERE file_id = F` and re-derive. This covers most call edges
(the file_id of a call edge is the file where the call occurs).

**② The edge points to a symbol defined inside the changed file (`dst_id` is a symbol in F) — needs care**
- If the symbol is renamed/deleted: the old `dst_id` may dangle.
- **SCIP's symbol stability saves us**: `scip_symbol` is a content-stable global identifier
  (e.g. `... ext4_file_read_iter().`). As long as the function name doesn't change, scip_symbol doesn't change,
  symbol_id doesn't change, and edges pointing to it remain valid.
- Only **renamed/deleted** symbols produce dangling edges. Handling strategy:
  - **MVP**: mark the symbol that a dangling edge points to with `is_external=1` (definition no longer exists), keep the edge but down-weight it.
    At query time, if the dst symbol has no definition, hint "symbol may have been renamed/deleted."
  - **Full**: run a lightweight GC pass after the increment — delete edges whose dst_id has neither a defining occurrence nor a SymbolInformation.

**Symbol ID reuse**:
When re-indexing file F, if a symbol in F keeps the same scip_symbol, it should **reuse the original symbol_id**
(`INSERT OR IGNORE` + update the def location) rather than being deleted and re-created.
Otherwise edges from other files pointing to these symbols would all dangle.
→ On deletion, **delete only occurrences and edges, not symbols**; update symbols' definition locations via upsert.

---

## 6. Schema / Code Changes Required

**Already present, reusable**:
- `files.sha` (currently empty) → fill with the file content hash
- `meta.index_timestamp` → rename/extend to `last_index_timestamp` + add `last_index_commit`
- File-level foreign keys `file_id` / `def_file_id` → the granularity basis for incremental deletion

**To add**:
```sql
-- Unstable file list (dirty, grep fallback)
CREATE TABLE unstable_files(
  path        TEXT PRIMARY KEY,
  reason      TEXT,        -- 'working_tree' | 'staged' | 'build_failed'
  detected_at INTEGER
);

-- files table additions
ALTER TABLE files ADD COLUMN indexed_at INTEGER;   -- last time this file was indexed
ALTER TABLE files ADD COLUMN content_sha TEXT;     -- content hash (or reuse sha)
```

**New code modules**:
```
src/sync/
├── change_detector.py   # P2-P3: mtime scan + git stability filtering + hash comparison
├── incremental.py       # P4-P5: filtered compile_commands + localized scip-clang + transactional ingestion
└── git_status.py        # git diff / status wrapper (stability determination)
```

**New SQLiteStore methods**:
```python
delete_file_records(file_path)      # delete occurrences + edges (not symbols)
upsert_symbol(...)                   # reuse the id if scip_symbol is unchanged
mark_unstable(paths, reason)         # write unstable_files
get_unstable_files()                 # for the MCP banner
get_file_sha(path) / set_file_sha    # hash comparison
```

**MCP server changes**:
Before returning a query, call `get_unstable_files()`; if the target file is hit, append a grep fallback banner.

---

## 7. Trigger Mechanism: How to "Hide Index Updates Inside the make Pipeline"

Three options, ordered by intrusiveness:

| Option | Intrusiveness | Description |
|---|---|---|
| **A. `kgraph build` wrapper** (recommended) | Low | `kgraph build -- make CC=clang LLVM=1 -j$(nproc)`: record the pre-build baseline → pass through and execute make → post-trigger P1-P7. The user only needs to replace `make` with `kgraph build -- make` |
| **B. Explicit `kgraph sync`** | Zero | After building, the user manually runs `kgraph sync`, which compares mtime + git to update incrementally. Simplest, but the user has to remember |
| **C. git hook (post-commit/post-merge)** | Medium | Triggered after commit/pull. But git events ≠ build events; the files may not have been compiled yet, which conflicts with the "compiler-aligned" philosophy |

**Recommended A + B combination**:
- `kgraph build -- <make command>` achieves "hide index updates inside the make pipeline" (the user's requirement)
- `kgraph sync` as a fallback, triggering an increment manually at any time

**Not recommended: C**: at commit time the files may not have been compiled, violating the "only index compile truth" principle.

---

## 8. Detailed Evaluation of Feasibility / Complexity / Necessity

### 8.1 Feasibility ✅ High
All dependencies are in place; no new external components are needed:
- ✅ File-level DB granularity (file_id foreign key)
- ✅ The `sha` field is already in the schema (just not populated)
- ✅ Kbuild's `.o` mtime + `.o.cmd` dependency files (build-bundled dependency tracking)
- ✅ git for stability determination
- ✅ scip-clang supports filtered compile_commands.json (indexing a TU subset)

**The one thing to verify**: when scip-clang indexes a single-TU subset, whether cross-TU symbol references are fully resolved.
Expected to be fine (by SCIP's design each TU is indexed independently + global scip_symbol merge), but needs empirical testing.

### 8.2 Complexity ⚠️ Medium
| Sub-problem | Complexity | Mitigation |
|---|---|---|
| File-change detection | Low | mtime + git status + content hash |
| Header dependency closure | **Low** (thought to be high) | **Build already computed it** — just read the set of rebuilt .o |
| Localized scip-clang | Medium | filter compile_commands.json, empirically verify subset-index correctness |
| Cross-file edge invalidation | Medium | scip_symbol stability + symbol upsert (don't delete symbols) + lightweight GC |
| Transactional atomicity | Low-Medium | wrap delete + insert in a single transaction, rollback on scip-clang failure |
| grep fallback signal | Low | unstable_files table + MCP banner |

Overall medium. The biggest complexity driver (header dependencies) is dissolved by "the build carries its own dependency tracking," which is the key.

### 8.3 Necessity
**KBench scenario: ✅ High (a hard requirement)**
- Full index: scip-clang 436s + ingestion ~540s ≈ **16 minutes/run**
- KBench has one base_commit per case, hundreds of cases → full re-index is infeasible
- Incremental: adjacent commits usually change a few to a few dozen files → seconds to minutes
- **Without lazy-indexing, KBench's token/tool-call efficiency evaluation cannot run**

**Interactive dev scenario: ⚠️ Medium**
- After `git pull` the developer changes a few dozen files and does an incremental `make`
- A 16-minute full re-index is too long; incremental keeps the graph fresh at acceptable cost
- But the developer can also accept "occasionally rebuilding fully by hand," so it's not an absolute hard requirement

---

## 9. Phased Implementation Plan

| Phase | Deliverable | Acceptance |
|---|---|---|
| **L1 hash baseline** | Populate `files.content_sha` + `meta.last_index_commit` during ingestion | After a full index every file has a hash |
| **L2 change detection** | `change_detector.py`: mtime scan + git filtering + hash comparison | Changing one file correctly identifies the incremental target set |
| **L3 localized index** | filtered compile_commands + localized scip-clang → partial.scip | A single-TU subset index produces a correct partial index |
| **L4 transactional ingestion** | `delete_file_records` + `upsert_symbol` + transaction | After an incremental update the query results match a full index |
| **L5 grep fallback** | `unstable_files` table + MCP banner | A dirty-file query returns a fallback signal |
| **L6 build wrapper** | `kgraph build -- <make>` + `kgraph sync` | After a build the graph updates incrementally and automatically |

L1-L2 are low-cost and should be done first (paving the way for KBench); L3-L4 are the core; L5-L6 polish the experience.

---

## 10. Open Questions / Risks

1. **Correctness of scip-clang subset indexing** — when indexing a single TU, can it produce a stable, consistent scip_symbol for symbols that the TU references but that are defined in other not-yet-re-indexed files? Needs empirical testing in the L3 phase.
   If inconsistent, cross-file edges will break. **This is the biggest risk.**

2. **Dangling edges from symbol renames** — the MVP uses is_external marking + lightweight GC, but the "rename" semantics
   (old symbol deleted + new symbol added; callers' edges should be reconnected to the new symbol) are not given directly by SCIP,
   and rely on the caller files also being re-indexed to heal naturally. This holds in most cases (renames usually touch the callers too),
   but cross-file renames (change the definition without changing the call, e.g. via a macro indirection) may be missed.

3. **Ambiguity of "partial builds" in the staging area** — a developer with a dirty worktree runs `make`, and the dirty files do get
   compiled. We still judge them unstable by git status and skip them. This matches the user's requirement, but it means files that are
   "built but not committed" do not enter the graph — this semantics needs to be made explicit in the documentation.

4. **Multi-config scenarios** — lazy-indexing assumes a single fixed config (defconfig).
   Switching config changes the set of compiled files, effectively swapping in a different graph, and should trigger a full rebuild rather than an increment.
   We need to detect config changes (e.g. the `.config` hash) and force a full index.

---

## Appendix: Consistency with KGraph's Philosophy

lazy-indexing is not "adding a feature" to KGraph; it is **carrying KGraph's core philosophy through to index updates**:

| KGraph principle | How lazy-indexing embodies it |
|---|---|
| Index only the truth the compiler sees | Update the index only after a successful build; uncompiled drafts never enter the graph |
| Config-aware | Incremental target = the TUs actually rebuilt under the current config; switching config triggers a full index |
| Function-pointer reachability (ops_bind) | Re-derive ops_bind edges incrementally, from the same source logic as the full index |
| Honest about unstable state | dirty files are explicitly marked unstable + grep fallback; never pretend the index is up to date |

The build is the source of truth for KGraph, so **the build should also be the moment KGraph updates its index** — that is the entire argument for lazy-indexing.
