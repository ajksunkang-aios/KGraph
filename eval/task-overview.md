# KBench Retrieval Tasks — Overview

> The 8 retrieval tasks KBench ships (v1), what each tests, which KGraph tool it
> targets, and how the two arms actually solved each in the first run.

KBench pins all retrieval tasks to a single kernel commit (`v7.1-rc7`,
`4549871…`) — the agent explores the tree *at that commit*, and the KGraph arm
queries a `kgraph.db` built *from that same commit*. See
[`KBench/tasks/retrieval/manifest.json`](https://github.com/ajksunkang-aios/KBench).

## Two design axes

Every task carries a **subtype** (which retrieval ability it exercises) and a
**tier** (whether grep can solve it):

- **direct tier** — grep can reach the answer by pattern-matching. The A/B gap
  here is expected to be **cost** (tokens / calls / time), not accuracy.
- **compiler-aware tier** — the answer lives behind a construct grep can't see
  (function-pointer ops-table binding, macro-expanded symbol). grep has a
  *structural blind spot*; the A/B gap here is expected to be **accuracy**.

This split is the whole point of the report's per-tier breakdown (DESIGN §7):
it stops you from conflating "grep could've gotten that for free" wins with
"grep literally cannot see this" wins.

## The 8 tasks

| # | task id | subtype | tier | prompt (abbreviated) | ground truth |
|---|---|---|---|---|---|
| 1 | `callers/ext4_file_read_iter` | callers | **compiler-aware** | Through which ops-table variable is `ext4_file_read_iter` bound? (indirect dispatch — grepping the name won't find the binding) | `ext4_file_operations @ fs/ext4/file.c` |
| 2 | `callers/vfs_read` | callers | direct | Which functions directly call `vfs_read`? | `ksys_read`, `ksys_pread64` |
| 3 | `code_snippet/ext4_read_iter_assign` | code_snippet | direct | In ext4's `file_operations` table, which function is assigned to `.read_iter`? | `ext4_file_read_iter @ fs/ext4/file.c` |
| 4 | `function_body/ext4_file_read_iter` | function_body | direct | In which source file is `ext4_file_read_iter` defined? | `fs/ext4/file.c` |
| 5 | `ops_impls/read_iter` | ops_impls | **compiler-aware** | Which function implements `read_iter` for ext4 (bound to `ext4_file_operations.read_iter`)? | `ext4_file_read_iter @ fs/ext4/file.c` |
| 6 | `struct_def/file_operations` | struct_def | direct | In which header is `struct file_operations` defined? | `include/linux/fs.h` |
| 7 | `symbol_def/generic_file_read_iter` | symbol_def | direct | In which source file is `generic_file_read_iter` defined? | `mm/filemap.c` |
| 8 | `symbol_def/vfs_read` | symbol_def | direct | In which source file is `vfs_read` defined? | `fs/read_write.c` |

All use the `set_recall_precision` scorer: the agent emits a fenced ```kbench
block of `name|file` lines, scored as set recall/precision/F1 against the GT
(see `KBench/kbench/scorers/set_recall.py`).

## Subtype → targeted KGraph tool

The B arm is **additive**: `A-baseline (grep/glob/read)` + all 13 of KGraph's
MCP tools. The agent is free to pick any; the table shows the tool each subtype
is *designed* to exercise and why KGraph has an edge there.

| subtype | primary KGraph tool | KGraph's edge over grep |
|---|---|---|
| `callers` (direct) | `find_callers` | compiler-resolved call set — no `ksmbd_vfs_read`-style name-collision false positives |
| `callers` (compiler-aware) | `find_ops_impls` / `find_callers` | **ops-table indirect dispatch** — the `.read_iter = …` binding is invisible to `grep ext4_file_read_iter` |
| `ops_impls` | `find_ops_impls` | **core moat**: function-pointer-table field → implementation resolution |
| `code_snippet` | `get_symbol` / `search_symbols` | locates the exact assignment in the ops table |
| `function_body` | `get_symbol` | definition file from the symbol index |
| `struct_def` | `get_symbol` / `find_type_definition` | struct type definition location |
| `symbol_def` | `get_symbol` | function definition file |

> The agent decides its own tool mix; in practice the B arm leaned on
> `get_symbol` (definition lookup) most, with `find_ops_impls` for the
> compiler-aware tasks. Only 4 of the 13 KGraph tools were exercised in v1 —
> see "Coverage gap" below.

## First run: how each arm actually solved each task (rep=1)

`tok` = in+out tokens, `calls` = total tool invocations. ⭐ marks the tasks
that target KGraph's compiler-aware moat; ⭐⭐ the grep-blind-spot case.

> **⚠ The rep=1 numbers below are a single point each and should NOT be cited.**
> The authoritative result is the rep=3 median in the next section, which
> corrected several rep=1 impressions (notably: the accuracy gap vanished at
> the median — grep's losses on `struct_def`/`code_snippet` were single-run
> luck, not systematic). rep=1 is kept here only as the per-task
> "how did the arms actually solve it" trace.

| task | A-baseline | B-kgraph | story |
|---|---|---|---|
| `callers/ext4_file_read_iter` ⭐ | glob×2 grep×7 read×3 → F1 1.0, 13560 tok, 149s | `find_ops_impls`+`get_symbol`+read → F1 1.0, 4182 tok, 8s | grep finally found it by brute force; **cost gap** (18× faster, 70% fewer tokens) |
| `callers/vfs_read` | grep×3 read×2 → **F1 0.8** (false-positive `read_code`) | `find_callers`×1 → F1 1.0, 3239 tok, 6s | grep **over-reported**; KGraph 1 call, exact |
| `code_snippet/ext4_read_iter_assign` | glob×1 grep×5 read×3 → F1 1.0, 8000 tok, 80s | `get_symbol`+grep×2+read+`search_symbols` → F1 1.0, 10192 tok, 10s | both right; B tried extra tools (more tokens) but 8× faster |
| `function_body/ext4_file_read_iter` | grep×2 → F1 1.0, 1677 tok, 77s | `get_symbol`×1 → F1 1.0, 6206 tok, 4s | both right; B trades a few more tokens for **19× speed** |
| `ops_impls/read_iter` ⭐ | grep×2 read×1 → F1 1.0, 2994 tok, 29s | `find_ops_impls`+`get_symbol` → F1 1.0, 9525 tok, 6s | grep lucked into the answer; **cost gap**, not accuracy |
| `struct_def/file_operations` ⭐⭐ | grep×1 → **F1 0.0** (found refs, not the def) | `get_symbol`×1 → F1 1.0, 304 tok, 3s | **crushing win**: grep scored 0, KGraph 1 call for full marks |
| `symbol_def/generic_file_read_iter` | grep×1 → F1 1.0, 815 tok, 13s | `get_symbol`×1 → F1 1.0, 314 tok, 4s | both right; B 3× faster, 60% fewer tokens |
| `symbol_def/vfs_read` | grep×1 → F1 1.0, 1425 tok, 15s | `get_symbol`×1 → F1 1.0, 3253 tok, 4s | both right; B 4× faster |

### Tool call frequency (rep=1)

| tool | A-baseline | B-kgraph | note |
|---|---|---|---|
| `grep` | 19 | 4 | baseline workhorse; B only for confirmation |
| `read` | 9 | 3 | source-body confirmation |
| `glob` | 3 | 0 | file finding |
| `get_symbol` | — | **7** | B's most-used tool (definition lookup) |
| `find_ops_impls` | — | 2 | the moat tool — ops binding (compiler-aware tasks) |
| `find_callers` | — | 1 | direct-call set |
| `search_symbols` | — | 1 | fuzzy symbol search |

## Authoritative result: rep=3 median (GLM-5.2, 8 tasks × 2 arms × 3 reps)

`--reps 3`, median over the 3 replicates per (task, arm). This satisfies KBench
DESIGN §12 (N≥3, median) and **supersedes the rep=1 table above**. Report:
`eval/reports/20260714T110036Z.md`.

### Headline (median over all 8 tasks)

| arm | F1 | tokens | tool-calls | wall (s) |
|---|---:|---:|---:|---:|
| A-baseline | 1.00 | 2027 | 2 | 33.2 |
| **B-kgraph** | **1.00** | 4496 | **1** | **5.5** |

### By tier (median)

| arm | tier | n | F1 | tokens | calls | wall (s) |
|---|---|---:|---:|---:|---:|---:|
| A-baseline | compiler-aware | 6 | 1.00 | 4424 | 4 | 88.6 |
| B-kgraph | compiler-aware | 6 | 1.00 | 4496 | 2 | 10.2 |
| A-baseline | direct | 18 | 1.00 | 1490 | 1 | 17.2 |
| B-kgraph | direct | 18 | 1.00 | 4554 | 1 | 5.3 |

### What changed vs rep=1 (why rep=3 was necessary)

| claim from rep=1 | rep=3 verdict |
|---|---|
| "B is +0.15 more accurate (A=0.85)" | ❌ **False at the median.** A's 0.85 was single-run variance: on `struct_def` A scored [1.0, 1.0, **0.0**] and on `code_snippet` [1.0, **0.0**, 1.0] — the 0s dragged rep=1's mean. At the median both arms are 1.00 on every tier. |
| "grep blind-spot on `struct_def` (A=0.0)" | ⚠ **Inflated.** grep *can* miss `struct file_operations`'s definition, but only ~1/3 of the time here — not the systematic failure rep=1 implied. |
| "B is much faster / fewer calls" | ✅ **Holds.** B-kgraph median: 1 call / 5.5s vs A's 2 calls / 33.2s — **~6× faster, half the calls.** This is the robust win. |
| "B saves tokens" | ❌ **False.** B's `get_symbol` returns richer payloads than grep's terse hits, so B's median tokens (4496) **exceed** A's (2027). The cost win is in **time and call count, not tokens.** |

### Honest read of the v1 result

On these 8 tasks, **the accuracy story did not survive the median** — the v1
set is too small and too grep-solvable for KGraph's accuracy advantage to
show. What *does* hold is a **cost/speed story**: KGraph gets the same
accuracy in ~1/6 the wall time and half the tool calls. The accuracy moat
(ops-bind / macro / indirect dispatch) needs harder, grep-blind tasks to
materialize — see the two gaps below.

## Coverage gap (what v1 doesn't exercise)

Of KGraph's 13 MCP tools, only **4** were exercised by the 8 v1 tasks:
`get_symbol`, `find_ops_impls`, `find_callers`, `search_symbols`. Untouched:

- `get_callchain` / `call_path` — multi-hop call paths (no v1 task asks "find
  the path from A to B")
- `find_callees` — reverse direction of `find_callers`
- `get_struct_layout` — struct field offsets/sizes
- `find_references` — all reference sites
- `find_type_definition` — explicit type-def lookup (v1 used `get_symbol`)
- `get_neighborhood` — fan-in/fan-out around a symbol
- `index_status` — meta tool

To prove the moat more completely, KBench should grow task subtypes for
`call_path`, `struct_layout`, and `callchain` — this is the Phase-3 direction
(contribute KGraph-specific tasks back to KBench; see eval/DESIGN.md §9).

## Known v1 weakness: compiler-aware tier too thin

Only 2 of 8 tasks are compiler-aware, and both happen to be grep-solvable by
brute force in this dataset — so the accuracy gap that *should* show up in
this tier didn't (grep scored 1.0 on both). The real grep blind-spot (macro
binding via `FOPS_READ(...)`, which scip-clang expands but grep can't) is only
exercised by KBench's
[`prototypes/synthetic_ops_bind`](https://github.com/ajksunkang-aios/KBench/tree/main/prototypes/synthetic_ops_bind)
probe, not the v1 real-kernel task set. Folding that probe's tasks in is the
clearest way to make the accuracy-gap story show up in the report.
