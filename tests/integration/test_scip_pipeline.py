"""
KGraph — SCIP Pipeline Integration Tests.

Validates the full path: synthetic index.scip → SCIPParser → IngestBatch → SQLiteStore.

Tests are organized in two sections:
  1. Parser output validation (IngestBatch correctness)
  2. Store query validation (SQLiteStore read-side methods)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parser import SCIPParser, EdgeType, SymbolKind
from storage import SQLiteStore


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _first_doc_batch(batches):
    """Find the first batch with a non-empty file path."""
    for b in batches:
        if b.file.path:
            return b
    return None


def _all_doc_batches(batches):
    """Collect all batches with non-empty file paths."""
    return [b for b in batches if b.file.path]


# ──────────────────────────────────────────────
# 1. Parser output validation
# ──────────────────────────────────────────────

class TestParserOutput:
    """Verify SCIPParser produces correct IngestBatch objects."""

    @pytest.fixture(autouse=True)
    def setup(self, scip_file: Path):
        self.parser = SCIPParser(scip_file)
        self.batches = list(self.parser.parse())

    def test_yields_at_least_3_batches(self):
        # metadata + 3 documents + external symbols = ≥5
        assert len(self.batches) >= 5

    def test_metadata_batch(self):
        meta_batch = self.batches[0]
        assert len(meta_batch.metadata) > 0
        keys = {m.key for m in meta_batch.metadata}
        assert "project_root" in keys
        assert "tool_name" in keys

    def test_three_document_batches(self):
        doc_batches = _all_doc_batches(self.batches)
        assert len(doc_batches) == 3
        paths = {b.file.path for b in doc_batches}
        assert "fs/ext4/file.c" in paths
        assert "fs/read_write.c" in paths
        assert "include/linux/fs.h" in paths

    def test_ext4_file_c_symbols(self):
        doc = _first_doc_batch(self.batches)
        assert doc.file.path == "fs/ext4/file.c"
        sym_names = {s.name for s in doc.symbols}
        assert "ext4_file_operations" in sym_names
        assert "ext4_file_read_iter" in sym_names
        assert "ext4_file_write_iter" in sym_names
        assert "ext4_file_open" in sym_names
        assert "read_iter" in sym_names
        assert "write_iter" in sym_names

    def test_symbol_kinds(self):
        doc = _first_doc_batch(self.batches)
        kind_map = {s.name: s.kind for s in doc.symbols}
        assert kind_map["ext4_file_operations"] == SymbolKind.STRUCT
        assert kind_map["ext4_file_read_iter"] == SymbolKind.FUNCTION
        assert kind_map["ext4_file_write_iter"] == SymbolKind.FUNCTION
        assert kind_map["ext4_file_open"] == SymbolKind.FUNCTION
        assert kind_map["read_iter"] == SymbolKind.FIELD

    def test_ext4_file_c_occurrences(self):
        doc = _first_doc_batch(self.batches)
        # At least: 4 definitions (ops + 3 functions) + 3 ops_bind refs
        assert len(doc.occurrences) >= 7

    def test_call_edges(self):
        """vfs_read → ext4_file_read_iter should produce a calls edge."""
        doc_batches = _all_doc_batches(self.batches)
        rw_batch = next(b for b in doc_batches if b.file.path == "fs/read_write.c")
        call_edges = [e for e in rw_batch.edges if e.type == EdgeType.CALLS]
        targets = {e.dst_symbol for e in call_edges}
        assert any("ext4_file_read_iter" in t for t in targets)
        assert any("ext4_file_write_iter" in t for t in targets)

    def test_ops_bind_edges(self):
        """ext4_file_operations body references to functions → ops_bind edges."""
        doc = _first_doc_batch(self.batches)
        ops_edges = [e for e in doc.edges if e.type == EdgeType.OPS_BIND]
        assert len(ops_edges) >= 3  # read_iter, write_iter, open

        # Verify specific binding: ext4_file_operations → ext4_file_read_iter
        targets = {e.dst_symbol for e in ops_edges}
        assert any("ext4_file_read_iter" in t for t in targets)

    def test_ops_bind_confidence(self):
        """ops_bind edges should have confidence=0.5 (heuristic)."""
        doc = _first_doc_batch(self.batches)
        ops_edges = [e for e in doc.edges if e.type == EdgeType.OPS_BIND]
        for edge in ops_edges:
            assert edge.confidence == 0.5

    def test_ops_bind_has_metadata(self):
        """ops_bind edges should carry field_name in metadata JSON."""
        import json
        doc = _first_doc_batch(self.batches)
        ops_edges = [e for e in doc.edges if e.type == EdgeType.OPS_BIND]
        for edge in ops_edges:
            if edge.metadata:
                meta = json.loads(edge.metadata)
                assert "field_name" in meta

    def test_implements_edge(self):
        """ext4_file_operations should have an implements relationship to file_operations."""
        doc = _first_doc_batch(self.batches)
        impl_edges = [e for e in doc.edges if e.type == EdgeType.IMPLEMENTS]
        assert len(impl_edges) >= 1
        assert any("file_operations" in e.dst_symbol for e in impl_edges)

    def test_type_of_edge(self):
        """read_iter field should have a type_of relationship to loff_t."""
        doc = _first_doc_batch(self.batches)
        type_edges = [e for e in doc.edges if e.type == EdgeType.TYPE_OF]
        assert len(type_edges) >= 1

    def test_external_symbols_batch(self):
        """External symbols (sys_read, __fdget_pos) should be in the last batch."""
        ext_batch = self.batches[-1]
        ext_names = {s.name for s in ext_batch.symbols}
        assert "sys_read" in ext_names
        assert "__fdget_pos" in ext_names
        # All external symbols should be marked as such
        for sym in ext_batch.symbols:
            assert sym.is_external is True


# ──────────────────────────────────────────────
# 2. Store query validation
# ──────────────────────────────────────────────

class TestStoreQueries:
    """Verify SQLiteStore read-side methods against populated synthetic data."""

    @pytest.fixture(autouse=True)
    def setup(self, populated_store: SQLiteStore):
        self.store = populated_store

    # ── search_symbols ──

    def test_search_exact_name(self):
        results = self.store.search_symbols("ext4_file_read_iter")
        assert len(results) >= 1
        assert results[0]["name"] == "ext4_file_read_iter"

    def test_search_partial_name(self):
        """FTS5 should match partial names."""
        results = self.store.search_symbols("ext4_file")
        assert len(results) >= 1
        names = {r["name"] for r in results}
        assert "ext4_file_read_iter" in names

    def test_search_with_kind_filter(self):
        results = self.store.search_symbols("ext4_file", kind="function")
        for r in results:
            assert r["kind"] == "function"

    def test_search_no_results(self):
        results = self.store.search_symbols("xyz_does_not_exist")
        assert len(results) == 0

    # ── get_symbol ──

    def test_get_symbol_exact(self):
        results = self.store.get_symbol("vfs_read")
        assert len(results) >= 1
        assert results[0]["name"] == "vfs_read"
        assert results[0]["kind"] == "function"

    def test_get_symbol_with_kind(self):
        results = self.store.get_symbol("ext4_file_operations", kind="struct")
        assert len(results) >= 1
        assert results[0]["kind"] == "struct"

    def test_get_symbol_not_found(self):
        results = self.store.get_symbol("does_not_exist")
        assert len(results) == 0

    # ── find_callers ──

    def test_find_callers_direct(self):
        """vfs_read is a direct caller of ext4_file_read_iter."""
        callers = self.store.find_callers(
            "scip clang c linux v6.12 ext4_file_read_iter()."
        )
        caller_names = {c["name"] for c in callers}
        assert "vfs_read" in caller_names

    def test_find_callers_depth_2(self):
        """Depth=2 should still find direct callers."""
        callers = self.store.find_callers(
            "scip clang c linux v6.12 ext4_file_read_iter().",
            depth=2,
        )
        assert len(callers) >= 1

    def test_find_callers_unknown_symbol(self):
        callers = self.store.find_callers(
            "scip clang c linux v6.12 nonexistent_func()."
        )
        assert len(callers) == 0

    # ── find_callees ──

    def test_find_callees_direct(self):
        """vfs_read calls ext4_file_read_iter."""
        callees = self.store.find_callees(
            "scip clang c linux v6.12 vfs_read()."
        )
        callee_names = {c["name"] for c in callees}
        assert "ext4_file_read_iter" in callee_names

    def test_find_callees_no_calls(self):
        """ext4_file_read_iter likely has no callees in our synthetic data."""
        callees = self.store.find_callees(
            "scip clang c linux v6.12 ext4_file_read_iter()."
        )
        # May have ops_bind as callee edge, but no direct calls
        direct_calls = [c for c in callees if c.get("edge_type") == "calls"]
        assert len(direct_calls) == 0

    # ── find_ops_impls ──

    def test_find_ops_impls_read_iter(self):
        results = self.store.find_ops_impls("read_iter")
        assert len(results) >= 1
        # Should find ext4_file_operations → ext4_file_read_iter
        impl_names = {r["impl_name"] for r in results}
        assert "ext4_file_read_iter" in impl_names
        ops_names = {r["ops_name"] for r in results}
        assert "ext4_file_operations" in ops_names

    def test_find_ops_impls_with_struct_filter(self):
        results = self.store.find_ops_impls("read_iter", struct_type="ext4_file_operations")
        assert len(results) >= 1
        for r in results:
            assert "ext4_file_operations" in r["ops_name"]

    def test_find_ops_impls_no_results(self):
        results = self.store.find_ops_impls("nonexistent_field")
        assert len(results) == 0

    # ── find_references ──

    def test_find_references_includes_definition(self):
        refs = self.store.find_references(
            "scip clang c linux v6.12 ext4_file_read_iter()."
        )
        assert len(refs) >= 2  # at least definition + reference
        has_def = any(r["is_definition"] for r in refs)
        has_ref = any(not r["is_definition"] for r in refs)
        assert has_def, "Should include at least one definition occurrence"
        assert has_ref, "Should include at least one reference occurrence"

    def test_find_references_has_location(self):
        refs = self.store.find_references(
            "scip clang c linux v6.12 ext4_file_read_iter()."
        )
        for r in refs:
            assert r["file_path"]
            assert r["start_line"] >= 0

    # ── find_type_definition ──

    def test_find_type_definition(self):
        """read_iter field should resolve to its type via type_of."""
        results = self.store.find_type_definition(
            "scip clang c linux v6.12 ext4_file_operations#read_iter."
        )
        assert len(results) >= 1
        type_names = {r["type_name"] for r in results}
        # Type target is ext4_file_read_iter (same-document symbol)
        assert "ext4_file_read_iter" in type_names

    # ── get_struct_layout ──

    def test_get_struct_layout(self):
        """contains edges should be derived from enclosing_symbol."""
        layout = self.store.get_struct_layout(
            "scip clang c linux v6.12 ext4_file_operations#"
        )
        assert layout["struct_name"] == "ext4_file_operations"
        field_names = [f["name"] for f in layout["fields"]]
        assert "read_iter" in field_names
        assert "write_iter" in field_names
        assert "open" in field_names

    def test_get_struct_layout_unknown(self):
        layout = self.store.get_struct_layout(
            "scip clang c linux v6.12 nonexistent_struct#"
        )
        assert layout["fields"] == []

    # ── get_callchain ──

    def test_get_callchain(self):
        """Call chain walks callers (calls + ops_bind) up to a root."""
        chain = self.store.get_callchain(
            "scip clang c linux v6.12 ext4_file_read_iter().", max_depth=10
        )
        # depth 0 = the target itself
        assert chain[0]["depth"] == 0
        assert chain[0]["name"] == "ext4_file_read_iter"
        # it has at least one caller (vfs_read via calls, or ext4_file_operations via ops_bind)
        assert len(chain) >= 2
        names = {n["name"] for n in chain}
        assert names & {"vfs_read", "ext4_file_operations"}

    def test_get_callchain_root(self):
        """A symbol with no callers is its own (1-level) chain."""
        chain = self.store.get_callchain(
            "scip clang c linux v6.12 vfs_read().", max_depth=10
        )
        assert len(chain) == 1
        assert chain[0]["name"] == "vfs_read"

    def test_get_callchain_unknown(self):
        chain = self.store.get_callchain(
            "scip clang c linux v6.12 nonexistent().", max_depth=10
        )
        assert chain == []

    # ── get_neighborhood ──

    def test_get_neighborhood_depth_1(self):
        nb = self.store.get_neighborhood(
            "scip clang c linux v6.12 vfs_read().",
            depth=1,
        )
        assert len(nb["nodes"]) >= 1
        node_names = {n["name"] for n in nb["nodes"]}
        assert "ext4_file_read_iter" in node_names

    def test_get_neighborhood_summary_mode(self):
        nb = self.store.get_neighborhood(
            "scip clang c linux v6.12 vfs_read().",
            depth=1,
            summary=True,
        )
        for node in nb["nodes"]:
            assert "name" in node
            assert "kind" in node

    def test_get_neighborhood_unknown_symbol(self):
        nb = self.store.get_neighborhood(
            "scip clang c linux v6.12 nonexistent_func()."
        )
        assert nb["nodes"] == []

    # ── call_path ──

    def test_call_path_exists(self):
        path = self.store.call_path(
            "scip clang c linux v6.12 vfs_read().",
            "scip clang c linux v6.12 ext4_file_read_iter().",
        )
        assert len(path) >= 1
        names = [p["name"] for p in path]
        assert "ext4_file_read_iter" in names

    def test_call_path_not_found(self):
        path = self.store.call_path(
            "scip clang c linux v6.12 ext4_file_read_iter().",
            "scip clang c linux v6.12 sys_read().",
            max_len=3,
        )
        assert len(path) == 0

    # ── metadata ──

    def test_get_metadata(self):
        meta = self.store.get_metadata()
        assert "project_root" in meta
        assert meta["project_root"] == "/kernel"
        assert "tool_name" in meta
        assert meta["tool_name"] == "scip-clang"
        assert "total_symbols" in meta
        assert "total_files" in meta

    # ── get_definition_location ──

    def test_get_definition_location(self):
        loc = self.store.get_definition_location(
            "scip clang c linux v6.12 vfs_read()."
        )
        assert loc is not None
        assert loc["name"] == "vfs_read"
        assert loc["def_file_path"] == "fs/read_write.c"
        assert loc["def_start_line"] >= 0

    def test_get_definition_location_external(self):
        """External symbols have no definition location."""
        loc = self.store.get_definition_location(
            "scip clang c linux v6.12 sys_read()."
        )
        assert loc is not None
        assert loc["name"] == "sys_read"
