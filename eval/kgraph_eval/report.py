"""Report rendering — KGraph's view on top of KBench's results.

KBench's own render.render() already produces the arm×tier×per-task F1+cost
table per run. We add two KGraph-facing views on top:

  1. Gain table — per tier, Δ(B-kgraph − A-baseline) for F1/tokens/calls.
     This is the headline number: does KGraph make agents more accurate AND
     cheaper? Reported per tier because the story differs (direct = cost gap,
     compiler-aware = accuracy gap).
  2. Model matrix — model(rows) × arm(cols), F1 and tokens per cell. Rendered
     only when ≥2 models ran (proves the gain generalizes across models, not
     overfit to one).

Aggregation honors KBench DESIGN §12: median over reps (not mean), per tier.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

ARMS = ("A-baseline", "B-kgraph")  # KBench's canonical arm names


def _load_run(run_dir: Path) -> list[dict]:
    """Load all result records from one runner.run() output dir."""
    d = run_dir / "retrieval"
    if not d.is_dir():
        return []
    rows = []
    for f in sorted(d.glob("*.json")):
        try:
            rows.append(json.loads(f.read_text()))
        except Exception:
            pass
    return rows


def _median(records: list[dict], key) -> float:
    vals = []
    for r in records:
        if "accuracy" not in r or "cost" not in r:
            continue
        if key == "f1":
            vals.append(r["accuracy"]["f1"])
        elif key == "tokens":
            vals.append(r["cost"]["tokens_in"] + r["cost"]["tokens_out"])
        elif key == "calls":
            vals.append(sum(r["cost"]["tool_calls"].values()))
        elif key == "wall":
            vals.append(r["cost"]["wall_seconds"])
    if not vals:
        return 0.0
    return statistics.median(vals)


def _by_tier(records: list[dict]) -> dict[str, list[dict]]:
    out = defaultdict(list)
    for r in records:
        if "accuracy" in r:
            out[r.get("tier") or "untiered"].append(r)
    return dict(out)


def _gather(all_runs: dict[str, list[dict]]) -> dict:
    """all_runs: {model: records_from_its_run}. → structured view."""
    view = {}
    for model, records in all_runs.items():
        by_arm = defaultdict(list)
        for r in records:
            if "error" in r:
                continue
            by_arm[r["arm"]].append(r)
        tiers = defaultdict(lambda: defaultdict(list))
        for arm, recs in by_arm.items():
            for tier, trecs in _by_tier(recs).items():
                tiers[tier][arm] = trecs
        view[model] = {"by_arm": dict(by_arm), "tiers": dict(tiers)}
    return view


def _gain(b: float, a: float) -> tuple[float, str]:
    """Δ = b − a for accuracy; for cost, percent saved. Returns (delta, fmt)."""
    if a == 0:
        return b, "—" if b == 0 else f"+{b:g}"
    pct = (b - a) / a * 100.0
    return b - a, f"{pct:+.0f}%"


def render_gain_table(view: dict) -> str:
    """Per-tier Δ(B−A) for the first model (the headline A/B)."""
    model = next(iter(view))
    m = view[model]
    lines = [
        f"### Gain (B-kgraph vs A-baseline) — model `{model}`",
        "",
        "> ΔF1 = B−A (positive = more accurate). "
        "Δtokens/Δcalls = % saved by B (negative % = B cheaper). "
        "direct tier → expect cost gap; compiler-aware → expect accuracy gap.",
        "",
        "| tier | n | ΔF1 | Δtokens | Δcalls |",
        "|---|---:|---:|---:|---:|",
    ]
    for tier in sorted(m["tiers"]):
        arm_recs = m["tiers"][tier]
        a = arm_recs.get(ARMS[0], [])
        b = arm_recs.get(ARMS[1], [])
        if not a or not b:
            continue
        n = max(len(a), len(b))
        af, at, ac = _median(a, "f1"), _median(a, "tokens"), _median(a, "calls")
        bf, bt, bc = _median(b, "f1"), _median(b, "tokens"), _median(b, "calls")
        _, tok_fmt = _gain(bt, at)
        _, call_fmt = _gain(bc, ac)
        lines.append(f"| {tier} | {n} | {bf-af:+.2f} | {tok_fmt} | {call_fmt} |")
    return "\n".join(lines) + "\n"


def render_model_matrix(view: dict) -> str | None:
    """model×arm cross-table of F1 and tokens. Only if ≥2 models."""
    models = list(view)
    if len(models) < 2:
        return None
    lines = [
        "## Model matrix",
        "",
        "> Proves the KGraph gain generalizes across models (F1 / median tokens per cell).",
        "",
        "### F1 (median)",
        "",
        "| model | " + " | ".join(ARMS) + " |",
        "|---|" + "|".join("---:" for _ in ARMS) + "|",
    ]
    for model in models:
        m = view[model]
        cells = []
        for arm in ARMS:
            recs = m["by_arm"].get(arm, [])
            cells.append(f"{_median(recs, 'f1'):.2f}" if recs else "—")
        lines.append(f"| {model} | " + " | ".join(cells) + " |")
    lines += ["", "### Median tokens (in+out)", "",
              "| model | " + " | ".join(ARMS) + " |",
              "|---|" + "|".join("---:" for _ in ARMS) + "|"]
    for model in models:
        m = view[model]
        cells = []
        for arm in ARMS:
            recs = m["by_arm"].get(arm, [])
            cells.append(f"{int(_median(recs, 'tokens'))}" if recs else "—")
        lines.append(f"| {model} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def render(eval_run_id: str, all_runs: dict[str, Path],
           meta: dict, reports_dir: Path) -> tuple[Path, Path]:
    """all_runs: {model: run_dir_path}. Returns (md_path, json_path)."""
    records_per_model = {m: _load_run(p) for m, p in all_runs.items()}
    view = _gather(records_per_model)

    parts = [f"# KGraph Eval Report — {eval_run_id}", ""]
    parts.append("## Reproducibility")
    parts.append("")
    parts.append("| key | value |")
    parts.append("|---|---|")
    for k, v in meta.items():
        parts.append(f"| {k} | `{v}` |")
    parts.append("")

    # Per-model KBench report references (KBench already rendered each).
    parts.append("## Per-run KBench reports")
    parts.append("")
    for model, p in all_runs.items():
        rid = p.name
        parts.append(f"- `{model}` → run `{rid}`: `reports/{rid}.md`")
    parts.append("")

    parts.append(render_gain_table(view))

    matrix = render_model_matrix(view)
    if matrix:
        parts.append(matrix)

    # Structured summary for dashboards.
    summary = {
        "eval_run_id": eval_run_id,
        "meta": meta,
        "models": {},
    }
    for model in view:
        m = view[model]
        t_summary = {}
        for tier, arm_recs in m["tiers"].items():
            row = {}
            for arm in ARMS:
                recs = arm_recs.get(arm, [])
                if recs:
                    row[arm] = {
                        "f1": _median(recs, "f1"),
                        "tokens": int(_median(recs, "tokens")),
                        "calls": _median(recs, "calls"),
                        "wall": _median(recs, "wall"),
                        "n": len(recs),
                    }
            t_summary[tier] = row
        summary["models"][model] = t_summary

    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"{eval_run_id}.md"
    json_path = reports_dir / f"{eval_run_id}.json"
    md_path.write_text("\n".join(parts))
    json_path.write_text(json.dumps(summary, indent=2))
    return md_path, json_path
