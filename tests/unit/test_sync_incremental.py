"""
Incremental sync tests — validate the per-file delete + re-insert merge (P5).

Uses the scip_pb2.Index builder pattern from conftest.py / test_get_struct_layout.py.

Tests:
  1. delete_file_records: occurrences + edges gone, symbols remain, def_file_id NULLed.
  2. incremental_ingest equivalence: incremental result == full-rebuild result.
  3. scoped_contains_recovery: struct/field in touched file → contains edge.
  4. scoped_gc_dangling_edges: renamed-away symbol → dangling edge removed.
  5. transactional_rollback: error mid-ingest → DB unchanged.
"""
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SRC = _HERE.parents[2] / "src"
_SCRIPTS = _HERE.parents[2] / "scripts"
for p in (_SRC, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import scip_pb2
from parser import SCIPParser
from storage import SQLiteStore


# ── builders shaped like real scip-clang output ──

def _doc(index, path="fs/x.c"):
    index.metadata.project_root = "/kernel"
    d = index.documents.add()
    d.relative_path = path
    d.language = "C"
    return d


def _func_def(doc, symbol, name, line):
    """A function SymbolInformation + its Definition occurrence."""
    s = doc.symbols.add()
    s.symbol = symbol
    s.display_name = name
    s.kind = 17  # Function
    o = doc.occurrences.add()
    o.symbol = symbol
    o.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    o.single_line_range.line = line
    o.single_line_range.start_character = 0
    o.single_line_range.end_character = len(name)
    o.multi_line_enclosing_range.start_line = line
    o.multi_line_enclosing_range.start_character = 0
    o.multi_line_enclosing_range.end_line = line + 5
    o.multi_line_enclosing_range.end_character = 1


def _call_ref(doc, caller_symbol, callee_symbol, line):
    """A reference occurrence of callee inside caller (a 'calls' edge)."""
    o = doc.occurrences.add()
    o.symbol = callee_symbol
    o.single_line_range.line = line
    o.single_line_range.start_character = 4
    o.single_line_range.end_character = 4
    # enclosing for the reference: the caller
    o.multi_line_enclosing_range.start_line = line - 1
    o.multi_line_enclosing_range.start_character = 0
    o.multi_line_enclosing_range.end_line = line + 1
    o.multi_line_enclosing_range.end_character = 1
    # symbol_map for enclosing: the caller must be in doc.symbols
    s = doc.symbols.add()
    s.symbol = caller_symbol
    s.display_name = caller_symbol.split("$")[1].rstrip("().") if "$" in caller_symbol else caller_symbol
    s.kind = 17


def _populate(index, tmp_path):
    scip_path = tmp_path / "index.scip"
    scip_path.write_bytes(index.SerializeToString())
    db_path = tmp_path / "kgraph.db"
    store = SQLiteStore(db_path)
    store.create_schema()
    for batch in SCIPParser(scip_path).parse():
        store.write_batch(batch)
    store.finalize()
    return store


class TestDeleteFileRecords:
    def test_deletes_occurrences_edges_keeps_symbols(self, tmp_path):
        idx = scip_pb2.Index()
        doc = _doc(idx, "fs/x.c")
        _func_def(doc, "cxx . . $ foo().", "foo", 10)
        _func_def(doc, "cxx . . $ bar().", "bar", 20)
        _call_ref(doc, "cxx . . $ bar().", "cxx . . $ foo().", 21)
        store = _populate(idx, tmp_path)

        store.begin_incremental()
        store.delete_file_records("fs/x.c")
        store.commit_incremental()

        # symbols remain
        assert store.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0] == 2
        # occurrences + edges gone
        assert store.conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
        # def_file_id NULLed
        assert store.conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE def_file_id IS NOT NULL"
        ).fetchone()[0] == 0
        store.close()


class TestIncrementalIngestEquivalence:
    def test_incremental_matches_full_rebuild(self, tmp_path):
        # "before": foo defined in fs/x.c at line 10
        before = scip_pb2.Index()
        doc = _doc(before, "fs/x.c")
        _func_def(doc, "cxx . . $ foo().", "foo", 10)
        store_before = _populate(before, tmp_path)
        # baseline: simulate a pre-existing DB for incremental
        store_before.conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('index_timestamp','1')"
        )
        store_before.conn.commit()
        store_before.close()

        # "after": same file, foo moved to line 100, new func baz added
        after = scip_pb2.Index()
        doc2 = _doc(after, "fs/x.c")
        _func_def(doc2, "cxx . . $ foo().", "foo", 100)  # moved
        _func_def(doc2, "cxx . . $ baz().", "baz", 200)  # new
        after_scip = tmp_path / "partial.scip"
        after_scip.write_bytes(after.SerializeToString())

        # (A) incremental: open existing DB, delete file records, re-insert
        store_inc = SQLiteStore(tmp_path / "kgraph.db")
        from sync.incremental import ingest_incremental
        ingest_incremental(store_inc, after_scip)
        inc_loc = store_inc.get_definition_location("cxx . . $ foo().")
        inc_baz = store_inc.get_definition_location("cxx . . $ baz().")
        store_inc.close()

        # (B) full rebuild from scratch
        full_db = tmp_path / "full" / "kgraph.db"
        full_db.parent.mkdir()
        store_full = SQLiteStore(full_db)
        store_full.create_schema()
        for batch in SCIPParser(after_scip).parse():
            store_full.write_batch(batch)
        store_full.finalize()
        full_loc = store_full.get_definition_location("cxx . . $ foo().")
        full_baz = store_full.get_definition_location("cxx . . $ baz().")
        store_full.close()

        # incremental result == full rebuild result (compare against full, no
        # hardcoded line: SCIP line numbers are stored as-is in def_start_line)
        assert inc_loc is not None and full_loc is not None
        assert inc_loc["def_start_line"] == full_loc["def_start_line"]
        assert inc_baz is not None and full_baz is not None
        assert inc_baz["def_start_line"] == full_baz["def_start_line"]


