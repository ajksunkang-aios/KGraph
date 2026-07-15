# Eval Snapshot — 20260715T072828Z (confidence fix verified)

KBench retrieval A/B, rep=3 median, GLM-5.2, after the ops_bind confidence fix.

## What this snapshot records

The first run where the **ops_bind confidence fix** (0.5 → 1.0) was verified end-to-end.
Previously `find_ops_impls` returned `confidence=0.5` for a compiler-established binding,
which made the agent distrust it and fall back to `grep`+`read` source verification —
defeating the "fewer calls" advantage. With confidence=1.0, the B arm stops falling back.

| | A-baseline | B-kgraph |
|---|---|---|
| F1 (median) | 1.0 | 1.0 |
| tokens (median) | 2375 | 3485 |
| tool-calls (median) | 2.0 | 1.0 |
| wall (median, s) | 42.7 | 6.1 |

**Before vs after the fix (both rep=3 median):** B tokens 4496 → 3485 (**-22%**),
B calls held at 1.0, B **no longer falls back to grep**.

## Files

```
reports/
  20260715T072828Z.html      A/B report: KPI cards + tier breakdown + per-task F1/wall + rep jitter
  toolcall-trace.html        per-task ordered tool-call sequence + tool-composition chart
  20260715T072828Z.md        KBench native report (arm × tier × per-task)
  20260715T072828Z.json      same, structured
results/20260715T072828Z/retrieval/*.json   48 raw cells (8 tasks × 2 arms × 3 reps), each with tool_trace
```

## Reproducibility

- kernel: torvalds/linux @ `4549871118cf` (v7.1-rc7), KBench retrieval pin
- KGraph: this commit (scip_parser.py confidence=1.0) + kgraph.db rebuilt clean
- KBench pin: `f89579a` (includes the tool_trace harness commit)
- model: GLM-5.2 via LiteLLM proxy; `KGRAPH_EVAL_CURL_TRANSPORT=1` (gateway 502s httpx, accepts curl)

## Metric definitions (for readers of the reports)

- **F1** — accuracy: agent's final answer vs ground truth (set recall/precision/F1).
- **wall time** — end-to-end wall-clock seconds for one episode (all LLM round-trips + tool exec).
- **tool calls** — count of tool invocations in the episode (NOT token usage).
- **tokens** — LLM input+output token usage (the "cost in money").
