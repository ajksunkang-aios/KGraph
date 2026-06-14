#!/usr/bin/env python3
"""
KGraph health check — synthetic retrieval benchmark + metrics collector.

Runs against a freshly-built kgraph.db (the CI index artifact). Produces a
single metrics.json that is the contract between a CI run and the GraphView
health dashboard. Stdlib-only (sqlite3) — no KGraph imports, so it runs
anywhere Python 3 does.

The "benchmark" section is a *synthetic retrieval canary*: a small set of
known-answer checks grounded in independently verified kernel facts. It is a
local, cheap proxy for correctness — if the index is healthy, all checks pass;
if a parser/indexer regression lands, a check flips. (It is distinct from
KBench, the full external benchmark.)

Usage:
    python3 bench/health_check.py \\
        --db linux/.kgraph/kgraph.db \\
        --index-scip linux/index.scip \\
        --linux-ref master --linux-head e21ee273e \\
        --out metrics.json

Timing (build/scip/ingest seconds) is read from env: build_seconds /
scip_seconds / ingest_seconds.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _edge_breakdown(conn: sqlite3.Connection) -> dict:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT type, COUNT(*) FROM edges GROUP BY type").fetchall()}


def canary_checks(conn: sqlite3.Connection, edge_breakdown: dict) -> list[dict]:
    """
    Synthetic retrieval canary. Each check is grounded in a verified fact:
      1. struct file_operations has many fields (contains edges)  — verified ~34
      2. read_iter has ops_bind bindings                          — grep's blind spot
      3. ext4 binds read_iter (ext4_file_read_iter)               — the killer example
      4. index scale sanity (symbol / ops_bind / contains counts)
      5. a known symbol (vfs_read) is present
    Thresholds are ranges (not exact) so they survive kernel-version drift.
    """
    checks = []

    def add(cid, passed, expected, actual):
        checks.append({"id": cid, "passed": bool(passed),
                       "expected": str(expected), "actual": actual})

    # 1. file_operations struct fields via contains edges
    n = conn.execute(
        "SELECT COUNT(*) FROM edges e JOIN symbols s ON e.src_id=s.id "
        "WHERE s.name='file_operations' AND e.type='contains'"
    ).fetchone()[0]
    add("struct_layout/file_operations_fields", n >= 30, ">=30", n)

    # 2. read_iter ops_bind bindings exist
    n = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE type='ops_bind' "
        "AND metadata LIKE '%read_iter%'"
    ).fetchone()[0]
    add("ops_impls/read_iter_bindings", n >= 1, ">=1", n)

    # 3. ext4 binds read_iter (the canonical indirect-call example)
    n = conn.execute(
        "SELECT COUNT(*) FROM edges e JOIN symbols d ON e.dst_id=d.id "
        "WHERE e.type='ops_bind' AND e.metadata LIKE '%read_iter%' "
        "AND d.name LIKE '%ext4%'"
    ).fetchone()[0]
    add("ops_impls/ext4_read_iter", n >= 1, ">=1", n)

    # 4. index scale sanity
    add("sanity/symbol_count", _count(conn, "symbols") > 400000,
        ">400000", _count(conn, "symbols"))
    add("sanity/ops_bind_count", edge_breakdown.get("ops_bind", 0) > 5000,
        ">5000", edge_breakdown.get("ops_bind", 0))
    add("sanity/contains_count", edge_breakdown.get("contains", 0) > 100000,
        ">100000", edge_breakdown.get("contains", 0))

    # 5. a well-known symbol exists
    n = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE name='vfs_read'").fetchone()[0]
    add("symbol/vfs_read_exists", n >= 1, ">=1", n)

    return checks


def _env_float(name: str) -> float:
    try:
        return float(os.environ.get(name, "") or 0)
    except ValueError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path to kgraph.db")
    ap.add_argument("--index-scip", default=None, help="path to index.scip (for size)")
    ap.add_argument("--linux-ref", default="")
    ap.add_argument("--linux-head", default="")
    ap.add_argument("--build-ok", default="true")
    ap.add_argument("--index-ok", default="true")
    ap.add_argument("--ingest-ok", default="true")
    ap.add_argument("--out", default="metrics.json")
    ap.add_argument("--append-jsonl", default=None,
                    help="append a one-line GraphView summary row to this JSONL file")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: db not found: {db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    eb = _edge_breakdown(conn)
    checks = canary_checks(conn, eb)

    index_scip_mb = None
    if args.index_scip:
        p = Path(args.index_scip)
        if p.exists():
            index_scip_mb = round(p.stat().st_size / 1024 / 1024, 1)

    metrics = {
        "schema_version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "linux_ref": args.linux_ref,
        "linux_head": args.linux_head,
        "outcomes": {
            "build": args.build_ok.lower() == "true",
            "index": args.index_ok.lower() == "true",
            "ingest": args.ingest_ok.lower() == "true",
        },
        "db_path": str(db),
        "db_size_mb": round(db.stat().st_size / 1024 / 1024, 1),
        "index_scip_size_mb": index_scip_mb,
        "counts": {
            "symbols": _count(conn, "symbols"),
            "files": _count(conn, "files"),
            "occurrences": _count(conn, "occurrences"),
            "edges": _count(conn, "edges"),
        },
        "edge_breakdown": eb,
        "timing_seconds": {
            "build": _env_float("build_seconds"),
            "scip": _env_float("scip_seconds"),
            "ingest": _env_float("ingest_seconds"),
        },
        "benchmark": {
            "name": "synthetic-retrieval-canary",
            "passed": sum(1 for c in checks if c["passed"]),
            "total": len(checks),
            "all_passed": all(c["passed"] for c in checks),
            "checks": checks,
        },
    }
    conn.close()

    Path(args.out).write_text(json.dumps(metrics, indent=2))

    # Optionally append a one-line summary row for the GraphView dashboard.
    if args.append_jsonl:
        row = {
            "ts": metrics["generated_at"],
            "head": metrics["linux_head"],
            "ref": metrics["linux_ref"],
            "buildable": all(metrics["outcomes"].values()),
            "benchmark_pass": metrics["benchmark"]["passed"],
            "benchmark_total": metrics["benchmark"]["total"],
            "benchmark_ok": metrics["benchmark"]["all_passed"],
            "symbols": metrics["counts"]["symbols"],
            "edges": metrics["counts"]["edges"],
            "ops_bind": metrics["edge_breakdown"].get("ops_bind", 0),
            "contains": metrics["edge_breakdown"].get("contains", 0),
            "db_mb": metrics["db_size_mb"],
            "build_s": metrics["timing_seconds"]["build"],
            "scip_s": metrics["timing_seconds"]["scip"],
            "ingest_s": metrics["timing_seconds"]["ingest"],
        }
        jsonl = Path(args.append_jsonl)
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        with open(jsonl, "a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"[health] appended GraphView row to {args.append_jsonl}")

    # Echo the benchmark summary to stdout for the run log.
    b = metrics["benchmark"]
    print(f"[health] benchmark {b['name']}: {b['passed']}/{b['total']} passed "
          f"({'OK' if b['all_passed'] else 'FAIL'})")
    for c in b["checks"]:
        flag = "✓" if c["passed"] else "✗"
        print(f"  {flag} {c['id']}: expected {c['expected']}, got {c['actual']}")
    print(f"[health] wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
