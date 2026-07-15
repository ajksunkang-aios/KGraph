"""
KGraph — SQLite GraphStore Implementation

Persists the code knowledge graph to a single SQLite database file.
Uses WAL mode for concurrent read/write, batch transactions for
ingestion performance, and recursive CTE for graph traversal queries.

This is the MVP storage backend. Future backends (Neo4j, custom
embedded DB) implement the same GraphStore interface.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from parser.models import (
    EdgeRecord,
    EdgeType,
    FileRecord,
    IngestBatch,
    MetadataRecord,
    OccurrenceRecord,
    SymbolKind,
    SymbolRecord,
)
from storage.graph_store import GraphStore

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Schema DDL
# ──────────────────────────────────────────────

_SCHEMA_SQL = """
-- Symbol nodes
CREATE TABLE IF NOT EXISTS symbols(
  id              INTEGER PRIMARY KEY,
  scip_symbol     TEXT UNIQUE NOT NULL,
  name            TEXT NOT NULL,
  kind            TEXT NOT NULL,
  signature       TEXT,
  documentation   TEXT,
  def_file_id     INTEGER REFERENCES files(id),
  def_start_line  INTEGER,
  def_end_line    INTEGER,
  is_external     INTEGER DEFAULT 0,
  subsystem       TEXT,
  enclosing_symbol TEXT
);

-- Exact source-file symbol navigation: WHERE def_file_id = ? with stable
-- source-order pagination.  Kept separate from the FTS index because this is
-- a definition-location query, not a text search.
CREATE INDEX IF NOT EXISTS idx_symbols_def_file_line_name
  ON symbols(def_file_id, def_start_line, name, scip_symbol);

-- Source files
CREATE TABLE IF NOT EXISTS files(
  id          INTEGER PRIMARY KEY,
  path        TEXT UNIQUE NOT NULL,
  language    TEXT,
  subsystem   TEXT,
  sha         TEXT
);

-- Occurrences (definitions and references)
CREATE TABLE IF NOT EXISTS occurrences(
  id                  INTEGER PRIMARY KEY,
  symbol_id           INTEGER NOT NULL REFERENCES symbols(id),
  file_id             INTEGER NOT NULL REFERENCES files(id),
  start_line          INTEGER NOT NULL,
  start_col           INTEGER NOT NULL,
  end_line            INTEGER NOT NULL,
  end_col             INTEGER NOT NULL,
  role                INTEGER NOT NULL,
  enclosing_symbol_id INTEGER REFERENCES symbols(id)
);

CREATE INDEX IF NOT EXISTS idx_occ_symbol ON occurrences(symbol_id);
CREATE INDEX IF NOT EXISTS idx_occ_file ON occurrences(file_id, start_line);
CREATE INDEX IF NOT EXISTS idx_occ_enclosing ON occurrences(enclosing_symbol_id);

-- Generic edge table
CREATE TABLE IF NOT EXISTS edges(
  src_id      INTEGER NOT NULL REFERENCES symbols(id),
  dst_id      INTEGER NOT NULL REFERENCES symbols(id),
  type        TEXT NOT NULL,
  file_id     INTEGER REFERENCES files(id),
  line        INTEGER,
  weight      INTEGER DEFAULT 1,
  confidence  REAL DEFAULT 1.0,
  metadata    TEXT,
  PRIMARY KEY(src_id, dst_id, type, file_id, line)
);

CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(src_id, type);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edges(dst_id, type);
CREATE INDEX IF NOT EXISTS idx_edge_type ON edges(type);

-- Full-text search (optional, enabled)
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
  name, signature, documentation,
  content=symbols, content_rowid=id
);

-- Index metadata
CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# ──────────────────────────────────────────────
# Batch insert SQL (prepared statements)
# ──────────────────────────────────────────────

_INSERT_FILE = """
INSERT OR IGNORE INTO files (path, language, subsystem, sha)
VALUES (?, ?, ?, ?)
"""

_INSERT_SYMBOL = """
INSERT OR IGNORE INTO symbols
  (scip_symbol, name, kind, signature, documentation,
   def_file_id, def_start_line, def_end_line,
   is_external, subsystem, enclosing_symbol)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_OCCURRENCE = """
INSERT INTO occurrences
  (symbol_id, file_id, start_line, start_col, end_line, end_col,
   role, enclosing_symbol_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_EDGE = """
INSERT OR IGNORE INTO edges
  (src_id, dst_id, type, file_id, line, weight, confidence, metadata)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_META = """
INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)
"""

# ──────────────────────────────────────────────
# FTS5 sync triggers
# ──────────────────────────────────────────────

_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS symbols_fts_insert AFTER INSERT ON symbols BEGIN
  INSERT INTO symbols_fts(rowid, name, signature, documentation)
  VALUES (new.id, new.name, new.signature, new.documentation);
END;

CREATE TRIGGER IF NOT EXISTS symbols_fts_delete AFTER DELETE ON symbols BEGIN
  INSERT INTO symbols_fts(symbols_fts, rowid, name, signature, documentation)
  VALUES ('delete', old.id, old.name, old.signature, old.documentation);
END;

CREATE TRIGGER IF NOT EXISTS symbols_fts_update AFTER UPDATE ON symbols BEGIN
  INSERT INTO symbols_fts(symbols_fts, rowid, name, signature, documentation)
  VALUES ('delete', old.id, old.name, old.signature, old.documentation);
  INSERT INTO symbols_fts(rowid, name, signature, documentation)
  VALUES (new.id, new.name, new.signature, new.documentation);
END;
"""


