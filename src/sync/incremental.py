"""
Incremental index ingestion (P4-P5 of the sync flow).

P4: run scip-clang on a filtered compile_commands.json (only rebuilt TUs) in a
    temp dir (to avoid overwriting the full index) → partial.scip.
P5: transactional per-file delete + re-insert: for each changed file, delete its
    old records (occurrences + edges, NOT symbols), then write the new batch from
    partial.scip. Scoped contains recovery + dangling-edge GC. All in one
    transaction (rollback on error).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from parser import SCIPParser
from storage import SQLiteStore


def run_partial_scip(scip_clang: str, filtered_cc: Path) -> Path:
    """Run scip-clang on a filtered compile_commands.json.

    Uses a temp dir as cwd so scip-clang writes index.scip there (not into the
    kernel dir, which would overwrite the full index). Returns the path to the
    produced index.scip (partial).

    The caller is responsible for cleaning up the temp dir after ingestion.
    """
    tmp = tempfile.mkdtemp(prefix="kgraph-sync-")
    cmd = [scip_clang, "--compdb-path", str(filtered_cc)]
    result = subprocess.run(
        cmd, cwd=tmp, capture_output=True, text=True, timeout=3600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"scip-clang failed (code {result.returncode}):\n"
            f"{result.stderr[-500:] if result.stderr else '(no stderr)'}"
        )
    partial = Path(tmp) / "index.scip"
    if not partial.exists():
        raise RuntimeError("scip-clang completed but index.scip was not produced")
    return partial


def cleanup_partial(partial_scip: Path) -> None:
    """Remove the temp dir containing the partial index."""
    try:
        parent = partial_scip.parent
        import shutil
        shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def ingest_incremental(store: SQLiteStore, partial_scip: Path) -> dict:
    """Merge a partial index into the existing DB, transactionally.

    For each Document in partial.scip:
      1. Delete the file's old occurrences + edges (NOT symbols), NULL def_file_id.
      2. Write the new batch (symbols upserted, occurrences + edges inserted).
    Then scoped contains recovery + scoped GC. All in one transaction.

    Returns a report dict: files_touched, elapsed_s.
    """
    t0 = time.perf_counter()
    touched_ids: list[int] = []
    formerly_defined: list[int] = []
    file_count = 0

    store.begin_incremental()
    try:
        for batch in SCIPParser(partial_scip).parse():
            fpath = batch.file.path
            if fpath:
                fid, fdef = store.delete_file_records(fpath)
                if fid:
                    touched_ids.append(fid)
                formerly_defined.extend(fdef)
                file_count += 1
            store.write_batch(batch)

        # Post-ingest fixes (still in transaction)
        store.scoped_contains_recovery(touched_ids)
        store.scoped_gc_dangling_edges(formerly_defined)
        store.commit_incremental()
    except Exception:
        store.rollback_incremental()
        raise

    elapsed = time.perf_counter() - t0
    return {
        "files_touched": file_count,
        "elapsed_s": round(elapsed, 2),
    }
