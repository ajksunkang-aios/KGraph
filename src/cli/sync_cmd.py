"""
KGraph CLI — `kgraph sync` command.

Incremental index refresh: detects rebuilt TUs (via .o mtime), runs scip-clang
on just those TUs, and merges the partial index into the existing DB.

Prereqs: `kgraph init .` must have been run (DB exists). The kernel must have
been built (make) so that .o files reflect the current source state.

Usage:
    kgraph sync <path>                  # incremental refresh after a build
    kgraph sync . --scip-clang /path    # override scip-clang binary
    kgraph sync . --force-full          # delegate to full init --force
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Make parent packages importable (mirrors init_cmd.py setup)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # src/
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from cli.init_cmd import _find_scip_clang  # noqa: E402
from storage import SQLiteStore             # noqa: E402
from sync.change_detector import detect_and_filter  # noqa: E402
from sync.incremental import ingest_incremental, run_partial_scip, cleanup_partial  # noqa: E402


def cmd_sync(argv: list[str] | None = None) -> int:
    """`kgraph sync` subcommand handler."""
    parser = argparse.ArgumentParser(
        prog="kgraph sync",
        description="Incrementally refresh the code graph after a kernel build.",
    )
    parser.add_argument(
        "path",
        help="Path to the kernel source directory (must contain compile_commands.json).",
    )
    parser.add_argument(
        "--scip-clang",
        default=None,
        help="Path to scip-clang binary (default: auto-detect).",
    )
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="Force a full rebuild (delegate to kgraph init --force).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.3,
        help="Max fraction of TUs for incremental (default 0.3 = 30%%). "
             "Above this, suggest a full rebuild.",
    )
    args = parser.parse_args(argv)
    kernel_dir = Path(args.path).resolve()

    # ── Pre-checks ──
    if not kernel_dir.is_dir():
        print(f"ERROR: Not a directory: {kernel_dir}")
        return 1

    db_path = kernel_dir / ".kgraph" / "kgraph.db"
    compdb = kernel_dir / "compile_commands.json"

    if not db_path.exists():
        print(f"ERROR: kgraph.db not found at {db_path}.")
        print("  Run `kgraph init .` first to build the initial graph.")
        return 1

    if not compdb.exists():
        print("ERROR: compile_commands.json not found.")
        print("  Build the kernel with: make CC=clang LLVM=1")
        return 1

    # ── Force-full: delegate to init ──
    if args.force_full:
        print("Forcing full rebuild — delegating to `kgraph init --force`...")
        from cli.init_cmd import cmd_init
        init_args = [str(kernel_dir), "--force"]
        if (kernel_dir / "index.scip").exists():
            init_args.append("--skip-build")
        return cmd_init(init_args)

    # ── Open store, read baseline ──
    store = SQLiteStore(db_path)
    meta = store.get_metadata()
    try:
        baseline_ts = int(float(meta.get("index_timestamp", 0)))
    except (ValueError, TypeError):
        baseline_ts = 0

    if baseline_ts == 0:
        print("WARNING: no index_timestamp in DB — all TUs treated as changed.")

    # ── P2: detect rebuilt TUs ──
    print("Scanning for rebuilt TUs...")
    with open(compdb) as f:
        total_tus = len(json.load(f))

    filtered_cc = kernel_dir / ".kgraph" / "filtered_compile_commands.json"
    targets = detect_and_filter(compdb, kernel_dir, baseline_ts, out_path=filtered_cc)

    if not targets:
        print("✓ Graph is up to date — no rebuilt TUs detected.")
        store.close()
        return 0

    n = len(targets)
    pct = n / total_tus if total_tus else 1.0
    print(f"  {n}/{total_tus} TUs rebuilt ({pct:.1%}).")

    if pct > args.threshold:
        print(f"  Above threshold ({args.threshold:.0%}) — too many changes for incremental.")
        print(f"  Run: kgraph init . --force")
        store.close()
        return 1

    # ── P4: run scip-clang on filtered compdb ──
    print(f"Running scip-clang on {n} TUs...")
    scip_clang = _find_scip_clang(args.scip_clang)
    partial_scip = run_partial_scip(scip_clang, filtered_cc)

    # ── P5: merge into DB ──
    print("Merging partial index into kgraph.db...")
    try:
        report = ingest_incremental(store, partial_scip)
    finally:
        cleanup_partial(partial_scip)

    # ── P7: update baseline ──
    store.conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ("index_timestamp", str(int(time.time()))),
    )
    store.conn.commit()
    store.close()

    print(f"\n✓ Synced {report['files_touched']} files in {report['elapsed_s']}s.")
    print(f"  Graph is now up to date with the last build.")
    return 0


if __name__ == "__main__":
    sys.exit(cmd_sync())
