"""
KGraph — GraphStore Interface

Abstract base class for graph storage backends.
The parser emits IngestBatch objects; any GraphStore implementation
can consume them. This is the **extension point** for future
storage engines (Neo4j, custom embedded DB, etc.).

Current implementation: SQLiteStore (see sqlite_store.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from parser.models import IngestBatch


class GraphStore(ABC):
    """
    Abstract interface for persisting and querying a code knowledge graph.

    Write side (ingestion):
        - create_schema():   initialize tables/collections
        - write_batch():     persist one IngestBatch
        - finalize():        flush buffers, commit transactions, build indexes

    Read side (querying):
        - search_symbols():  find symbols by name/pattern
        - find_callers():    reverse call graph
        - find_callees():    forward call graph
        - get_neighborhood(): N-hop subgraph
        - call_path():       path between two symbols
        - find_ops_impls():  function-pointer field implementations

    The write side is used during `kgraph init` (ingestion pipeline).
    The read side is used by the MCP server (query engine).
    """

    # ── Write side (ingestion) ──

    @abstractmethod
    def create_schema(self) -> None:
        """Initialize the storage schema (tables, indexes, constraints)."""
        ...

    @abstractmethod
    def write_batch(self, batch: IngestBatch) -> None:
        """Persist one IngestBatch (one Document's worth of data)."""
        ...

    @abstractmethod
    def finalize(self) -> None:
        """
        Finalize ingestion: flush buffers, commit pending transactions,
        build secondary indexes, write metadata.
        """
        ...

    # ── Read side (querying) ──

    @abstractmethod
    def search_symbols(self, query: str, kind: Optional[str] = None,
                       limit: int = 50) -> list[dict]:
        """
        Search symbols by name (exact, prefix, or pattern).

        Returns list of dicts with: scip_symbol, name, kind, signature,
        def_file_path, def_start_line.
        """
        ...

    @abstractmethod
    def find_callers(self, scip_symbol: str, depth: int = 1,
                     limit: int = 100) -> list[dict]:
        """
        Find symbols that call the given symbol (reverse call graph).

        Returns list of dicts with: src_symbol, src_name, src_kind,
        file_path, line, edge_type.
        """
        ...

    @abstractmethod
    def find_callees(self, scip_symbol: str, depth: int = 1,
                     limit: int = 100) -> list[dict]:
        """
        Find symbols called by the given symbol (forward call graph).

        Returns list of dicts with: dst_symbol, dst_name, dst_kind,
        file_path, line, edge_type.
        """
        ...

    @abstractmethod
    def get_neighborhood(self, scip_symbol: str, depth: int = 1,
                         edge_types: Optional[list[str]] = None,
                         summary: bool = False) -> dict:
        """
        Get N-hop neighborhood around a symbol.

        Returns dict with: center_symbol, nodes (list), edges (list).
        If summary=True, nodes contain only name + file:line (compact).
        """
        ...

    @abstractmethod
    def call_path(self, src_symbol: str, dst_symbol: str,
                  max_len: int = 10) -> list[dict]:
        """
        Find a call path between two symbols.

        Returns list of dicts representing the path:
        each dict has: symbol, name, kind, file_path, line.
        """
        ...

    @abstractmethod
    def find_ops_impls(self, field_name: str,
                       struct_type: Optional[str] = None) -> list[dict]:
        """
        Find all implementations bound to a function-pointer field
        via ops_bind edges.

        Returns list of dicts with: impl_symbol, impl_name,
        ops_symbol, ops_name, file_path, line, field_name, confidence.
        """
        ...

    @abstractmethod
    def get_metadata(self) -> dict[str, str]:
        """Get all index metadata as a dict."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the store (release connections, cleanup)."""
        ...