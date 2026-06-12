"""
KGraph — Real index.scip ingestion test.

Parse the actual Linux kernel SCIP index and store into SQLite,
then verify with queries.
"""

import os
import sys
import time
from pathlib import Path

# Setup paths
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from parser import SCIPParser
from storage import SQLiteStore

# Per-project paths: everything lives inside the kernel source tree.
# Point KGRAPH_ROOT at the kernel source dir (where index.scip and
# .kgraph/ live). Defaults to the current working directory.
LINUX_DIR = Path(os.environ.get("KGRAPH_ROOT", Path.cwd()))
SCIP_PATH = LINUX_DIR / "index.scip"
DB_PATH = LINUX_DIR / ".kgraph" / "kgraph.db"

def ingest_real():
    """Parse real Linux kernel index.scip → SQLite."""
    print(f"SCIP index: {SCIP_PATH}")
    print(f"SCIP size: {SCIP_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"Target DB: {DB_PATH}")

    # ── Parse ──

    parser = SCIPParser(SCIP_PATH)

    # Create .kgraph directory
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # ── Store ──

    store = SQLiteStore(DB_PATH)
    store.create_schema()

    total_symbols = 0
    total_occurrences = 0
    total_edges = 0
    total_files = 0
    batch_count = 0

    t_start = time.time()

    print("\nParsing and storing...")
    try:
        for batch in parser.parse():
            batch_count += 1

            n_sym = len(batch.symbols)
            n_occ = len(batch.occurrences)
            n_edge = len(batch.edges)

            total_symbols += n_sym
            total_occurrences += n_occ
            total_edges += n_edge
            if batch.file.path:
                total_files += 1

            store.write_batch(batch)

            if batch_count % 100 == 0:
                elapsed = time.time() - t_start
                print(f"  [{batch_count} batches] "
                      f"files={total_files} sym={total_symbols} "
                      f"occ={total_occurrences} edges={total_edges} "
                      f"time={elapsed:.1f}s")

    except MemoryError:
        print("\n❌ MemoryError — 415MB index too large for full-load mode.")
        print("   Switch to parse_stream() for large indexes.")
        store.close()
        return

    store.finalize()
    elapsed = time.time() - t_start

    print(f"\n=== Ingestion complete ===")
    print(f"  Batches:     {batch_count}")
    print(f"  Files:        {total_files}")
    print(f"  Symbols:      {total_symbols}")
    print(f"  Occurrences:  {total_occurrences}")
    print(f"  Edges:        {total_edges}")
    print(f"  Time:         {elapsed:.1f}s")

    # ── Edge type breakdown ──

    edge_counts = {}
    for row in store.conn.execute(
        "SELECT type, COUNT(*) FROM edges GROUP BY type"
    ).fetchall():
        edge_counts[row[0]] = row[1]
    print(f"\n  Edge breakdown:")
    for etype, count in sorted(edge_counts.items(), key=lambda x: -x[1]):
        print(f"    {etype}: {count}")

    # ── Query verification ──

    print(f"\n=== Query verification ===")

    # 1. Search for ext4_file_read_iter
    print("\n1. search_symbols('ext4_file_read_iter')")
    results = store.search_symbols("ext4_file_read_iter", limit=5)
    for r in results:
        print(f"   {r['name']} ({r['kind']}) @ {r.get('def_file_path', '?')}:{r.get('def_start_line', '?')}")

    # 2. Find callers of ext4_file_read_iter
    if results:
        sym = results[0]['scip_symbol']
        print(f"\n2. find_callers('{sym}', depth=2)")
        callers = store.find_callers(sym, depth=2, limit=20)
        for c in callers:
            print(f"   depth={c['depth']} {c['name']} ({c['kind']}) "
                  f"type={c['edge_type']} @ {c.get('file_path', '?')}:{c.get('line', '?')}")

    # 3. Find ops_bind implementations for read_iter
    print(f"\n3. find_ops_impls('read_iter')")
    impls = store.find_ops_impls("read_iter")
    print(f"   Found {len(impls)} ops_bind implementations")
    for impl in impls[:10]:
        print(f"   {impl['ops_name']} → {impl['impl_name']} "
              f"field={impl.get('metadata', '?')} confidence={impl.get('confidence', '?')}")

    # 4. Search for vfs_read
    print(f"\n4. search_symbols('vfs_read')")
    vfs_results = store.search_symbols("vfs_read", limit=5)
    for r in vfs_results:
        print(f"   {r['name']} ({r['kind']}) @ {r.get('def_file_path', '?')}:{r.get('def_start_line', '?')}")

    # 5. Metadata
    meta = store.get_metadata()
    print(f"\n5. Metadata:")
    for k, v in meta.items():
        print(f"   {k}: {v}")

    # DB size
    db_size = DB_PATH.stat().st_size / 1024 / 1024
    print(f"\n   DB size: {db_size:.1f} MB")

    store.close()
    print("\n✅ Done!")


if __name__ == "__main__":
    ingest_real()