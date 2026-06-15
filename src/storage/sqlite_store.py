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

        # Ingestion caches: scip_symbol → rowid
        self._symbol_id_cache: dict[str, int] = {}
        self._file_id_cache: dict[str, int] = {}
        self._batch_counter = 0

    # ── Schema creation ──

    def create_schema(self) -> None:
        """Create all tables, indexes, and FTS triggers."""
        logger.info("Creating schema in %s", self.db_path)
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.executescript(_FTS_TRIGGERS)
        self.conn.commit()

    # ── Batch write (ingestion) ──

    def write_batch(self, batch: IngestBatch) -> None:
        """Persist one IngestBatch."""
        try:
            # 1. Write file
            file_id = self._write_file(batch.file)

            # 2. Write symbols
            self._write_symbols(batch.symbols, file_id)

            # 3. Write occurrences
            self._write_occurrences(batch.occurrences, file_id)

            # 4. Write edges
            self._write_edges(batch.edges, file_id)

            # 5. Write metadata
            self._write_metadata(batch.metadata)

            # Periodic commit for batch performance
            self._batch_counter += 1
            if self._batch_counter % 10 == 0:  # Commit every 10 documents
                self.conn.commit()
                logger.debug("Committed after %d batches", self._batch_counter)

        except sqlite3.Error as e:
            logger.error("SQLite error writing batch for %s: %s",
                         batch.file.path, e)
            raise

    def finalize(self) -> None:
        """Final commit and index optimization."""
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
        """Insert symbol records."""
        for sym in symbols:
            if sym.scip_symbol in self._symbol_id_cache:
                # Update existing symbol's definition location
                self.conn.execute(
                    "UPDATE symbols SET def_file_id=?, def_start_line=?, def_end_line=? "
                    "WHERE scip_symbol=? AND def_file_id IS NULL",
                    (file_id, sym.def_start_line, sym.def_end_line, sym.scip_symbol),
                )
                continue

            self.conn.execute(_INSERT_SYMBOL, (
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
            ))

            rowid = self._get_symbol_id(sym.scip_symbol)
            self._symbol_id_cache[sym.scip_symbol] = rowid

    def _write_occurrences(self, occurrences: list[OccurrenceRecord],
                           file_id: int) -> None:
        """Insert occurrence records."""
        for occ in occurrences:
            symbol_id = self._get_symbol_id(occ.symbol)
            if symbol_id is None:
                continue  # Skip occurrences with unknown symbols

            enclosing_id = None
            if occ.enclosing_symbol:
                enclosing_id = self._get_symbol_id(occ.enclosing_symbol)

            self.conn.execute(_INSERT_OCCURRENCE, (
                symbol_id, file_id,
                occ.start_line, occ.start_col,
                occ.end_line, occ.end_col,
                occ.role, enclosing_id,
            ))

    def _write_edges(self, edges: list[EdgeRecord], file_id: int) -> None:
        """Insert edge records."""
        for edge in edges:
            src_id = self._get_symbol_id(edge.src_symbol)
            dst_id = self._get_symbol_id(edge.dst_symbol)
            if src_id is None or dst_id is None:
                continue  # Skip edges with unknown symbols

            edge_file_id = self._get_file_id(edge.file_path) if edge.file_path else None

            self.conn.execute(_INSERT_EDGE, (
                src_id, dst_id, edge.type,
                edge_file_id, edge.line,
                edge.weight, edge.confidence,
                edge.metadata,
            ))

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
                         summary: bool = False) -> dict:
        """Get N-hop neighborhood around a symbol."""
        sym_id = self._get_symbol_id(scip_symbol)
        if sym_id is None:
            return {"center_symbol": scip_symbol, "nodes": [], "edges": []}

        types = edge_types or ["calls", "references", "ops_bind", "implements", "type_of", "contains"]
        types_str = ",".join(f"'{t}'" for t in types)

        # Collect all nodes and edges in N-hop range using recursive CTE
        sql = f"""
            WITH RECURSIVE neighbors(depth, node_id, edge_src, edge_dst,
                                     edge_type, edge_file_id, edge_line) AS (
                -- Initial: edges from/to center symbol
                SELECT 1, CASE WHEN e.src_id=? THEN e.dst_id ELSE e.src_id END,
                       e.src_id, e.dst_id, e.type, e.file_id, e.line
                FROM edges e
                WHERE (e.src_id=? OR e.dst_id=?) AND e.type IN ({types_str})

                UNION ALL
                -- Expand: edges from/to discovered nodes
                SELECT n.depth + 1,
                       CASE WHEN e.src_id=n.node_id THEN e.dst_id ELSE e.src_id END,
                       e.src_id, e.dst_id, e.type, e.file_id, e.line
                FROM edges e
                JOIN neighbors n ON (e.src_id=n.node_id OR e.dst_id=n.node_id)
                WHERE n.depth < ? AND e.type IN ({types_str})
            )
            SELECT DISTINCT s.scip_symbol, s.name, s.kind,
                   f.path as def_file_path, s.def_start_line
            FROM symbols s
            JOIN (SELECT DISTINCT node_id FROM neighbors) n ON s.id = n.node_id
            LEFT JOIN files f ON s.def_file_id = f.id
        """
        node_rows = self.conn.execute(sql, (sym_id, sym_id, sym_id, depth)).fetchall()

        if summary:
            nodes = [{"name": r["name"], "kind": r["kind"],
                      "file": r["def_file_path"], "line": r["def_start_line"]}
                     for r in node_rows]
        else:
            nodes = [dict(r) for r in node_rows]

        # Also include center node
        center_row = self.conn.execute(
            "SELECT s.scip_symbol, s.name, s.kind, f.path, s.def_start_line "
            "FROM symbols s LEFT JOIN files f ON s.def_file_id=f.id WHERE s.id=?",
            (sym_id,),
        ).fetchone()

        return {
            "center_symbol": scip_symbol,
            "center": dict(center_row) if center_row else {},
            "nodes": nodes,
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

    def close(self) -> None:
        """Close the database connection."""
        self.conn.commit()
        self.conn.close()
        logger.info("Closed SQLite store: %s", self.db_path)