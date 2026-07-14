# KGraph Eval

> One command. Build nothing. Run the KBench retrieval A/B on an existing
> `kgraph.db`. Render a report.

This is the eval harness on the **system-under-test** side. It does **not**
reimplement KBench's task set, scorer, or agent loop — those live in
[KBench](https://github.com/ajksunkang-aios/KBench). KGraph/eval only:

1. Consumes an existing `kgraph.db` (you build it with `kgraph init`).
2. Pulls KBench at a pinned commit.
3. Runs KBench's `runner.run(A-baseline, B-kgraph)` (the agent loop, arms,
   and `set_recall_precision` scorer are all KBench's).
4. Renders a KGraph-facing report: a **gain table** (ΔF1 / Δtokens / Δcalls,
   by tier) and, with ≥2 models, a **model matrix**.

See [`DESIGN.md`](DESIGN.md) for the full rationale and the KBench contract.

---

## Prerequisites (your job, not eval's)

1. **A built `kgraph.db`** for the kernel tree. From the kernel root:
   ```bash
   cd /path/to/linux
   kgraph init .          # scip-clang → SQLite → .kgraph/kgraph.db (Linux x86-64 only)
   ```
2. **The tree at KBench's retrieval pin** (`v7.1-rc7`, commit `4549871…`), so the
   db was built from the same tree the tasks target. eval pin-checks this; pass
   `--relax-pin` to skip for a local probe (the report will flag non-pinned).
3. **LLM env**: an Anthropic-compatible endpoint (GLM/Claude/OpenRouter/…):
   ```bash
   export ANTHROPIC_BASE_URL=https://...
   export ANTHROPIC_AUTH_TOKEN=...      # or ANTHROPIC_API_KEY
   export ANTHROPIC_MODEL=glm-5         # default model
   ```

## Install

```bash
pip install -e ./eval        # from the KGraph repo root
```

## Run

```bash
python -m kgraph_eval --kernel /path/to/linux
# → db auto-discovered at /path/to/linux/.kgraph/kgraph.db
# → report: eval/reports/<run_id>.md (+ .json)
```

### Options (all have defaults — zero-config for the common case)

| flag | default | purpose |
|---|---|---|
| `--kernel` | *(required)* | kernel source root; db defaults to `<kernel>/.kgraph/kgraph.db` |
| `--db` | `<kernel>/.kgraph/kgraph.db` | override the db path |
| `--models` | `$ANTHROPIC_MODEL` or `glm-5` | comma-separated; **≥2 → also renders the model matrix** |
| `--arms` | `A-baseline,B-kgraph` | comma-separated arm set |
| `--reps` | `3` | replicates per task×arm (median reported, per KBench §12) |
| `--relax-pin` | off | skip the kernel-commit vs KBench-pin check |
| `--max-turns` | `12` | agent loop turn cap |

## Reading the report

The headline is the **gain table**, per tier:

| tier | n | ΔF1 | Δtokens | Δcalls |
|---|---:|---:|---:|---:|
| compiler-aware | … | +0.70 | -50% | -88% |
| direct | … | +0.00 | -50% | -80% |

- **ΔF1** = B−A (positive = KGraph made the agent more accurate).
- **Δtokens / Δcalls** = % saved by the B arm (negative = cheaper).
- **direct** tier (grep can solve): expect ΔF1≈0, the gap is **cost**.
- **compiler-aware** tier (ops-bind / indirect / macro): expect a real ΔF1 —
  the grep-blind-spot where KGraph wins on accuracy.

With ≥2 models, a **model matrix** (model×arm, F1 and median tokens per cell)
proves the gain generalizes across models rather than overfitting one.

Each model also gets KBench's own per-run report (`eval/reports/<run_id>.md`)
with the full arm×tier×per-task breakdown.

## CI

[`.github/workflows/eval-retrieval.yml`](../.github/workflows/eval-retrieval.yml)
runs the whole chain on push of a tag / manual dispatch: it owns the db build
(clone Linux @ the KBench pin → defconfig → scip-clang → `kgraph init`), then
calls `kgraph_eval` to consume it. Results + report are uploaded as an artifact.

> The workflow — not `kgraph_eval` — builds the db. That's the point: real
> users build their own db with `kgraph init`; CI does the same. eval only
> consumes. (See DESIGN §7, "build_sut 意义不大".)

## Reproducibility & rigor

Every report records `kgraph_commit`, `kbench_pin`, `kernel_pin`, `kernel_head`,
models, arms, and reps. Aggregation is **median over N≥3 reps** (KBench §12),
the system prompt is tool-neutral (no B-arm favoritism), and the ground truth
comes from KBench (compiler-resolved, independently verified) — KGraph/eval
never produces GT, so there's no circularity.

## Layout

```
eval/
  DESIGN.md            full design + KBench contract
  README.md            this file
  pyproject.toml       standalone subproject (deps: anthropic + KBench runtime)
  .env.example         LLM env template
  kgraph_eval/
    cli.py             one command: discover db → pin-check → run → report
    kbench.py          clone KBench @ pin + preflight + call runner.run
    report.py          gain table + model matrix on top of KBench's results
  results/             run outputs (.gitignore) — KBench runner.run writes here
  reports/             final reports (.gitignore)
  datasets/            KBench clone cache (.gitignore)
```
