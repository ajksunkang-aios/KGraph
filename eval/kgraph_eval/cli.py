"""KGraph eval CLI — one command: build nothing, run the A/B, render a report.

Usage:
    python -m kgraph_eval --kernel /path/to/linux

Prerequisites (the user's job, not eval's):
  - The kernel tree at --kernel has a built kgraph.db at
    <kernel>/.kgraph/kgraph.db (run `kgraph init .` there).
  - The tree is checked out at the commit KBench pins for retrieval (v7.1-rc7),
    so the db was built from the same tree the tasks target. --relax-pin skips
    this check for local probes (report will flag non-pinned).
  - LLM env is set: ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN (or API_KEY).

What this command does (eval/DESIGN.md §4):
  1. Discover the db (default <kernel>/.kgraph/kgraph.db; --db overrides).
  2. Pin-check the kernel tree against KBench's retrieval manifest.
  3. Clone KBench at the pinned commit (cached under eval/datasets/).
  4. For each model: run KBench's runner.run(A-baseline, B-kgraph, reps).
  5. Render KBench's per-run report + KGraph's gain table + model matrix.
  6. Print a summary table to stdout.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_EVAL_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from kgraph_eval import kbench, report  # noqa: E402

RESULTS_DIR = _EVAL_ROOT / "results"
REPORTS_DIR = _EVAL_ROOT / "reports"
DATASETS_DIR = _EVAL_ROOT / "datasets"


def _discover_db(kernel: Path, db_override: Path | None) -> Path:
    if db_override:
        if not db_override.exists():
            sys.exit(f"ERROR: --db not found: {db_override}")
        return db_override
    db = kernel / ".kgraph" / "kgraph.db"
    if not db.exists():
        sys.exit(
            f"ERROR: kgraph.db not found at {db}.\n"
            f"  Build it first:  cd {kernel} && kgraph init ."
        )
    return db


def _resolve_kgraph_repo() -> Path:
    # The KGraph repo this eval ships with: eval's parent dir.
    return _EVAL_ROOT.parent


def _kgraph_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(_EVAL_ROOT.parent), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="kgraph_eval",
        description="Run the KBench retrieval A/B on an existing kgraph.db and render a report.",
    )
    ap.add_argument("--kernel", required=True, type=Path,
                    help="kernel source root (must contain .kgraph/kgraph.db)")
    ap.add_argument("--db", default=None, type=Path,
                    help="kgraph.db path (default: <kernel>/.kgraph/kgraph.db)")
    ap.add_argument("--models", default=os.environ.get("ANTHROPIC_MODEL", "glm-5"),
                    help="comma-separated model ids (≥2 → also renders a model matrix)")
    ap.add_argument("--arms", default="A-baseline,B-kgraph",
                    help="comma-separated arms (default both)")
    ap.add_argument("--reps", type=int, default=3,
                    help="replicates per task×arm (median reported)")
    ap.add_argument("--relax-pin", action="store_true",
                    help="skip kernel-commit vs KBench-pin check (local probes)")
    ap.add_argument("--max-turns", type=int, default=12)
    args = ap.parse_args(argv)

    kernel = args.kernel.resolve()
    if not kernel.is_dir():
        sys.exit(f"ERROR: kernel tree not found: {kernel}")
    db_path = _discover_db(kernel, args.db)
    kgraph_repo = _resolve_kgraph_repo()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if "B-kgraph" in arms and not kgraph_repo.exists():
        sys.exit(f"ERROR: KGraph repo not found at {kgraph_repo}")

    print(f"KGraph eval\n  kernel:  {kernel}\n  db:      {db_path}\n"
          f"  models: {models}\n  arms:   {arms}\n  reps:   {args.reps}")

    # 1. Clone KBench at pin.
    print("\n[1/3] ensuring KBench at pinned commit …")
    kbench_root = kbench.ensure_kbench(DATASETS_DIR, kgraph_repo)

    # 2. Pin-check the kernel tree.
    pin_commit = kbench.load_retrieval_pin(kbench_root)
    print(f"  KBench retrieval pin: {pin_commit[:12]} (v7.1-rc7)")
    kbench.check_pin(kernel, pin_commit, relax=args.relax_pin)

    # 3. Run each model's A/B.
    print(f"\n[2/3] running {len(models)} model(s) × {len(arms)} arm(s) × {args.reps} reps …")
    run_dirs: dict[str, Path] = {}
    for model in models:
        print(f"\n  ── model {model} ──")
        run_id, run_dir = kbench.run_one(
            kbench_root, model, arms, args.reps,
            kernel, kgraph_repo, db_path, RESULTS_DIR, args.max_turns,
        )
        run_dirs[model] = run_dir
        print(f"    run_id={run_id}  results={run_dir}")

    # 4. Render KGraph's report (gain table + model matrix).
    print("\n[3/3] rendering report …")
    # Stamp the eval run id the same way KBench stamps inner run_ids (UTC second),
    # so the outer report id is consistent in format with the per-run ones.
    from kbench.harness.runner import _now_iso as _kb_now
    eval_run_id = _kb_now()
    meta = {
        "kgraph_commit": _kgraph_commit(),
        "kbench_pin": kbench.KBENCH_PIN,
        "kernel_pin": pin_commit,
        "kernel_head": subprocess.run(
            ["git", "-C", str(kernel), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()[:12],
        "models": ",".join(models),
        "arms": ",".join(arms),
        "reps": args.reps,
        "relax_pin": str(args.relax_pin),
    }
    md, js = report.render(eval_run_id, run_dirs, meta, REPORTS_DIR)
    print(f"\n✅ eval {eval_run_id}")
    print(f"   report: {md}")
    print(f"   data:   {js}")

    # 5. Print a stdout summary (first model's gain, per tier).
    _print_summary(md)
    return 0


def _print_summary(md_path: Path) -> None:
    """Echo the gain table section of the rendered report to stdout."""
    text = md_path.read_text()
    start = text.find("### Gain")
    if start == -1:
        return
    end = text.find("\n##", start)
    if end == -1:
        end = len(text)
    print("\n" + text[start:end].strip())


if __name__ == "__main__":
    sys.exit(main())
