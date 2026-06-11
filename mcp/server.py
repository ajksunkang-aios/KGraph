"""
KGraph MCP Server — Compiler-Aware Kernel Code Graph

Exposes the SQLite code knowledge graph to AI agents via MCP tools.
A minimal viable toolset covering the most common agent code-indexing needs:

  - search_symbols     : find symbols by name (fuzzy FTS)
  - get_symbol         : exact-name symbol lookup (definition + signature)
  - get_function_body  : read the actual source body of a function/symbol
  - find_callers       : reverse call graph (who calls X) — incl. ops_bind
  - find_callees       : forward call graph (what X calls)
  - find_references    : every use of a symbol (definition + references)
  - find_type_definition: go-to-type-definition (type_of edges)
  - get_struct_layout  : struct fields (contains edges)
  - find_ops_impls     : ★ function-pointer field → implementations (kernel killer)
  - get_neighborhood   : N-hop subgraph (compact context pack)
  - call_path          : call path between two functions
  - index_status       : index metadata + statistics

Token-budget design: tools accept `limit` and `summary` parameters,
and return compact name+file:line by default rather than full source.

Usage:
    cd /path/to/linux          # kernel source with .kgraph/kgraph.db
    python -m mcp.server        # stdio MCP server (auto-launched by agent)

Environment:
    KGRAPH_DB    : path to kgraph.db (default: ./.kgraph/kgraph.db)
    KGRAPH_ROOT  : kernel source root (default: cwd)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

# ── Path setup: make src/ importable ──
_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
sys.path.insert(0, str(_PROJECT / "src"))
sys.path.insert(0, str(_PROJECT / "scripts"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from storage import SQLiteStore  # noqa: E402

# Load source_reader by file path to avoid colliding with the `mcp` SDK
# package name (this directory is also named `mcp/`).
import importlib.util as _ilu  # noqa: E402
_sr_spec = _ilu.spec_from_file_location(
    "kgraph_source_reader", str(_HERE / "source_reader.py")
)
_sr = _ilu.module_from_spec(_sr_spec)
_sr_spec.loader.exec_module(_sr)
read_source_with_lineno = _sr.read_source_with_lineno


# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

def _resolve_db_path() -> Path:
    """Locate the kgraph.db — env override or ./.kgraph/kgraph.db in cwd."""
    env = os.environ.get("KGRAPH_DB")
    if env:
        return Path(env)
    return Path.cwd() / ".kgraph" / "kgraph.db"


def _resolve_project_root() -> Path:
    """Locate the kernel source root for reading source bodies."""
    env = os.environ.get("KGRAPH_ROOT")
    if env:
        return Path(env)
    return Path.cwd()


DB_PATH = _resolve_db_path()
PROJECT_ROOT = _resolve_project_root()

# Lazy-opened store (opened on first tool call)
_store: Optional[SQLiteStore] = None


def get_store() -> SQLiteStore:
    """Get or open the SQLite store (read-only use)."""
    global _store
    if _store is None:
        if not DB_PATH.exists():
            raise FileNotFoundError(
                f"KGraph DB not found: {DB_PATH}\n"
                f"Run `kgraph init .` in the kernel source directory first."
            )
        _store = SQLiteStore(DB_PATH)
    return _store


# ──────────────────────────────────────────────
# MCP server
# ──────────────────────────────────────────────

mcp = FastMCP(
    "kgraph",
    instructions=(
        "KGraph is a compiler-aware kernel code knowledge graph. "
        "It indexes what the compiler actually sees (config-aware, macro-resolved, "
        "function-pointer-callable). Use it to answer structural questions about "
        "kernel code WITHOUT grep/file-reading:\n"
        "- 'who calls X' → find_callers\n"
        "- 'what does X call' → find_callees\n"
        "- 'show me X's source' → get_function_body\n"
        "- 'what's the type of X' → find_type_definition\n"
        "- 'where is X used' → find_references\n"
        "- 'what implements ->read_iter' → find_ops_impls (resolves indirect calls "
        "through function-pointer tables that grep can't follow)\n"
        "find_callers/find_callees already include ops_bind edges, so indirect "
        "calls through ops tables ARE captured. Prefer these tools over grep — "
        "they return precise compiler-resolved results."
    ),
)


# ── Symbol search & lookup ──

@mcp.tool()
def search_symbols(query: str, kind: Optional[str] = None, limit: int = 20) -> str:
    """
    Search symbols by name (fuzzy full-text search).

    Use when you don't know the exact symbol name. For exact names,
    prefer get_symbol (faster, precise).

    Args:
        query: name or partial name to search (FTS5 syntax supported)
        kind: optional filter — function/struct/field/macro/typedef/global_var/...
        limit: max results (default 20)

    Returns symbols with name, kind, signature, and definition location.
    """
    store = get_store()
    results = store.search_symbols(query, kind=kind, limit=limit)
    if not results:
        return f"No symbols found matching '{query}'" + (f" (kind={kind})" if kind else "")
    return _format_symbol_list(results)


@mcp.tool()
def get_symbol(name: str, kind: Optional[str] = None, limit: int = 10) -> str:
    """
    Exact-name symbol lookup — returns definition location + signature.

    Use when you know the exact symbol name (from a crash stack, a known
    function name, etc.). Returns every matching symbol (there may be
    several same-named static functions across translation units).

    Args:
        name: exact symbol name (e.g. "ext4_file_read_iter")
        kind: optional filter — function/struct/macro/...
        limit: max results (default 10)

    Returns each symbol's kind, signature, file:line, and scip_symbol id.
    """
    store = get_store()
    results = store.get_symbol(name, kind=kind, limit=limit)
    if not results:
        return f"No symbol named '{name}'" + (f" (kind={kind})" if kind else "")
    return _format_symbol_list(results, include_scip=True)


@mcp.tool()
def get_function_body(name: str, kind: Optional[str] = None,
                      context: int = 0) -> str:
    """
    Read the actual source body of a function/symbol from disk.

    Resolves the symbol by exact name, then reads its definition line
    range from the on-disk source tree. Returns source with line numbers.

    Args:
        name: exact symbol name (e.g. "ext4_file_read_iter")
        kind: optional filter (e.g. "function") to disambiguate
        context: extra lines before/after the definition (default 0)

    Returns the source code, or a not-found message. If multiple symbols
    match, returns the first non-external definition.
    """
    store = get_store()
    candidates = store.get_symbol(name, kind=kind, limit=10)
    # Prefer a candidate with a real definition location
    target = None
    for c in candidates:
        if c.get("def_file_path") and c.get("def_start_line", -1) >= 0:
            target = c
            break
    if target is None:
        if candidates:
            return (f"Symbol '{name}' found but has no on-disk definition "
                    f"(external or header-only). scip_symbol: {candidates[0]['scip_symbol']}")
        return f"No symbol named '{name}'"

    body = read_source_with_lineno(
        PROJECT_ROOT,
        target["def_file_path"],
        target["def_start_line"],
        target.get("def_end_line", target["def_start_line"]),
        context=context,
    )
    header = (f"// {target['name']} ({target['kind']}) "
              f"@ {target['def_file_path']}:{target['def_start_line'] + 1}\n")
    if body is None:
        return header + f"// (source file not readable at {PROJECT_ROOT / target['def_file_path']})"
    return header + body


# ── Call graph ──

@mcp.tool()
def find_callers(name: str, depth: int = 1, limit: int = 50) -> str:
    """
    Find functions that call the given symbol (reverse call graph).

    Includes ops_bind edges — so indirect calls through function-pointer
    tables (e.g. ->read_iter) ARE captured, which grep cannot follow.

    Args:
        name: exact symbol name
        depth: how many call levels to walk up (default 1)
        limit: max results (default 50)

    Returns callers with name, kind, edge type (calls/ops_bind), file:line.
    """
    store = get_store()
    scip = _resolve_one(store, name)
    if scip is None:
        return f"No symbol named '{name}'"
    results = store.find_callers(scip, depth=depth, limit=limit)
    if not results:
        return f"No callers found for '{name}'"
    return _format_edge_list(results, direction="caller")


@mcp.tool()
def find_callees(name: str, depth: int = 1, limit: int = 50) -> str:
    """
    Find functions called by the given symbol (forward call graph).

    Includes ops_bind edges (indirect calls through function-pointer tables).

    Args:
        name: exact symbol name
        depth: how many call levels to walk down (default 1)
        limit: max results (default 50)

    Returns callees with name, kind, edge type, file:line.
    """
    store = get_store()
    scip = _resolve_one(store, name)
    if scip is None:
        return f"No symbol named '{name}'"
    results = store.find_callees(scip, depth=depth, limit=limit)
    if not results:
        return f"No callees found for '{name}'"
    return _format_edge_list(results, direction="callee")


@mcp.tool()
def call_path(source: str, target: str, max_len: int = 10) -> str:
    """
    Find a call path between two functions.

    Args:
        source: starting function name
        target: target function name
        max_len: max path length (default 10)

    Returns the chain of functions connecting source → target, or a
    no-path message.
    """
    store = get_store()
    src = _resolve_one(store, source)
    dst = _resolve_one(store, target)
    if src is None:
        return f"No symbol named '{source}'"
    if dst is None:
        return f"No symbol named '{target}'"
    results = store.call_path(src, dst, max_len=max_len)
    if not results:
        return f"No call path found from '{source}' to '{target}' (within {max_len} hops)"
    lines = [f"Call path {source} → {target}:"]
    for i, r in enumerate(results):
        lines.append(f"  {i+1}. {r['name']} ({r['kind']}) "
                     f"@ {r.get('file_path', '?')}:{r.get('line', '?')}")
    return "\n".join(lines)


# ── References & types ──

@mcp.tool()
def find_references(name: str, limit: int = 100) -> str:
    """
    Find all references to a symbol (definition + every use site).

    Use for "where is this variable/function used". Each reference shows
    the enclosing function it appears in.

    Args:
        name: exact symbol name
        limit: max results (default 100)

    Returns each occurrence with file:line, role (def/ref), and the
    enclosing function/struct.
    """
    store = get_store()
    scip = _resolve_one(store, name)
    if scip is None:
        return f"No symbol named '{name}'"
    results = store.find_references(scip, limit=limit)
    if not results:
        return f"No references found for '{name}'"
    lines = [f"References to '{name}' ({len(results)} occurrences):"]
    for r in results:
        role = "DEF" if r["is_definition"] else "ref"
        enc = r.get("enclosing_name") or "(file scope)"
        lines.append(f"  [{role}] {r['file_path']}:{r['start_line'] + 1} "
                     f"in {enc}")
    return "\n".join(lines)


@mcp.tool()
def find_type_definition(name: str) -> str:
    """
    Go-to-type-definition: find the type of a variable/parameter.

    Follows type_of edges to the type's own definition.

    Args:
        name: exact symbol name (a variable, field, or parameter)

    Returns the type symbol(s) with kind, signature, and definition location.
    """
    store = get_store()
    scip = _resolve_one(store, name)
    if scip is None:
        return f"No symbol named '{name}'"
    results = store.find_type_definition(scip)
    if not results:
        return f"No type definition recorded for '{name}'"
    lines = [f"Type definition of '{name}':"]
    for r in results:
        loc = (f"{r.get('def_file_path', '?')}:{r['def_start_line'] + 1}"
               if r.get("def_start_line", -1) >= 0 else "(external)")
        lines.append(f"  {r['type_name']} ({r['type_kind']}) @ {loc}")
        if r.get("signature"):
            lines.append(f"    {r['signature']}")
    return "\n".join(lines)


@mcp.tool()
def get_struct_layout(name: str) -> str:
    """
    Get a struct's fields (via contains edges).

    Args:
        name: exact struct name (e.g. "file_operations")

    Returns the struct's fields with name, kind, and line.
    """
    store = get_store()
    scip = _resolve_one(store, name, prefer_kind="struct")
    if scip is None:
        return f"No struct named '{name}'"
    layout = store.get_struct_layout(scip)
    fields = layout.get("fields", [])
    if not fields:
        return f"Struct '{name}' has no recorded fields (may be opaque or external)"
    lines = [f"struct {layout.get('struct_name', name)} ({len(fields)} fields):"]
    for fld in fields:
        sig = f" — {fld['signature']}" if fld.get("signature") else ""
        lines.append(f"  .{fld['name']} ({fld['kind']}){sig}")
    return "\n".join(lines)


# ── Kernel-specific: indirect-call resolution ──

@mcp.tool()
def find_ops_impls(field_name: str, struct_type: Optional[str] = None) -> str:
    """
    ★ Resolve indirect calls through function-pointer tables.

    Finds all implementations bound to a function-pointer field (e.g.
    ->read_iter) via ops_bind edges. This captures kernel indirect
    dispatch (VFS ops, driver ops, net proto ops) that grep and
    syntax-based tools CANNOT follow.

    Args:
        field_name: the function-pointer field or impl name
                    (e.g. "read_iter")
        struct_type: optional filter by ops struct name
                     (e.g. "ext4_file_operations")

    Returns each binding: ops_table → implementation_function, file:line.
    """
    store = get_store()
    results = store.find_ops_impls(field_name, struct_type=struct_type)
    if not results:
        return f"No ops_bind implementations found for '{field_name}'"
    lines = [f"Implementations bound via '{field_name}' ({len(results)} found):"]
    for r in results:
        meta = r.get("metadata", "")
        field = ""
        if meta:
            try:
                field = json.loads(meta).get("field_name", "")
            except (json.JSONDecodeError, AttributeError):
                pass
        lines.append(f"  {r['ops_name']} → {r['impl_name']} "
                     f"@ {r.get('file_path', '?')}:{r.get('line', '?')} "
                     f"(confidence={r.get('confidence', '?')})")
    return "\n".join(lines)


# ── Neighborhood & status ──

@mcp.tool()
def get_neighborhood(name: str, depth: int = 1,
                     edge_types: Optional[str] = None,
                     summary: bool = True) -> str:
    """
    Get the N-hop neighborhood subgraph around a symbol.

    The most token-efficient way to pack context: returns the symbols
    directly related to the target (callers, callees, types, fields).

    Args:
        name: exact symbol name
        depth: hop count (default 1)
        edge_types: comma-separated edge types to follow
                    (e.g. "calls,ops_bind"); default all
        summary: if true, return compact name+file:line only (default true)

    Returns the neighborhood nodes.
    """
    store = get_store()
    scip = _resolve_one(store, name)
    if scip is None:
        return f"No symbol named '{name}'"
    et = [t.strip() for t in edge_types.split(",")] if edge_types else None
    nb = store.get_neighborhood(scip, depth=depth, edge_types=et, summary=summary)
    nodes = nb.get("nodes", [])
    if not nodes:
        return f"No neighbors found for '{name}'"
    lines = [f"Neighborhood of '{name}' (depth={depth}, {len(nodes)} nodes):"]
    for n in nodes:
        if summary:
            lines.append(f"  {n.get('name')} ({n.get('kind')}) "
                         f"@ {n.get('file', '?')}:{(n.get('line') or 0) + 1}")
        else:
            lines.append(f"  {n.get('name')} ({n.get('kind')}) "
                         f"@ {n.get('def_file_path', '?')}:{(n.get('def_start_line') or 0) + 1}")
    return "\n".join(lines)


@mcp.tool()
def index_status() -> str:
    """
    Show index metadata and statistics.

    Returns the kernel project root, tool info, symbol/file counts,
    and edge-type breakdown — useful to confirm the index is loaded.
    """
    store = get_store()
    meta = store.get_metadata()
    lines = ["KGraph index status:"]
    for k in ("project_root", "tool_name", "tool_version",
              "total_symbols", "total_files", "index_timestamp"):
        if k in meta:
            lines.append(f"  {k}: {meta[k]}")
    # Edge breakdown
    lines.append("  edges by type:")
    for row in store.conn.execute(
        "SELECT type, COUNT(*) c FROM edges GROUP BY type ORDER BY c DESC"
    ).fetchall():
        lines.append(f"    {row[0]}: {row[1]}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────

def _resolve_one(store: SQLiteStore, name: str,
                 prefer_kind: Optional[str] = None) -> Optional[str]:
    """Resolve a name to a single scip_symbol (prefer non-external defs)."""
    candidates = store.get_symbol(name, kind=prefer_kind, limit=10)
    if not candidates:
        # Retry without kind filter
        if prefer_kind:
            candidates = store.get_symbol(name, limit=10)
        if not candidates:
            return None
    # Prefer one with a real definition
    for c in candidates:
        if not c.get("is_external") and c.get("def_start_line", -1) >= 0:
            return c["scip_symbol"]
    return candidates[0]["scip_symbol"]


def _format_symbol_list(results: list[dict], include_scip: bool = False) -> str:
    """Format a list of symbol dicts compactly."""
    lines = [f"Found {len(results)} symbol(s):"]
    for r in results:
        loc = (f"{r.get('def_file_path', '?')}:{(r.get('def_start_line') or 0) + 1}"
               if r.get("def_start_line", -1) >= 0 else "(external)")
        lines.append(f"  {r['name']} ({r['kind']}) @ {loc}")
        if r.get("signature"):
            lines.append(f"    {r['signature']}")
        if include_scip:
            lines.append(f"    id: {r['scip_symbol']}")
    return "\n".join(lines)


def _format_edge_list(results: list[dict], direction: str) -> str:
    """Format a caller/callee edge list compactly."""
    lines = [f"Found {len(results)} {direction}(s):"]
    for r in results:
        depth = r.get("depth", 1)
        indent = "  " * depth
        etype = r.get("edge_type", "calls")
        marker = " [ops_bind]" if etype == "ops_bind" else ""
        lines.append(f"{indent}{r['name']} ({r['kind']})"
                     f"{marker} @ {r.get('file_path', '?')}:{(r.get('line') or 0) + 1}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()