# Keep the fragment API deliberately small and explicit.  These values are
# persisted by the parser and safe to expose as a query filter; arbitrary
# strings must never be interpolated into a SQL IN clause.
_NEIGHBORHOOD_EDGE_TYPES = frozenset({
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
_DEFAULT_NEIGHBORHOOD_EDGE_TYPES = (
    EdgeType.CALLS,
    EdgeType.REFERENCES,
    EdgeType.OPS_BIND,
    EdgeType.IMPLEMENTS,
    EdgeType.TYPE_OF,
    EdgeType.CONTAINS,
)

_FILE_SYMBOLS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbols_def_file_line_name
  ON symbols(def_file_id, def_start_line, name, scip_symbol)
"""
_MAX_FILE_SYMBOLS_OFFSET = 1_000_000


def _bounded_int(value: int, default: int, minimum: int, maximum: int) -> int:
    """Clamp public graph-fragment limits to predictable, safe bounds."""
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


class SQLiteStore(GraphStore):
    """
    SQLite-backed implementation of GraphStore.

    Usage:
        store = SQLiteStore("/path/to/.kgraph/kgraph.db")
        store.create_schema()
        for batch in parser.parse():
            store.write_batch(batch)
        store.finalize()
    """

    BATCH_SIZE = 10000  # Number of rows per transaction commit

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.row_factory = sqlite3.Row

        # Indexes in _SCHEMA_SQL cover newly created databases.  View also
        # opens long-lived pre-existing indexes, so install this one lazily at
        # startup when the symbols table already exists.  CREATE INDEX IF NOT
        # EXISTS makes the migration idempotent and keeps versioned schema
        # bookkeeping unnecessary for this additive optimization.
        self._ensure_file_symbols_index()

        # Ingestion caches: scip_symbol → rowid
        self._symbol_id_cache: dict[str, int] = {}
        self._file_id_cache: dict[str, int] = {}
        self._batch_counter = 0

        # Bulk-load tuning + per-phase timing instrumentation.
        self._bulk_mode = False
        self._incremental_mode = False
        _vmaj_vmin = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
        self._supports_returning = _vmaj_vmin >= (3, 35)  # INSERT ... RETURNING
        self._stats: dict[str, float] = {
            "symbol_write_s": 0.0, "occ_write_s": 0.0, "edge_write_s": 0.0,
            "symbols": 0, "occurrences": 0, "edges": 0, "batches": 0,
        }

    def _ensure_file_symbols_index(self) -> None:
        """Install the source-file browsing index on existing databases."""
        has_symbols_table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'symbols'"
        ).fetchone()
        if has_symbols_table is None:
            return
        try:
            self.conn.execute(_FILE_SYMBOLS_INDEX_SQL)
            self.conn.commit()
        except sqlite3.OperationalError as exc:
            # A viewer can intentionally point at a read-only DB, or another
            # process can temporarily hold the schema write lock.  The query
            # remains correct without this additive index, so do not turn an
            # optional optimization into a View startup failure.
            try:
                self.conn.rollback()
            except sqlite3.Error:  # pragma: no cover - defensive cleanup
                pass
            logger.warning(
                "Could not install file-symbols index; falling back to scan: %s",
                exc,
            )

    # ── Schema creation ──

    def create_schema(self) -> None:
        """Create all tables, indexes, and FTS triggers."""
        logger.info("Creating schema in %s", self.db_path)
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.executescript(_FTS_TRIGGERS)
        self.conn.commit()

    # ── Bulk-load mode (full ingest) ──

    def begin_bulk_load(self) -> None:
        """Enter bulk-load mode for a full ingest.

        - One transaction for the whole ingest (write_batch's periodic commit
          is suppressed; finalize() commits once).
        - PRAGMA synchronous=OFF + temp_store=MEMORY: the DB is rebuildable
          from index.scip, so we trade crash-durability during load for speed.
        finalize() restores synchronous=NORMAL.
        """
        self._bulk_mode = True
        self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.execute("PRAGMA temp_store=MEMORY")

    def ingest_stats(self) -> dict:
        """Per-phase write timings + row counts accumulated by write_batch."""
        return dict(self._stats)

    # ── Batch write (ingestion) ──

    def write_batch(self, batch: IngestBatch) -> None:
        """Persist one IngestBatch."""
        try:
            # 1. Write file
            file_id = self._write_file(batch.file)

            # 2. Write symbols
            t = time.perf_counter()
            self._write_symbols(batch.symbols, file_id)
            self._stats["symbol_write_s"] += time.perf_counter() - t
            self._stats["symbols"] += len(batch.symbols)

            # 3. Write occurrences (batched via executemany)
            t = time.perf_counter()
            self._write_occurrences(batch.occurrences, file_id)
            self._stats["occ_write_s"] += time.perf_counter() - t
            self._stats["occurrences"] += len(batch.occurrences)

            # 4. Write edges (batched via executemany)
            t = time.perf_counter()
            self._write_edges(batch.edges, file_id)
            self._stats["edge_write_s"] += time.perf_counter() - t
            self._stats["edges"] += len(batch.edges)

            # 5. Write metadata
            self._write_metadata(batch.metadata)
            self._stats["batches"] += 1

            # Periodic commit (suppressed during bulk load; finalize commits once)
            self._batch_counter += 1
            if not self._incremental_mode and not self._bulk_mode and self._batch_counter % 10 == 0:
                self.conn.commit()
                logger.debug("Committed after %d batches", self._batch_counter)

        except sqlite3.Error as e:
            logger.error("SQLite error writing batch for %s: %s",
                         batch.file.path, e)
            raise

    def finalize(self) -> None:
        """Final commit and index optimization."""
        self.conn.commit()

        # Cross-document contains recovery. The parser's Step 6 derives
        # struct→field containment only within a single Document (it checks
        # the current Document's symbol_map). When a struct and its fields
        # land in different Documents — common in real scip-clang output —
        # those contains edges are missed and get_struct_layout returns
        # empty (the prepend_buffer failure). Recover them here from
        # symbols.enclosing_symbol (populated for every field from its
        # symbol name), now that all symbols are in the DB. INSERT OR IGNORE
        # dedupes against Step 6's same-Document edges (composite PK
        # src_id,dst_id,type,file_id,line).
        self.conn.execute(
            """
            INSERT OR IGNORE INTO edges (src_id, dst_id, type, file_id, line, weight, confidence)
            SELECT s.id, f.id, 'contains', f.def_file_id, f.def_start_line, 1, 1.0
            FROM symbols f
            JOIN symbols s ON f.enclosing_symbol = s.scip_symbol
            WHERE f.enclosing_symbol != '' AND f.kind = 'field'
            """
        )
        self.conn.commit()

        # Optimize FTS
        self.conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES ('optimize')")
        self.conn.commit()

        # Write final metadata
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("index_timestamp", str(int(__import__("time").time()))),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("total_symbols", str(len(self._symbol_id_cache))),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("total_files", str(len(self._file_id_cache))),
        )
        self.conn.commit()

        # Restore bulk-load PRAGMAs (DB is committed; future writes use the
        # normal durable settings).
        if self._bulk_mode:
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self._bulk_mode = False

        logger.info("Finalized: %d symbols, %d files",
                     len(self._symbol_id_cache), len(self._file_id_cache))

    # ── Internal write methods ──

    def _write_file(self, file_rec: FileRecord) -> int:
        """Insert a file record and return its rowid."""
        if file_rec.path in self._file_id_cache:
            return self._file_id_cache[file_rec.path]

        self.conn.execute(_INSERT_FILE, (
            file_rec.path, file_rec.language,
            file_rec.subsystem, file_rec.sha,
        ))

        rowid = self._get_file_id(file_rec.path)
        self._file_id_cache[file_rec.path] = rowid
        return rowid

    def _write_symbols(self, symbols: list[SymbolRecord], file_id: int) -> None:
        """Insert symbol records. Existing symbols (by scip_symbol) keep their
        rowid (upsert) so inbound edges stay valid; new symbols use
        INSERT ... RETURNING id to avoid a second SELECT per symbol."""
        for sym in symbols:
            if sym.scip_symbol in self._symbol_id_cache:
                # Update existing symbol's definition location
                self.conn.execute(
                    "UPDATE symbols SET def_file_id=?, def_start_line=?, def_end_line=? "
                    "WHERE scip_symbol=? AND def_file_id IS NULL",
                    (file_id, sym.def_start_line, sym.def_end_line, sym.scip_symbol),
                )
                continue

            params = (
                sym.scip_symbol,
                sym.name,
                sym.kind,
                sym.signature,
                sym.documentation,
                file_id,
                sym.def_start_line,
                sym.def_end_line,
                int(sym.is_external),
                sym.subsystem,
                sym.enclosing_symbol,
            )
            if self._supports_returning:
                row = self.conn.execute(_INSERT_SYMBOL + " RETURNING id", params).fetchone()
                rowid = row[0] if row is not None else self._get_symbol_id(sym.scip_symbol)
            else:
                self.conn.execute(_INSERT_SYMBOL, params)
                rowid = self._get_symbol_id(sym.scip_symbol)
            self._symbol_id_cache[sym.scip_symbol] = rowid
            # Restore def location if NULLed (incremental: delete_file_records
            # NULLed it so the re-ingest upsert can re-establish it).
            self.conn.execute(
                "UPDATE symbols SET def_file_id=?, def_start_line=?, def_end_line=? "
                "WHERE id=? AND def_file_id IS NULL",
                (file_id, sym.def_start_line, sym.def_end_line, rowid),
            )

    def _write_occurrences(self, occurrences: list[OccurrenceRecord],
                           file_id: int) -> None:
        """Insert occurrence records (batched: resolve ids in Python via the
        symbol-id cache, then one executemany per Document)."""
        rows = []
        for occ in occurrences:
            symbol_id = self._get_symbol_id(occ.symbol)
            if symbol_id is None:
                continue  # Skip occurrences with unknown symbols

            enclosing_id = None
            if occ.enclosing_symbol:
                enclosing_id = self._get_symbol_id(occ.enclosing_symbol)

            rows.append((
                symbol_id, file_id,
                occ.start_line, occ.start_col,
                occ.end_line, occ.end_col,
                occ.role, enclosing_id,
            ))
        if rows:
            self.conn.executemany(_INSERT_OCCURRENCE, rows)

    def _write_edges(self, edges: list[EdgeRecord], file_id: int) -> None:
        """Insert edge records (batched: resolve src/dst ids in Python via the
        caches, then one executemany per Document)."""
        rows = []
        for edge in edges:
            src_id = self._get_symbol_id(edge.src_symbol)
            dst_id = self._get_symbol_id(edge.dst_symbol)
            if src_id is None or dst_id is None:
                continue  # Skip edges with unknown symbols

            edge_file_id = self._get_file_id(edge.file_path) if edge.file_path else None

            rows.append((
                src_id, dst_id, edge.type,
                edge_file_id, edge.line,
                edge.weight, edge.confidence,
                edge.metadata,
            ))
        if rows:
            self.conn.executemany(_INSERT_EDGE, rows)

    def _write_metadata(self, metadata: list[MetadataRecord]) -> None:
        """Insert metadata records."""
        for meta in metadata:
            self.conn.execute(_INSERT_META, (meta.key, meta.value))

    # ── ID lookup helpers ──

    def _get_symbol_id(self, scip_symbol: str) -> Optional[int]:
        """Get the rowid for a scip_symbol string."""
        if scip_symbol in self._symbol_id_cache:
            return self._symbol_id_cache[scip_symbol]

        row = self.conn.execute(
            "SELECT id FROM symbols WHERE scip_symbol=?", (scip_symbol,)
        ).fetchone()
        if row:
            self._symbol_id_cache[scip_symbol] = row[0]
            return row[0]
        return None

    def _get_file_id(self, path: str) -> Optional[int]:
        """Get the rowid for a file path."""
        if path in self._file_id_cache:
            return self._file_id_cache[path]

        row = self.conn.execute(
            "SELECT id FROM files WHERE path=?", (path,)
        ).fetchone()
        if row:
            self._file_id_cache[path] = row[0]
            return row[0]
        return None

    # ── Read side (querying) ──

    def search_symbols(self, query: str, kind: Optional[str] = None,
                       limit: int = 50) -> list[dict]:
        """Search symbols by name using FTS5."""
        if kind:
            sql = """
                SELECT s.id, s.scip_symbol, s.name, s.kind, s.signature,
                       f.path as def_file_path, s.def_start_line
                FROM symbols s
                JOIN symbols_fts fts ON fts.rowid = s.id
                LEFT JOIN files f ON s.def_file_id = f.id
                WHERE symbols_fts MATCH ?
                  AND s.kind = ?
                ORDER BY rank
                LIMIT ?
            """
            rows = self.conn.execute(sql, (query, kind, limit)).fetchall()
        else:
            sql = """
                SELECT s.id, s.scip_symbol, s.name, s.kind, s.signature,
                       f.path as def_file_path, s.def_start_line
                FROM symbols s
                JOIN symbols_fts fts ON fts.rowid = s.id
                LEFT JOIN files f ON s.def_file_id = f.id
                WHERE symbols_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            rows = self.conn.execute(sql, (query, limit)).fetchall()

        return [dict(r) for r in rows]

    def find_callers(self, scip_symbol: str, depth: int = 1,
                     limit: int = 100) -> list[dict]:
        """Find callers via recursive CTE on edges table."""
        sym_id = self._get_symbol_id(scip_symbol)
        if sym_id is None:
            return []

        sql = """
            WITH RECURSIVE callers(depth, src_id, edge_type, file_id, line) AS (
                SELECT 1, src_id, type, file_id, line
                FROM edges
                WHERE dst_id = ? AND type IN ('calls', 'ops_bind')
                UNION ALL
                SELECT c.depth + 1, e.src_id, e.type, e.file_id, e.line
                FROM edges e
                JOIN callers c ON e.dst_id = c.src_id
                WHERE c.depth < ? AND e.type IN ('calls', 'ops_bind')
            )
            SELECT c.depth, c.edge_type, c.line,
                   s.scip_symbol, s.name, s.kind,
                   f.path as file_path
            FROM callers c
            JOIN symbols s ON c.src_id = s.id
            LEFT JOIN files f ON c.file_id = f.id
            ORDER BY c.depth, c.line
            LIMIT ?
        """
        rows = self.conn.execute(sql, (sym_id, depth, limit)).fetchall()
        return [dict(r) for r in rows]

    def find_callees(self, scip_symbol: str, depth: int = 1,
                     limit: int = 100) -> list[dict]:
        """Find callees via recursive CTE on edges table."""
        sym_id = self._get_symbol_id(scip_symbol)
        if sym_id is None:
            return []

        sql = """
            WITH RECURSIVE callees(depth, dst_id, edge_type, file_id, line) AS (
                SELECT 1, dst_id, type, file_id, line
                FROM edges
                WHERE src_id = ? AND type IN ('calls', 'ops_bind')
                UNION ALL
                SELECT c.depth + 1, e.dst_id, e.type, e.file_id, e.line
                FROM edges e
                JOIN callees c ON e.src_id = c.dst_id
                WHERE c.depth < ? AND e.type IN ('calls', 'ops_bind')
            )
            SELECT c.depth, c.edge_type, c.line,
                   s.scip_symbol, s.name, s.kind,
                   f.path as file_path
            FROM callees c
            JOIN symbols s ON c.dst_id = s.id
            LEFT JOIN files f ON c.file_id = f.id
            ORDER BY c.depth, c.line
            LIMIT ?
        """
        rows = self.conn.execute(sql, (sym_id, depth, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_neighborhood(self, scip_symbol: str, depth: int = 1,
                         edge_types: Optional[list[str]] = None,
                         summary: bool = False,
                         max_nodes: int = 160,
                         max_edges: int = 360) -> dict:
        """Return a bounded, evidence-preserving N-hop graph fragment.

        The previous implementation returned only nodes.  Consumers then had to
        fabricate a center-star graph, which lost directed relationships and
        collapsed parallel ``calls`` / ``ops_bind`` edges.  This breadth-first
        traversal keeps the actual edge rows and bounds each response before it
        can turn a high-degree kernel symbol into a browser-sized full graph.
        """
        max_depth = _bounded_int(depth, 1, 1, 3)
        max_nodes = _bounded_int(max_nodes, 160, 1, 500)
        max_edges = _bounded_int(max_edges, 360, 0, 1_000)
        requested_types = edge_types or list(_DEFAULT_NEIGHBORHOOD_EDGE_TYPES)
        types = [edge_type for edge_type in requested_types
                 if edge_type in _NEIGHBORHOOD_EDGE_TYPES]

        empty = {
            "center_symbol": scip_symbol,
            "center": {},
            "nodes": [],
            "edges": [],
            "truncated": False,
            "truncation": {"nodes": False, "edges": False},
            "limits": {"max_depth": max_depth, "max_nodes": max_nodes,
                       "max_edges": max_edges},
        }
        sym_id = self._get_symbol_id(scip_symbol)
        if sym_id is None:
            return empty

        center_row = self.conn.execute(
            """
            SELECT s.id, s.scip_symbol, s.name, s.kind,
                   f.path AS def_file_path, s.def_start_line
            FROM symbols s
            LEFT JOIN files f ON s.def_file_id = f.id
            WHERE s.id = ?
            """,
            (sym_id,),
        ).fetchone()
        if center_row is None:  # defensive: _get_symbol_id already found it
            return empty

        center = dict(center_row)
        center.pop("id", None)
        node_by_id = {sym_id: center}
        frontier = {sym_id}
        edge_by_id: dict[int, dict] = {}
        nodes_truncated = False
        edges_truncated = False

        if types and max_edges:
            type_marks = ", ".join("?" for _ in types)
            for _hop in range(max_depth):
                if not frontier or len(edge_by_id) >= max_edges:
                    if frontier and len(edge_by_id) >= max_edges:
                        edges_truncated = True
                    break

                frontier_ids = sorted(frontier)
                frontier_marks = ", ".join("?" for _ in frontier_ids)
                # Ask for one extra row so the response can say when it had to
                # stop.  The edge joins also provide complete node display data,
                # avoiding a second unbounded node query.
                remaining = max_edges - len(edge_by_id)
                sql = f"""
                    SELECT e.rowid AS edge_id, e.src_id, e.dst_id,
                           e.type, e.weight, e.confidence, e.metadata,
                           f.path AS file_path, e.line,
                           src.scip_symbol AS src_symbol, src.name AS src_name,
                           src.kind AS src_kind, src_f.path AS src_def_file_path,
                           src.def_start_line AS src_def_start_line,
                           dst.scip_symbol AS dst_symbol, dst.name AS dst_name,
                           dst.kind AS dst_kind, dst_f.path AS dst_def_file_path,
                           dst.def_start_line AS dst_def_start_line
                    FROM edges e
                    JOIN symbols src ON src.id = e.src_id
                    JOIN symbols dst ON dst.id = e.dst_id
                    LEFT JOIN files f ON f.id = e.file_id
                    LEFT JOIN files src_f ON src_f.id = src.def_file_id
                    LEFT JOIN files dst_f ON dst_f.id = dst.def_file_id
                    WHERE (e.src_id IN ({frontier_marks})
                           OR e.dst_id IN ({frontier_marks}))
                      AND e.type IN ({type_marks})
                    ORDER BY CASE e.type
                        WHEN 'calls' THEN 0
                        WHEN 'ops_bind' THEN 1
                        ELSE 2
                    END, e.rowid
                    LIMIT ?
                """
                params = [*frontier_ids, *frontier_ids, *types, remaining + 1]
                rows = self.conn.execute(sql, params).fetchall()
                if len(rows) > remaining:
                    edges_truncated = True

                next_frontier: set[int] = set()
                for row in rows:
                    edge_id = row["edge_id"]
                    if edge_id in edge_by_id:
                        continue
                    if len(edge_by_id) >= max_edges:
                        edges_truncated = True
                        break

                    endpoint_rows = (
                        (row["src_id"], row["src_symbol"], row["src_name"],
                         row["src_kind"], row["src_def_file_path"],
                         row["src_def_start_line"]),
                        (row["dst_id"], row["dst_symbol"], row["dst_name"],
                         row["dst_kind"], row["dst_def_file_path"],
                         row["dst_def_start_line"]),
                    )
                    edge_has_hidden_node = False
                    for node_id, symbol, name, kind, file_path, start_line in endpoint_rows:
                        if node_id in node_by_id:
                            continue
                        if len(node_by_id) >= max_nodes:
                            nodes_truncated = True
                            edge_has_hidden_node = True
                            break
                        node_by_id[node_id] = {
                            "scip_symbol": symbol,
                            "name": name,
                            "kind": kind,
                            "def_file_path": file_path,
                            "def_start_line": start_line,
                        }
                        next_frontier.add(node_id)

                    if edge_has_hidden_node:
                        continue

                    metadata = {}
                    if row["metadata"]:
                        try:
                            metadata = json.loads(row["metadata"])
                        except (TypeError, json.JSONDecodeError):
                            metadata = {"raw": row["metadata"]}
                    evidence = None
                    if row["file_path"] and row["line"] is not None and row["line"] >= 0:
                        evidence = {"file_path": row["file_path"], "line": row["line"]}
                    edge_by_id[edge_id] = {
                        "id": f"edge:{edge_id}",
                        "source": row["src_symbol"],
                        "target": row["dst_symbol"],
                        "type": row["type"],
                        "weight": row["weight"],
                        "confidence": row["confidence"],
                        "evidence": evidence,
                        "metadata": metadata,
                    }

                frontier = next_frontier

        nodes = [node for node_id, node in node_by_id.items() if node_id != sym_id]
        if summary:
            nodes = [
                {"scip_symbol": node["scip_symbol"], "name": node["name"],
                 "kind": node["kind"], "file": node["def_file_path"],
                 "line": node["def_start_line"]}
                for node in nodes
            ]

        return {
            "center_symbol": scip_symbol,
            "center": center,
            "nodes": nodes,
            "edges": list(edge_by_id.values()),
            "truncated": nodes_truncated or edges_truncated,
            "truncation": {"nodes": nodes_truncated, "edges": edges_truncated},
            "limits": {"max_depth": max_depth, "max_nodes": max_nodes,
                       "max_edges": max_edges},
        }

    @staticmethod
    def _network_group_path(path: Optional[str], prefix: Optional[str]) -> str:
        """Map one source path to the current directory-map granularity."""
        if path is None:
            return "@external"
        if not path:
            return "@outside" if prefix else "@root"
        if not prefix:
            return path.split("/", 1)[0] or "@root"

        scoped_prefix = f"{prefix}/"
        if not path.startswith(scoped_prefix):
            return "@outside"
        child = path[len(scoped_prefix):].split("/", 1)[0]
        return f"{prefix}/{child}" if child else prefix

    @staticmethod
    def _network_group_label(group: str) -> str:
        if group == "@outside":
            return "outside scope"
        if group == "@external":
            return "external"
        if group == "@root":
            return "repository root"
        return group.rsplit("/", 1)[-1]

    def get_global_network(self, prefix: Optional[str] = None,
                           edge_types: Optional[list[str]] = None,
                           include_internal: bool = False,
                           max_nodes: int = 100,
                           max_edges: int = 320) -> dict:
        """Build a bounded global code map from real file-level relationships.

        A kernel-scale database has far too many symbol nodes for a browser
        graph.  This query first aggregates the edge table by defining file
        pair in SQLite, then rolls those compact rows up to the first directory
        below ``prefix`` in Python.  The work is proportional to the distinct
        file pairs (tens of thousands in a Linux index), rather than the full
        symbol graph, and the JSON result remains capped.
        """
        clean_prefix = (prefix or "").strip().strip("/") or None
        max_nodes = _bounded_int(max_nodes, 100, 2, 160)
        max_edges = _bounded_int(max_edges, 320, 1, 420)
        requested_types = edge_types or [EdgeType.CALLS, EdgeType.OPS_BIND]
        types = [edge_type for edge_type in requested_types
                 if edge_type in _NEIGHBORHOOD_EDGE_TYPES]

        empty = {
            "scope": {
                "prefix": clean_prefix,
                "label": "Linux" if not clean_prefix else f"Linux / {clean_prefix}",
                "parent": "/".join(clean_prefix.split("/")[:-1]) if clean_prefix else None,
            },
            "nodes": [],
            "edges": [],
            "totals": {"files": 0, "symbols": 0, "relationships": 0},
            "truncated": False,
            "truncation": {"nodes": False, "edges": False},
            "limits": {"max_nodes": max_nodes, "max_edges": max_edges},
            "edge_types": types,
        }
        if not types:
            return empty

        file_rows = self.conn.execute(
            """
            SELECT f.path, COUNT(s.id) AS symbols
            FROM files f
            LEFT JOIN symbols s ON s.def_file_id = f.id AND s.is_external = 0
            WHERE f.path <> ''
            GROUP BY f.id, f.path
            """
        ).fetchall()

        groups: dict[str, dict[str, int]] = defaultdict(lambda: {
            "files": 0,
            "symbols": 0,
            "incoming": 0,
            "outgoing": 0,
            "internal": 0,
        })
        drillable: set[str] = set()
        file_groups: set[str] = set()
        for row in file_rows:
            path = row["path"]
            group = self._network_group_path(path, clean_prefix)
            groups[group]["files"] += 1
            groups[group]["symbols"] += int(row["symbols"] or 0)
            # At a directory's immediate-child granularity, a source file is
            # represented by its own exact path while a directory represents
            # one or more descendants.  The UI needs this distinction to use
            # the correct drill-down action (file symbol list vs. sub-map).
            if group == path:
                file_groups.add(group)
            if not group.startswith("@") and path.startswith(f"{group}/"):
                drillable.add(group)

        type_marks = ", ".join("?" for _ in types)
        # A global source map represents definitions in the indexed tree.
        # SCIP external placeholders use an empty defining-file path; retaining
        # them produces an isolated "repository root" node with no navigable
        # source directory.  Keep external symbols available to the symbol
        # explorer, but omit them from this directory-level map.
        where_parts = [
            f"e.type IN ({type_marks})",
            "src.is_external = 0",
            "dst.is_external = 0",
            "src_f.path <> ''",
            "dst_f.path <> ''",
        ]
        params: list[object] = [*types]
        if clean_prefix:
            path_like = f"{clean_prefix}/%"
            where_parts.append(
                "(src_f.path = ? OR src_f.path LIKE ? OR dst_f.path = ? OR dst_f.path LIKE ?)"
            )
            params.extend([clean_prefix, path_like, clean_prefix, path_like])

        rows = self.conn.execute(
            f"""
            SELECT src_f.path AS src_path, dst_f.path AS dst_path, e.type,
                   COUNT(*) AS relationships,
                   SUM(COALESCE(e.weight, 1)) AS weight
            FROM edges e
            JOIN symbols src ON src.id = e.src_id
            JOIN files src_f ON src_f.id = src.def_file_id
            JOIN symbols dst ON dst.id = e.dst_id
            JOIN files dst_f ON dst_f.id = dst.def_file_id
            WHERE {' AND '.join(where_parts)}
            GROUP BY src_f.path, dst_f.path, e.type
            """,
            params,
        ).fetchall()

        edge_by_key: dict[tuple[str, str, str], dict] = {}
        total_relationships = 0
        for row in rows:
            source = self._network_group_path(row["src_path"], clean_prefix)
            target = self._network_group_path(row["dst_path"], clean_prefix)
            relationships = int(row["relationships"] or 0)
            weight = int(row["weight"] or relationships)
            total_relationships += relationships
            groups[source]  # ensure nodes found only through relationships are retained
            groups[target]
            if source == target:
                groups[source]["internal"] += relationships
                if not include_internal:
                    continue
            groups[source]["outgoing"] += relationships
            groups[target]["incoming"] += relationships
            key = (source, target, row["type"])
            aggregate = edge_by_key.setdefault(key, {
                "id": f"global:{source}>{target}:{row['type']}",
                "source": source,
                "target": target,
                "type": row["type"],
                "relationships": 0,
                "weight": 0,
            })
            aggregate["relationships"] += relationships
            aggregate["weight"] += weight

        ranked_groups = sorted(
            groups,
            key=lambda group: (
                groups[group]["incoming"] + groups[group]["outgoing"],
                groups[group]["symbols"],
                groups[group]["files"],
                group,
            ),
            reverse=True,
        )
        nodes_truncated = len(ranked_groups) > max_nodes
        selected_groups = set(ranked_groups[:max_nodes])
        if clean_prefix and "@outside" in groups and "@outside" not in selected_groups:
            selected_groups.discard(ranked_groups[-1])
            selected_groups.add("@outside")

        nodes = []
        for group in ranked_groups:
            if group not in selected_groups:
                continue
            stats = groups[group]
            nodes.append({
                "id": group,
                "label": self._network_group_label(group),
                "path": None if group.startswith("@") else group,
                "files": stats["files"],
                "symbols": stats["symbols"],
                "incoming": stats["incoming"],
                "outgoing": stats["outgoing"],
                "internal": stats["internal"],
                "relationships": stats["incoming"] + stats["outgoing"],
                "can_drill": group in drillable,
                "is_file": group in file_groups,
            })

        ranked_edges = sorted(
            (edge for edge in edge_by_key.values()
             if edge["source"] in selected_groups and edge["target"] in selected_groups),
            key=lambda edge: (
                edge["relationships"], edge["weight"], edge["type"], edge["id"],
            ),
            reverse=True,
        )
        edges_truncated = len(ranked_edges) > max_edges
        edges = ranked_edges[:max_edges]
        scope_groups = [
            group for group in groups if group not in {"@outside", "@external"}
        ]

        return {
            "scope": {
                "prefix": clean_prefix,
                "label": "Linux" if not clean_prefix else f"Linux / {clean_prefix}",
                "parent": "/".join(clean_prefix.split("/")[:-1]) if clean_prefix else None,
            },
            "nodes": nodes,
            "edges": edges,
            "totals": {
                "files": sum(groups[group]["files"] for group in scope_groups),
                "symbols": sum(groups[group]["symbols"] for group in scope_groups),
                "relationships": total_relationships,
            },
            "truncated": nodes_truncated or edges_truncated,
            "truncation": {"nodes": nodes_truncated, "edges": edges_truncated},
            "limits": {"max_nodes": max_nodes, "max_edges": max_edges},
            "edge_types": types,
        }

    def get_file_symbols(self, path: str, limit: int = 500,
                         offset: int = 0) -> Optional[dict]:
        """Return bounded indexed definitions for one exact source file.

        Definitions are read from ``symbols.def_file_id`` rather than inferred
        from occurrences, so the response is stable even when a symbol has
        many references in the same file.  This is the compact bridge from a
        directory graph leaf to a cscope-style symbol navigator.
        """
        max_symbols = _bounded_int(limit, 500, 1, 1000)
        start_offset = _bounded_int(offset, 0, 0, _MAX_FILE_SYMBOLS_OFFSET)
        file_row = self.conn.execute(
            """
            SELECT id, path, language, subsystem, sha
            FROM files
            WHERE path = ?
            """,
            (path,),
        ).fetchone()
        if file_row is None:
            return None

        # SCIP emits one synthetic module symbol per document (for example
        # ``<file>/fs/read_write.c``).  It is useful for index bookkeeping but
        # is not a source definition a developer can navigate to, so omit it
        # from both the file-page total and the visible rows.
        total = self.conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE def_file_id = ? AND kind <> ?",
            (file_row["id"], SymbolKind.MODULE),
        ).fetchone()[0]
        rows = self.conn.execute(
            """
            SELECT s.scip_symbol, s.name, s.kind, s.signature,
                   s.def_start_line, s.def_end_line, s.is_external
            FROM symbols s
            WHERE s.def_file_id = ? AND s.kind <> ?
            ORDER BY
                s.def_start_line, s.name, s.scip_symbol
            LIMIT ? OFFSET ?
            """,
            (file_row["id"], SymbolKind.MODULE, max_symbols, start_offset),
        ).fetchall()
        symbols = []
        for row in rows:
            symbol = dict(row)
            symbol["is_external"] = bool(symbol["is_external"])
            symbols.append(symbol)
        return {
            "file": {
                "path": file_row["path"],
                "language": file_row["language"],
                "subsystem": file_row["subsystem"],
                "sha": file_row["sha"],
            },
            "symbols": symbols,
            "totals": {"symbols": total},
            "offset": start_offset,
            "truncated": total > start_offset + len(rows),
            "next_offset": (
                start_offset + len(rows)
                if total > start_offset + len(rows)
                else None
            ),
            "limits": {"max_symbols": max_symbols},
        }

    def call_path(self, src_symbol: str, dst_symbol: str,
                  max_len: int = 10) -> list[dict]:
        """Find shortest call path between two symbols using BFS via CTE."""
        src_id = self._get_symbol_id(src_symbol)
        dst_id = self._get_symbol_id(dst_symbol)
        if src_id is None or dst_id is None:
            return []

        sql = """
            WITH RECURSIVE path(depth, node_id, prev_node_id, edge_type, line) AS (
                SELECT 1, dst_id, src_id, type, line
                FROM edges WHERE src_id=? AND type IN ('calls','ops_bind')
                UNION ALL
                SELECT p.depth + 1, e.dst_id, e.src_id, e.type, e.line
                FROM edges e
                JOIN path p ON e.src_id = p.node_id
                WHERE p.depth < ? AND e.type IN ('calls','ops_bind')
                  AND p.node_id != ?  -- stop if we reached target
            )
            SELECT s.scip_symbol, s.name, s.kind, f.path as file_path, p.line, p.edge_type
            FROM path p
            JOIN symbols s ON p.node_id = s.id
            LEFT JOIN files f ON s.def_file_id = f.id
            WHERE p.node_id = ?
            ORDER BY p.depth
            LIMIT ?
        """
        rows = self.conn.execute(sql, (src_id, max_len, dst_id, dst_id, max_len)).fetchall()
        return [dict(r) for r in rows]

    def get_callchain(self, scip_symbol: str, max_depth: int = 20) -> list[dict]:
        """
        Trace the call chain from a symbol UP to a root (a symbol with no
        callers), following `calls` and `ops_bind` edges (indirect calls
        through ops tables included).

        Walks one caller per level (first by line), with a cycle guard and a
        depth cap. Returns ONE chain (target at depth 0 → root last), each dict:
        {depth, scip_symbol, name, kind, file_path, line}. `line`/`file_path`
        are the call site of the edge into the previous (inner) node.
        """
        sym_id = self._get_symbol_id(scip_symbol)
        if sym_id is None:
            return []

        # depth 0 = the target itself
        target = self.conn.execute(
            "SELECT scip_symbol, name, kind FROM symbols WHERE id = ?", (sym_id,)
        ).fetchone()
        chain = [{
            "depth": 0, "scip_symbol": target["scip_symbol"],
            "name": target["name"], "kind": target["kind"],
            "file_path": None, "line": None,
        }]
        seen = {sym_id}
        cur_id = sym_id

        for depth in range(1, max_depth + 1):
            caller = self.conn.execute(
                """
                SELECT e.src_id, e.line, s.scip_symbol, s.name, s.kind,
                       f.path AS file_path
                FROM edges e
                JOIN symbols s ON e.src_id = s.id
                LEFT JOIN files f ON e.file_id = f.id
                WHERE e.dst_id = ? AND e.type IN ('calls', 'ops_bind')
                ORDER BY e.line
                LIMIT 1
                """,
                (cur_id,),
            ).fetchone()
            if caller is None:
                break  # cur is a root (no callers)
            if caller["src_id"] in seen:
                break  # cycle guard
            chain.append({
                "depth": depth, "scip_symbol": caller["scip_symbol"],
                "name": caller["name"], "kind": caller["kind"],
                "file_path": caller["file_path"], "line": caller["line"],
            })
            cur_id = caller["src_id"]
            seen.add(caller["src_id"])

        return chain

    def find_ops_impls(self, field_name: str,
                       struct_type: Optional[str] = None) -> list[dict]:
        """Find ops_bind implementations for a function-pointer field."""
        # Search by field_name in edge metadata JSON
        sql = """
            SELECT e.src_id, e.dst_id, e.type, e.file_id, e.line,
                   e.confidence, e.metadata,
                   src_s.scip_symbol as ops_symbol, src_s.name as ops_name,
                   dst_s.scip_symbol as impl_symbol, dst_s.name as impl_name,
                   f.path as file_path
            FROM edges e
            JOIN symbols src_s ON e.src_id = src_s.id
            JOIN symbols dst_s ON e.dst_id = dst_s.id
            LEFT JOIN files f ON e.file_id = f.id
            WHERE e.type = 'ops_bind'
              AND e.metadata LIKE ?
        """
        pattern = f'%{field_name}%'
        rows = self.conn.execute(sql, (pattern,)).fetchall()

        if struct_type:
            # Further filter by struct type name
            rows = [r for r in rows if r["ops_name"].startswith(struct_type)
                    or struct_type in r["ops_symbol"]]

        return [dict(r) for r in rows]

    def get_symbol(self, name: str, kind: Optional[str] = None,
                   limit: int = 10) -> list[dict]:
        """
        Exact-name symbol lookup (not fuzzy FTS).

        Agents usually know the exact symbol name (from a crash stack,
        a grep hit, etc.) and want its definition. This is faster and
        more precise than search_symbols (which uses FTS ranking).

        Returns list of dicts with: scip_symbol, name, kind, signature,
        documentation, def_file_path, def_start_line, def_end_line.
        """
        if kind:
            sql = """
                SELECT s.scip_symbol, s.name, s.kind, s.signature, s.documentation,
                       f.path as def_file_path, s.def_start_line, s.def_end_line,
                       s.is_external
                FROM symbols s
                LEFT JOIN files f ON s.def_file_id = f.id
                WHERE s.name = ? AND s.kind = ?
                ORDER BY s.is_external ASC, s.def_start_line ASC
                LIMIT ?
            """
            rows = self.conn.execute(sql, (name, kind, limit)).fetchall()
        else:
            sql = """
                SELECT s.scip_symbol, s.name, s.kind, s.signature, s.documentation,
                       f.path as def_file_path, s.def_start_line, s.def_end_line,
                       s.is_external
                FROM symbols s
                LEFT JOIN files f ON s.def_file_id = f.id
                WHERE s.name = ?
                ORDER BY s.is_external ASC, s.def_start_line ASC
                LIMIT ?
            """
            rows = self.conn.execute(sql, (name, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_definition_location(self, scip_symbol: str) -> Optional[dict]:
        """
        Get a symbol's definition location (file path + line range).

        Used by the MCP server to read the actual source body from disk.
        Returns dict with: scip_symbol, name, kind, def_file_path,
        def_start_line, def_end_line — or None if not found / external.
        """
        row = self.conn.execute(
            """
            SELECT s.scip_symbol, s.name, s.kind, s.signature,
                   f.path as def_file_path, s.def_start_line, s.def_end_line
            FROM symbols s
            LEFT JOIN files f ON s.def_file_id = f.id
            WHERE s.scip_symbol = ?
            """,
            (scip_symbol,),
        ).fetchone()
        return dict(row) if row else None

    def find_type_definition(self, scip_symbol: str) -> list[dict]:
        """
        Find the type definition(s) of a symbol (Go-to-type-definition).

        Follows `type_of` edges: variable/parameter → its type symbol.
        Also returns the type's own definition location.

        Returns list of dicts with: type_symbol, type_name, type_kind,
        def_file_path, def_start_line, signature.
        """
        sym_id = self._get_symbol_id(scip_symbol)
        if sym_id is None:
            return []

        sql = """
            SELECT dst_s.scip_symbol as type_symbol, dst_s.name as type_name,
                   dst_s.kind as type_kind, dst_s.signature,
                   f.path as def_file_path, dst_s.def_start_line, dst_s.def_end_line
            FROM edges e
            JOIN symbols dst_s ON e.dst_id = dst_s.id
            LEFT JOIN files f ON dst_s.def_file_id = f.id
            WHERE e.src_id = ? AND e.type = 'type_of'
        """
        rows = self.conn.execute(sql, (sym_id,)).fetchall()
        return [dict(r) for r in rows]

    def find_references(self, scip_symbol: str, limit: int = 200) -> list[dict]:
        """
        Find all references to a symbol (every occurrence, definition + uses).

        Returns each occurrence with its location and role, plus the
        enclosing function/struct it sits in — so an agent can see
        "who uses this variable/function and where".

        Returns list of dicts with: file_path, start_line, start_col,
        role, is_definition, enclosing_name, enclosing_kind.
        """
        sym_id = self._get_symbol_id(scip_symbol)
        if sym_id is None:
            return []

        sql = """
            SELECT f.path as file_path, o.start_line, o.start_col,
                   o.end_line, o.end_col, o.role,
                   enc.name as enclosing_name, enc.kind as enclosing_kind,
                   enc.scip_symbol as enclosing_symbol
            FROM occurrences o
            JOIN files f ON o.file_id = f.id
            LEFT JOIN symbols enc ON o.enclosing_symbol_id = enc.id
            WHERE o.symbol_id = ?
            ORDER BY f.path, o.start_line
            LIMIT ?
        """
        rows = self.conn.execute(sql, (sym_id, limit)).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["is_definition"] = bool(d["role"] & 0x1)  # SymbolRole.DEFINITION
            results.append(d)
        return results

    def get_struct_layout(self, scip_symbol: str) -> dict:
        """
        Get a struct's fields via `contains` edges.

        Returns dict with: struct_symbol, struct_name, fields (list of
        field dicts with name, kind, signature, line).
        """
        sym_id = self._get_symbol_id(scip_symbol)
        if sym_id is None:
            return {"struct_symbol": scip_symbol, "fields": []}

        struct_row = self.conn.execute(
            "SELECT scip_symbol, name, kind FROM symbols WHERE id = ?", (sym_id,)
        ).fetchone()

        sql = """
            SELECT dst_s.scip_symbol, dst_s.name, dst_s.kind, dst_s.signature,
                   dst_s.def_start_line
            FROM edges e
            JOIN symbols dst_s ON e.dst_id = dst_s.id
            WHERE e.src_id = ? AND e.type = 'contains'
            ORDER BY dst_s.def_start_line
        """
        field_rows = self.conn.execute(sql, (sym_id,)).fetchall()
        return {
            "struct_symbol": struct_row["scip_symbol"] if struct_row else scip_symbol,
            "struct_name": struct_row["name"] if struct_row else "",
            "fields": [dict(r) for r in field_rows],
        }

    def get_metadata(self) -> dict[str, str]:
        """Get all index metadata."""
        rows = self.conn.execute("SELECT key, value FROM meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def get_edge_counts(self) -> dict[str, int]:
        """Count of edges by type (e.g. {'calls': N, 'ops_bind': M, ...})."""
        rows = self.conn.execute(
            "SELECT type, COUNT(*) FROM edges GROUP BY type"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ── Incremental sync (transactional per-file delete + re-insert) ──

    def begin_incremental(self) -> None:
        """Enter incremental mode: suppress write_batch auto-commit. The first
        DML auto-starts a transaction; caller MUST commit/rollback_incremental."""
        self._incremental_mode = True

    def commit_incremental(self) -> None:
        """Commit the incremental transaction and exit incremental mode."""
        self.conn.commit()
        self._incremental_mode = False

    def rollback_incremental(self) -> None:
        """Rollback the incremental transaction and exit incremental mode."""
        self.conn.rollback()
        self._incremental_mode = False

    def delete_file_records(self, file_path: str) -> tuple[Optional[int], list[int]]:
        """Delete a file's occurrences + edges (NOT symbols). NULL out def_file_id
        for symbols formerly defined here so the re-ingest upsert can re-establish
        them. Returns (file_id, formerly_defined_symbol_ids) for scoped GC/contains."""
        fid = self._get_file_id(file_path)
        if fid is None:
            return (None, [])
        formerly_defined = [r[0] for r in self.conn.execute(
            "SELECT id FROM symbols WHERE def_file_id=?", (fid,)).fetchall()]
        self.conn.execute("DELETE FROM occurrences WHERE file_id=?", (fid,))
        self.conn.execute("DELETE FROM edges WHERE file_id=?", (fid,))
        self.conn.execute(
            "UPDATE symbols SET def_file_id=NULL, def_start_line=-1, def_end_line=-1 "
            "WHERE def_file_id=?", (fid,))
        return (fid, formerly_defined)

    def scoped_contains_recovery(self, touched_file_ids: list[int]) -> None:
        """Scoped version of finalize()'s global contains recovery — only for
        structs/fields affected by the touched files. Avoids the O(all fields)
        global pass.

        The contains edge source is the enclosing STRUCT (via
        enclosing_symbol), the dst is the field. We re-derive contains for
        fields whose def is in a touched file (those were just re-ingested and
        their contains edges deleted), by joining field.enclosing_symbol to the
        struct's scip_symbol.
        """
        if not touched_file_ids:
            return
        ph = ",".join("?" * len(touched_file_ids))
        # Delete contains edges whose dst (field) is in a touched file.
        self.conn.execute(
            f"DELETE FROM edges WHERE type='contains' "
            f"AND dst_id IN (SELECT id FROM symbols WHERE def_file_id IN ({ph}))",
            touched_file_ids,
        )
        # Re-derive: for every field in a touched file, join to its enclosing struct.
        self.conn.execute(
            f"INSERT OR IGNORE INTO edges (src_id, dst_id, type, file_id, line, weight, confidence) "
            f"SELECT s.id, f.id, 'contains', f.def_file_id, f.def_start_line, 1, 1.0 "
            f"FROM symbols f JOIN symbols s ON f.enclosing_symbol = s.scip_symbol "
            f"WHERE f.enclosing_symbol != '' AND f.kind = 'field' "
            f"AND f.def_file_id IN ({ph})",
            touched_file_ids,
        )

    def scoped_gc_dangling_edges(self, candidate_symbol_ids: list[int]) -> None:
        """Delete edges pointing at symbols that no longer have a defining
        occurrence (renamed/deleted). Scoped to candidates from delete_file_records."""
        if not candidate_symbol_ids:
            return
        ph = ",".join("?" * len(candidate_symbol_ids))
        self.conn.execute(
            f"DELETE FROM edges WHERE dst_id IN ({ph}) "
            f"AND NOT EXISTS (SELECT 1 FROM occurrences o "
            f"WHERE o.symbol_id = edges.dst_id AND (o.role & 1) = 1)",
            candidate_symbol_ids,
        )

    def close(self) -> None:
        """Close the database connection."""
        self.conn.commit()
        self.conn.close()
        logger.info("Closed SQLite store: %s", self.db_path)
