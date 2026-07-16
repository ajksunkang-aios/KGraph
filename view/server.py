#!/usr/bin/env python3
"""
KGraph View — local interactive code-graph explorer.

Launches a lightweight stdlib HTTP server that:
  - serves the `view/static/` static frontend (health dashboard + explorer), and
  - exposes a read-only JSON `/api/*` wrapping SQLiteStore's query methods.

Because the same server serves the page AND the API, they are same-origin — no
CORS. Single-threaded (HTTPServer, not ThreadingHTTPServer) on purpose: the
SQLite connection is created once and is not safe to share across threads; for
a local read-only explorer, serial requests are fine.

Usage:
    kgraph view                           # env KGRAPH_DB / KGRAPH_ROOT, port 8000
    python view/server.py --db <kgraph.db> --root <linux> [--port 8000] [--no-browser]

Lines in API responses are 0-based (SCIP convention, as the store returns);
the UI adds +1 for display.
"""
from __future__ import annotations

import argparse
import importlib.util as _ilu
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Path setup: make src/ + scripts/ importable (mirror init_cmd.py) ──
_HERE = Path(__file__).resolve().parent          # view/
_PROJECT = _HERE.parent                           # repo root (or lib/kgraph/ in bundle)
for _sub in ("src", "scripts"):
    _p = str(_PROJECT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from storage import SQLiteStore  # noqa: E402
from parser.models import EdgeType  # noqa: E402

# Source reader (mcp/ dir collides with the MCP SDK pkg name → load by file path)
_sr_path = _PROJECT / "mcp" / "source_reader.py"
_sr_spec = _ilu.spec_from_file_location("kgraph_source_reader", str(_sr_path))
_sr = _ilu.module_from_spec(_sr_spec)
_sr_spec.loader.exec_module(_sr)
read_source_with_lineno = _sr.read_source_with_lineno

# Static frontend lives next to this file under view/static/ (both in the repo
# and in the npm bundle's lib/kgraph/view/static/). Resolves via _HERE (the
# view/ dir) so it works regardless of whether _PROJECT is the repo root or
# the bundle lib dir.
GRAPHVIEW_DIR = _HERE / "static"

# A live kernel graph can contain hundreds of thousands of symbols.  The View
# API is intentionally a fragment service, not a whole-graph download endpoint.
_MAX_FRAGMENT_DEPTH = 2
_MAX_FRAGMENT_NODES = 160
_MAX_FRAGMENT_EDGES = 360
_MAX_GLOBAL_NODES = 100
_MAX_GLOBAL_EDGES = 320
_MAX_FILE_SYMBOLS = 1000
_MAX_FILE_SYMBOL_OFFSET = 1_000_000
# ``HTTPServer`` is deliberately single-threaded because its SQLite connection
# belongs to that serving thread.  Bound the time spent waiting for an accepted
# client to finish its request headers, otherwise a cancelled browser navigation
# can keep every later page/API request queued indefinitely.
_HTTP_CLIENT_TIMEOUT_SECONDS = 5
_VIEW_EDGE_TYPES = frozenset({
    EdgeType.CALLS,
    EdgeType.REFERENCES,
    EdgeType.DEFINES,
    EdgeType.CONTAINS,
    EdgeType.INCLUDES,
    EdgeType.OPS_BIND,
    EdgeType.TYPE_OF,
    EdgeType.MACRO_EXPANDS,
    EdgeType.IMPLEMENTS,
})

# ── Lazy store singleton ──
_store: SQLiteStore | None = None
_DB_PATH: Path | None = None
_ROOT_PATH: Path | None = None


def get_store() -> SQLiteStore:
    global _store
    if _store is None:
        if not _DB_PATH or not _DB_PATH.exists():
            raise FileNotFoundError(
                f"KGraph DB not found: {_DB_PATH}\n"
                f"Run `kgraph init .` in the kernel source directory first, "
                f"or pass --db / set KGRAPH_DB."
            )
        _store = SQLiteStore(_DB_PATH)
    return _store


def _resolve_one(store: SQLiteStore, name: str, kind: str | None = None) -> str | None:
    """Resolve a human name to a single scip_symbol (ported from mcp/server.py)."""
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


def _resolve_requested_symbol(store: SQLiteStore, query: dict[str, str | None]) -> str | None:
    """Prefer a SCIP id from the UI; retain name lookup for legacy callers."""
    scip = query.get("scip")
    if scip:
        row = store.conn.execute(
            "SELECT 1 FROM symbols WHERE scip_symbol = ?", (scip,)
        ).fetchone()
        return scip if row else None
    name = query.get("name")
    if not name:
        return None
    return _resolve_one(store, name, query.get("kind"))


def _network_prefix(value: str | None) -> str | None:
    """Normalise a relative source-tree path used to scope the global map."""
    if not value:
        return None
    prefix = value.strip().strip("/")
    if not prefix:
        return None
    if any(part in ("", ".", "..") for part in prefix.split("/")):
        return None
    return prefix


def _source_file_path(value: str | None) -> str | None:
    """Validate one exact, source-tree-relative file path for the View API."""
    if not value:
        return None
    path = value.strip()
    if not path or path.startswith("/") or path.endswith("/") or "\\" in path:
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def _file_symbol_offset(value: str | None) -> int | None:
    """Parse a bounded, non-negative page offset without silently rewinding."""
    if value is None:
        return 0
    try:
        offset = int(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= offset <= _MAX_FILE_SYMBOL_OFFSET:
        return None
    return offset


# ── HTTP handler ──

class _MIME:
    def __init__(self):
        self.types = {".html": "text/html; charset=utf-8",
                      ".js": "application/javascript; charset=utf-8",
                      ".mjs": "application/javascript; charset=utf-8",
                      ".css": "text/css; charset=utf-8",
                      ".json": "application/json; charset=utf-8",
                      ".jsonl": "application/json; charset=utf-8",
                      ".svg": "image/svg+xml", ".png": "image/png",
                      ".ico": "image/x-icon", ".map": "application/json"}


_MIME_TYPES = _MIME().types


class Handler(BaseHTTPRequestHandler):

    # ``StreamRequestHandler.setup`` applies this to the accepted socket.
    # BaseHTTPRequestHandler then closes a slow/incomplete request on timeout,
    # allowing the single serving loop to accept the next local request.
    timeout = _HTTP_CLIENT_TIMEOUT_SECONDS

    def log_message(self, fmt, *args):  # quieter logging
        sys.stderr.write(f"[view] {self.address_string()} {fmt % args}\n")

    # ── helpers ──
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_err(self, msg, status=400):
        self._send_json({"error": msg}, status=status)

    def _qs(self, parsed):
        """parse_qs → flat {k: first-v-or-None}."""
        q = parse_qs(parsed.query)
        return {k: (v[0] if v else None) for k, v in q.items()}

    @staticmethod
    def _int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _bounded_int(cls, v, default, minimum, maximum):
        return max(minimum, min(cls._int(v, default), maximum))

    # ── routing ──
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                self._api(path, self._qs(parsed))
            else:
                self._static(path)
        except FileNotFoundError as e:
            self._send_err(str(e), status=404)
        except Exception as e:  # pragma: no cover - defensive
            self._send_err(f"{type(e).__name__}: {e}", status=500)

    # ── static (view/static/) ──
    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        if rel == "graph":
            rel = "graph.html"
        fs = (GRAPHVIEW_DIR / rel).resolve()
        # prevent traversal
        try:
            fs.relative_to(GRAPHVIEW_DIR.resolve())
        except ValueError:
            self._send_err("forbidden", status=403)
            return
        if not fs.is_file():
            self._send_err(f"not found: /{rel}", status=404)
            return
        body = fs.read_bytes()
        ct = _MIME_TYPES.get(fs.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── API ──
    def _api(self, path, q):
        store = get_store()
        seg = path[len("/api/"):]

        if seg == "status":
            self._send_json({"metadata": store.get_metadata(),
                             "edge_counts": store.get_edge_counts(),
                             "fragment_limits": {
                                 "max_depth": _MAX_FRAGMENT_DEPTH,
                                 "max_nodes": _MAX_FRAGMENT_NODES,
                                 "max_edges": _MAX_FRAGMENT_EDGES,
                             },
                             "global_network_limits": {
                                 "max_nodes": _MAX_GLOBAL_NODES,
                                 "max_edges": _MAX_GLOBAL_EDGES,
                             },
                             "file_symbol_limits": {
                                 "max_symbols": _MAX_FILE_SYMBOLS,
                                 "max_offset": _MAX_FILE_SYMBOL_OFFSET,
                             }})
            return

        if seg == "search":
            t = q.get("q")
            if not t:
                return self._send_err("missing ?q=")
            res = store.search_symbols(t, kind=q.get("kind"),
                                       limit=self._int(q.get("limit"), 50))
            return self._send_json(res)

        if seg == "resolve":
            name = q.get("name")
            if not name:
                return self._send_err("missing ?name=")
            cands = store.get_symbol(name, kind=q.get("kind"), limit=10)
            scip = _resolve_one(store, name, q.get("kind"))
            return self._send_json({"scip_symbol": scip, "candidates": cands})

        if seg == "global-network":
            raw_prefix = q.get("prefix")
            prefix = _network_prefix(raw_prefix)
            if raw_prefix and prefix is None:
                return self._send_err("invalid ?prefix= (expected a relative source-tree path)")
            et = q.get("edge_types")
            etypes = [t.strip() for t in et.split(",")] if et else None
            unknown = [edge_type for edge_type in (etypes or [])
                       if edge_type not in _VIEW_EDGE_TYPES]
            if unknown:
                return self._send_err(
                    f"unknown edge type(s): {', '.join(sorted(set(unknown)))}"
                )
            include_internal = str(q.get("include_internal") or "").lower() in {
                "1", "true", "yes", "on",
            }
            network = store.get_global_network(
                prefix=prefix,
                edge_types=etypes,
                include_internal=include_internal,
                max_nodes=self._bounded_int(
                    q.get("max_nodes"), _MAX_GLOBAL_NODES, 2, _MAX_GLOBAL_NODES,
                ),
                max_edges=self._bounded_int(
                    q.get("max_edges"), _MAX_GLOBAL_EDGES, 1, _MAX_GLOBAL_EDGES,
                ),
            )
            return self._send_json(network)

        if seg == "file-symbols":
            raw_path = q.get("path")
            if not raw_path:
                return self._send_err("missing ?path=")
            file_path = _source_file_path(raw_path)
            if file_path is None:
                return self._send_err(
                    "invalid ?path= (expected an exact relative source-file path)"
                )
            # ``limit`` is the public parameter.  Accept max_symbols as a
            # friendly alias for clients that mirror the response field.
            requested_limit = q.get("limit") or q.get("max_symbols")
            offset = _file_symbol_offset(q.get("offset"))
            if offset is None:
                return self._send_err(
                    f"invalid ?offset= (expected an integer from 0 to {_MAX_FILE_SYMBOL_OFFSET})"
                )
            result = store.get_file_symbols(
                file_path,
                limit=self._bounded_int(
                    requested_limit, 500, 1, _MAX_FILE_SYMBOLS,
                ),
                offset=offset,
            )
            if result is None:
                return self._send_err(f"No indexed file '{file_path}'", status=404)
            return self._send_json(result)

        # Endpoints below address a single symbol.  The visual explorer always
        # supplies a SCIP id so duplicate static helpers cannot be re-resolved
        # by their short name.  Name lookup remains for older links and scripts.
        name = q.get("name")
        if seg in ("neighborhood", "fragment", "callers", "callees", "struct", "body", "callchain"):
            if not q.get("scip") and not name:
                return self._send_err("missing ?scip= (or legacy ?name=)")
            scip = _resolve_requested_symbol(store, q)
            if scip is None:
                label = q.get("scip") or name
                return self._send_err(f"No indexed symbol '{label}'", status=404)

        if seg in ("neighborhood", "fragment"):
            et = q.get("edge_types")
            etypes = [t.strip() for t in et.split(",")] if et else None
            unknown = [edge_type for edge_type in (etypes or [])
                       if edge_type not in _VIEW_EDGE_TYPES]
            if unknown:
                return self._send_err(
                    f"unknown edge type(s): {', '.join(sorted(set(unknown)))}"
                )
            nb = store.get_neighborhood(
                scip,
                depth=self._bounded_int(q.get("depth"), 1, 1, _MAX_FRAGMENT_DEPTH),
                edge_types=etypes,
                summary=False,
                max_nodes=self._bounded_int(
                    q.get("max_nodes"), _MAX_FRAGMENT_NODES, 1, _MAX_FRAGMENT_NODES,
                ),
                max_edges=self._bounded_int(
                    q.get("max_edges"), _MAX_FRAGMENT_EDGES, 0, _MAX_FRAGMENT_EDGES,
                ),
            )
            return self._send_json(nb)

        if seg == "callers":
            return self._send_json(
                store.find_callers(scip, depth=self._int(q.get("depth"), 1),
                                   limit=self._int(q.get("limit"), 100)))

        if seg == "callees":
            return self._send_json(
                store.find_callees(scip, depth=self._int(q.get("depth"), 1),
                                   limit=self._int(q.get("limit"), 100)))

        if seg == "struct":
            return self._send_json(store.get_struct_layout(scip))

        if seg == "callchain":
            return self._send_json(
                store.get_callchain(scip, max_depth=self._int(q.get("max_depth"), 20)))

        if seg == "body":
            loc = store.get_definition_location(scip)
            if not loc or not loc.get("def_file_path") \
               or (loc.get("def_start_line", -1) < 0):
                return self._send_json({"name": name or scip, "body": None,
                                        "note": "no on-disk definition (external)"})
            body = read_source_with_lineno(
                _ROOT_PATH, loc["def_file_path"],
                loc["def_start_line"],
                loc.get("def_end_line", loc["def_start_line"]),
            )
            return self._send_json({"name": name or loc.get("name") or scip,
                                    "kind": loc.get("kind"),
                                    "file": loc["def_file_path"],
                                    "start_line": loc["def_start_line"],
                                    "body": body})

        if seg == "ops":
            field = q.get("field")
            if not field:
                return self._send_err("missing ?field=")
            rows = store.find_ops_impls(field, struct_type=q.get("struct_type"))
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
                    "field_name": meta.get("field_name", ""),
                    "confidence": r.get("confidence"),
                })
            return self._send_json(out)

        self._send_err(f"unknown endpoint: {seg}", status=404)


def main(argv=None) -> int:
    global _DB_PATH, _ROOT_PATH
    ap = argparse.ArgumentParser(prog="kgraph view",
                                 description="Local KGraph code-graph explorer.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", help="path to kgraph.db (default: KGRAPH_DB or ./.kgraph/kgraph.db)")
    ap.add_argument("--root", help="kernel source root (default: KGRAPH_ROOT or cwd)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    _DB_PATH = Path(args.db or os.environ.get("KGRAPH_DB") or Path.cwd() / ".kgraph" / "kgraph.db")
    _ROOT_PATH = Path(args.root or os.environ.get("KGRAPH_ROOT") or Path.cwd())

    if not GRAPHVIEW_DIR.is_dir():
        print(f"ERROR: view/static dir not found: {GRAPHVIEW_DIR}", file=sys.stderr)
        return 1

    url = f"http://localhost:{args.port}/graph.html"
    print(f"KGraph View")
    print(f"  db:   {_DB_PATH}")
    print(f"  root: {_ROOT_PATH}")
    print(f"  →     {url}   (health dashboard: http://localhost:{args.port}/)")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    httpd = HTTPServer(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
