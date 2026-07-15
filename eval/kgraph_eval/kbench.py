"""KBench adapter — clone KBench at a pinned commit, run the A/B, return run paths.

This is the only place KGraph/eval touches KBench internals. It:
  1. Ensures KBench is checked out at a pinned commit (clone --depth 1, cached
     under eval/datasets/). Pinned, not submodule'd — per KBench DESIGN §10.
  2. Loads KBench's retrieval tasks + the manifest's pinned kernel commit.
  3. Preflight: the kernel tree's HEAD must match the manifest commit, so the
     kgraph.db was built from the *same* tree the tasks target (non-circular
     GT guarantee, on KGraph's side). --relax-pin skips this for local probes.
  4. Calls kbench.harness.runner.run(..., results_dir=<our dir>) directly —
     bypassing KBench's cli.py, which hard-codes results_dir inside the KBench
     repo. This keeps all artifacts in eval/results/.
  5. Renders KBench's own markdown+json report per run.

Note on run_id collisions: runner.run() stamps run_id as a UTC second
(`%Y%m%dT%H%M%SZ`). Two models invoked within the same second would collide
and overwrite each other's results dir. To avoid that, each model writes into
its own results subdir (results/<model-run>/), so the inner run_id is scoped
per model and never collides across models.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# KBench repo: pinned for reproducibility. Update deliberately; recorded in
# every report so a run is reproducible. `git clone --depth 1 <commit>` does
# not work for arbitrary SHAs on GitHub, so we do a shallow clone of the
# default branch then `checkout` the pin (still cheap).
KBENCH_URL = "https://github.com/ajksunkang-aios/KBench.git"
KBENCH_PIN = "f89579adcb983a4554649cd4b5ed7974683aad14"  # feat(harness): tool_trace (HEAD after tool-trace commit)

RETRIEVAL_MANIFEST = "tasks/retrieval/manifest.json"


class PinMismatch(Exception):
    """Kernel HEAD != KBench's pinned retrieval commit (non-circular risk)."""


def _is_kbench_at_pin(path: Path) -> bool:
    """True if `path` is a git repo whose HEAD == KBENCH_PIN (or contains it)."""
    if not path.is_dir() or not (path / ".git").exists():
        return False
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    return head == KBENCH_PIN


def ensure_kbench(datasets_dir: Path, kgraph_repo: Path | None = None) -> Path:
    """Get KBench at KBENCH_PIN. Prefers local copies; clones as last resort.

    Discovery order (first one whose HEAD == KBENCH_PIN wins):
      1. $KBENCH_REPO env (explicit)
      2. <kgraph_repo>/KBench  (sibling symlink in a dev checkout)
      3. <datasets_dir>/KBench (a prior cached clone)

    Otherwise clone into datasets/ and checkout the pin. Note GitHub does NOT
    support `fetch --depth 1 <arbitrary-sha>`, so the clone fetches the full
    default branch then checks out the pin commit (still works; just not shallow).
    """
    datasets_dir.mkdir(parents=True, exist_ok=True)

    # 1-2. Try local copies first (avoids network + the shallow-sha problem).
    candidates: list[Path] = []
    env_kb = os.environ.get("KBENCH_REPO")
    if env_kb:
        candidates.append(Path(env_kb))
    if kgraph_repo:
        candidates.append(kgraph_repo / "KBench")
    for c in candidates:
        if _is_kbench_at_pin(c):
            print(f"  using local KBench at pin {KBENCH_PIN[:12]}: {c}")
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            return c

    # 3. Cached clone under datasets/.
    kbench = datasets_dir / "KBench"
    if _is_kbench_at_pin(kbench):
        if str(kbench) not in sys.path:
            sys.path.insert(0, str(kbench))
        return kbench

    # 4. Clone fresh (full branch fetch — GitHub won't shallow-fetch a raw SHA).
    if kbench.is_dir():
        import shutil
        shutil.rmtree(kbench)
    print(f"  cloning KBench ({KBENCH_PIN[:12]}) → {kbench}")
    subprocess.run(
        ["git", "clone", KBENCH_URL, str(kbench)],
        check=True, capture_output=True, text=True,
    )
    head = subprocess.run(
        ["git", "-C", str(kbench), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    if head != KBENCH_PIN:
        subprocess.run(
            ["git", "-C", str(kbench), "checkout", KBENCH_PIN],
            check=True, capture_output=True, text=True,
        )
    if str(kbench) not in sys.path:
        sys.path.insert(0, str(kbench))
    return kbench


def load_retrieval_pin(kbench_root: Path) -> str:
    """The kernel commit KBench's retrieval tasks are pinned to (v7.1-rc7)."""
    m = json.loads((kbench_root / RETRIEVAL_MANIFEST).read_text())
    return m["commit"]


def load_tasks(kbench_root: Path) -> list[dict]:
    """KBench's retrieval task loader (mirrors kbench.cli.load_tasks)."""
    tasks_dir = kbench_root / "tasks" / "retrieval"
    tasks = []
    for f in sorted(tasks_dir.glob("**/*.json")):
        if f.name == "manifest.json":
            continue
        tasks.append(json.loads(f.read_text()))
    return tasks


def check_pin(kernel_root: Path, expected_commit: str, relax: bool) -> None:
    """Verify the kernel tree is at the pinned commit the tasks target."""
    head = subprocess.run(
        ["git", "-C", str(kernel_root), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not head:
        # Not a git tree / git missing — can't verify. Warn, don't fail hard.
        print(f"  warn: could not read kernel HEAD (git?) — pin unverified")
        return
    if head != expected_commit:
        msg = (f"kernel HEAD ({head[:12]}) != KBench retrieval pin "
               f"({expected_commit[:12]}, v7.1-rc7).\n"
               f"  The kgraph.db may have been built from a different tree than "
               f"the tasks target → non-circular GT risk.")
        if relax:
            print(f"  ⚠ pin mismatch (--relax-pin): {msg}")
        else:
            raise PinMismatch(msg + "\n  pass --relax-pin to proceed anyway.")


def run_one(kbench_root: Path, model: str, arms: list[str], reps: int,
            kernel_root: Path, kgraph_repo: Path, db_path: Path,
            results_dir: Path, max_turns: int) -> tuple[str, Path]:
    """Run one (model) A/B via KBench. Returns (run_id, results_subdir).

    Each model gets its own results subdir so inner run_ids (UTC-second-stamped
    by runner.run) can't collide across models.
    """
    from kbench.harness import runner as _runner  # imported late, after sys.path set
    from kbench.report import render as _render

    # Workaround for gateways that 502 the httpx client but accept curl: route
    # the SDK's HTTP through curl. Opt-in via env (off in a clean environment).
    import os as _os
    if _os.environ.get("KGRAPH_EVAL_CURL_TRANSPORT"):
        from kgraph_eval.curl_transport import install_curl_client_patch
        install_curl_client_patch()

    # Scoped results dir per model → no cross-model run_id collision.
    model_slug = model.replace("/", "_")
    model_results = results_dir / f"model-{model_slug}"
    model_reports = results_dir.parent / "reports"

    tasks = load_tasks(kbench_root)
    run_id = _runner.run(
        tasks=tasks, arms=arms, reps=reps, model=model,
        repo_root=kernel_root, kgraph_repo=kgraph_repo, db_path=db_path,
        results_dir=model_results, max_turns=max_turns,
    )
    # KBench's own per-run report (arm×tier×per-task) — useful as-is.
    _render.render(run_id, model_results, model_reports)
    return run_id, model_results / run_id