class TestScopedContainsRecovery:
    def test_struct_field_contains_recovered(self, tmp_path):
        # struct S defined in fs/x.c; field S.x defined in fs/y.c (cross-doc).
        # Ingest_incremental deletes fs/y.c, re-inserts the field batch, then
        # scoped_contains_recovery re-derives the S→x contains edge.
        from sync.incremental import ingest_incremental

        # baseline index: struct in x.c, field in y.c
        before = scip_pb2.Index()
        before.metadata.project_root = "/kernel"
        doc_s = _doc(before, "fs/x.c")
        s = doc_s.symbols.add()
        s.symbol = "cxx . . $ S#"
        s.display_name = "S"
        s.kind = 49
        o = doc_s.occurrences.add()
        o.symbol = "cxx . . $ S#"
        o.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
        o.single_line_range.line = 10
        o.multi_line_enclosing_range.start_line = 10
        o.multi_line_enclosing_range.start_character = 0
        o.multi_line_enclosing_range.end_line = 20
        o.multi_line_enclosing_range.end_character = 1
        doc_f = _doc(before, "fs/y.c")
        fld = doc_f.symbols.add()
        fld.symbol = "cxx . . $ S#x."
        fld.display_name = "x"
        fld.kind = 15
        fld.enclosing_symbol = "cxx . . $ S#"
        fo = doc_f.occurrences.add()
        fo.symbol = "cxx . . $ S#x."
        fo.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
        fo.single_line_range.line = 5
        fo.single_line_range.start_character = 4
        fo.single_line_range.end_character = 5
        store = _populate(before, tmp_path)
        assert [f["name"] for f in store.get_struct_layout("cxx . . $ S#")["fields"]] == ["x"]
        # set a baseline so the field's file is "touched"
        store.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('index_timestamp','1')")
        store.conn.commit()
        store.close()

        # partial: same field re-emitted (fs/y.c rebuilt) → ingest_incremental
        partial = scip_pb2.Index()
        partial.metadata.project_root = "/kernel"
        doc_p = _doc(partial, "fs/y.c")
        fld2 = doc_p.symbols.add()
        fld2.symbol = "cxx . . $ S#x."
        fld2.display_name = "x"
        fld2.kind = 15
        fld2.enclosing_symbol = "cxx . . $ S#"
        fo2 = doc_p.occurrences.add()
        fo2.symbol = "cxx . . $ S#x."
        fo2.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
        fo2.single_line_range.line = 7  # moved
        fo2.single_line_range.start_character = 4
        fo2.single_line_range.end_character = 5
        partial_scip = tmp_path / "partial.scip"
        partial_scip.write_bytes(partial.SerializeToString())

        store2 = SQLiteStore(tmp_path / "kgraph.db")
        ingest_incremental(store2, partial_scip)
        layout = store2.get_struct_layout("cxx . . $ S#")
        assert [f["name"] for f in layout["fields"]] == ["x"]
        store2.close()


class TestScopedGC:
    def test_dangling_edge_removed(self, tmp_path):
        # foo defined in fs/x.c; bar calls foo (edge in fs/y.c, dst=foo)
        idx = scip_pb2.Index()
        doc_x = _doc(idx, "fs/x.c")
        _func_def(doc_x, "cxx . . $ foo().", "foo", 10)

        doc_y = _doc(idx, "fs/y.c")
        _func_def(doc_y, "cxx . . $ bar().", "bar", 5)
        _call_ref(doc_y, "cxx . . $ bar().", "cxx . . $ foo().", 6)
        store = _populate(idx, tmp_path)

        # before: there's a calls edge bar → foo (foo has a def occurrence)
        callers = store.find_callers("cxx . . $ foo().")
        assert len(callers) >= 1

        # now: delete fs/x.c (foo's def gone) → foo has no def occurrence
        store.begin_incremental()
        fid, formerly = store.delete_file_records("fs/x.c")
        # foo's id is in formerly_defined; scoped GC removes the dangling edge
        store.scoped_gc_dangling_edges(formerly)
        store.commit_incremental()

        # the calls edge bar → foo should be gone (foo has no def)
        callers_after = store.find_callers("cxx . . $ foo().")
        assert callers_after == []
        store.close()


class TestTransactionalRollback:
    def test_rollback_leaves_db_unchanged(self, tmp_path):
        idx = scip_pb2.Index()
        doc = _doc(idx, "fs/x.c")
        _func_def(doc, "cxx . . $ foo().", "foo", 10)
        store = _populate(idx, tmp_path)

        before_count = store.conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]

        store.begin_incremental()
        store.delete_file_records("fs/x.c")
        # inject an error before commit
        try:
            raise RuntimeError("simulated mid-ingest failure")
        except RuntimeError:
            store.rollback_incremental()

        after_count = store.conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
        assert before_count == after_count  # unchanged
        assert store._incremental_mode is False
        store.close()
