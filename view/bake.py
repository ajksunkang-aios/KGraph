#!/usr/bin/env python3
"""
Bake curated GraphView snapshots for the static GitHub-Pages demo.

Pages can't run `view/server.py` (no live SQLite), so we pre-bake a few
high-impact views into JSON under view/static/data/. The JSON shapes are
**identical** to what graph.js consumes from /api/*, so the explorer renders
snapshots unchanged in snapshot mode (selected via data/manifest.json).

Curated views (the KGraph highlight reel):
  - read_iter ops table     — VFS indirect-call impls (the killer feature)
  - vfs_read call chain     — function → syscall root
  - file_operations layout  — struct fields (contains edges)
  - vfs_read function body  — actual source (get_function_body)

Usage:
    python view/bake.py --db <kgraph.db> --root <linux-src> --out view/static/data
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
for _sub in ("src", "scripts"):
    _p = str(_PROJECT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import importlib.util as _ilu  # noqa: E402
from storage import SQLiteStore  # noqa: E402

# Source reader (mcp/ dir collides with the MCP SDK pkg name → load by file path)
_sr_spec = _ilu.spec_from_file_location("kgraph_source_reader",
                                        str(_PROJECT / "mcp" / "source_reader.py"))
_sr = _ilu.module_from_spec(_sr_spec)
_sr_spec.loader.exec_module(_sr)
read_source_with_lineno = _sr.read_source_with_lineno


def _resolve(store: SQLiteStore, name: str, kind: str | None = None) -> str | None:
    cands = store.get_symbol(name, kind=kind, limit=10)
    if not cands:
        if kind:
            cands = store.get_symbol(name, limit=10)
        if not cands:
            return None
    for c in cands:
        if not c.get("is_external") and c.get("def_start_line", -1) >= 0:
            return c["scip_symbol"]
    return cands[0]["scip_symbol"]


# ── bakers — each returns JSON in the SAME shape as the corresponding /api endpoint ──

def bake_ops(store: SQLiteStore, field: str) -> list:
    """Shape == /api/ops (metadata parsed into field_name)."""
    rows = store.find_ops_impls(field)
    out = []
    for r in rows:
        meta = {}
        if r.get("metadata"):
            try:
                meta = json.loads(r["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        out.append({
            "ops_name": r.get("ops_name"), "impl_name": r.get("impl_name"),
            "ops_symbol": r.get("ops_symbol"), "impl_symbol": r.get("impl_symbol"),
            "file_path": r.get("file_path"), "line": r.get("line"),
            "field_name": meta.get("field_name", ""), "confidence": r.get("confidence"),
        })
    return out


def bake_callchain(store: SQLiteStore, name: str) -> list:
    """Shape == /api/callchain."""
    scip = _resolve(store, name)
    return store.get_callchain(scip, max_depth=20) if scip else []


def bake_struct(store: SQLiteStore, name: str) -> dict:
    """Shape == /api/struct."""
    scip = _resolve(store, name)
    return store.get_struct_layout(scip) if scip else {"struct_symbol": "", "struct_name": name, "fields": []}


def bake_body(store: SQLiteStore, root: Path, name: str) -> dict:
    """Shape == /api/body: actual source of a function, with line numbers."""
    scip = _resolve(store, name)
    if not scip:
        return {"name": name, "body": None, "note": "not found"}
    loc = store.get_definition_location(scip)
    if not loc or not loc.get("def_file_path") or (loc.get("def_start_line", -1) < 0):
        return {"name": name, "body": None, "note": "no on-disk definition (external)"}
    body = read_source_with_lineno(
        root, loc["def_file_path"], loc["def_start_line"],
        loc.get("def_end_line", loc["def_start_line"]),
    )
    return {"name": name, "kind": loc.get("kind"), "file": loc["def_file_path"],
            "start_line": loc["def_start_line"], "body": body}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--root", default=".",
                    help="kernel source root (for reading function bodies)")
    ap.add_argument("--out", default="view/static/data")
    args = ap.parse_args(argv)

    store = SQLiteStore(args.db)
    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    views = [
        ("ops_read_iter", "read_iter ops table — VFS indirect-call impls", "ops",
         lambda: bake_ops(store, "read_iter")),
        ("callchain_vfs_read", "vfs_read call chain → syscall root", "callchain",
         lambda: bake_callchain(store, "vfs_read")),
        ("struct_file_operations", "struct file_operations layout", "struct",
         lambda: bake_struct(store, "file_operations")),
        ("body_vfs_read", "vfs_read function body (actual source)", "body",
         lambda: bake_body(store, root, "vfs_read")),
    ]

    manifest = []
    for key, title, view, fn in views:
        data = fn()
        fname = f"snapshot_{key}.json"
        (out / fname).write_text(json.dumps(data, indent=2))
        if view == "struct":
            n = len(data.get("fields", []))
        elif view == "body":
            n = (data.get("body") or "").count("\n") + 1 if data.get("body") else 0
        else:
            n = len(data)
        manifest.append({"key": key, "title": title, "view": view,
                         "file": f"data/{fname}", "count": n})
        print(f"  {key:28s} ({view:12s}) {n} items → {fname}")

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nbaked {len(manifest)} snapshots → {out}/manifest.json")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
