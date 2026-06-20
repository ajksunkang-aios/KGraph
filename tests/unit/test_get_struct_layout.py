"""
Targeted stress tests for get_struct_layout against REAL scip-clang shape.

Real scip-clang leaves SymbolInformation.kind unset and enclosing_symbol
empty for non-local symbols (incl. struct fields), so containment must be
recovered positionally by the parser (Step 7). The existing
test_real_scheme_pipeline only covers a single ideal struct; these tests
stress the harder cases that break in practice:

  - multiple structs in one file (field crosstalk)
  - field ordering
  - empty / nonexistent structs
  - struct enclosing_range that does NOT cover the field lines (the
    real-world failure mode when scip-clang's range is inaccurate)
"""

from pathlib import Path

import scip_pb2
from parser import SCIPParser
from storage import SQLiteStore


# ── builders shaped like real scip-clang output (kind unset, no enclosing_symbol) ──

def _doc(index: scip_pb2.Index, path: str = "fs/x.c"):
    index.metadata.project_root = "/kernel"
    d = index.documents.add()
    d.relative_path = path
    d.language = "C"
    return d


def _struct(symbol: str, name: str, doc, def_line: int,
            body_start: int, body_end: int, col: int = 30) -> None:
    """A struct SymbolInformation + its Definition occurrence whose
    multi_line_enclosing_range spans [body_start, body_end]."""
    s = doc.symbols.add()
    s.symbol = symbol
    s.display_name = name
    o = doc.occurrences.add()
    o.symbol = symbol
    o.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    o.single_line_range.line = def_line
    o.single_line_range.start_character = col
    o.single_line_range.end_character = col + len(name)
    o.multi_line_enclosing_range.start_line = body_start
    o.multi_line_enclosing_range.start_character = 0
    o.multi_line_enclosing_range.end_line = body_end
    o.multi_line_enclosing_range.end_character = 1


def _field(struct_symbol: str, name: str, doc, line: int, col: int = 4) -> None:
    """A field SymbolInformation (Term '.') + its Definition occurrence at `line`."""
    f = doc.symbols.add()
    f.symbol = f"{struct_symbol}{name}."
    f.display_name = name
    fo = doc.occurrences.add()
    fo.symbol = f.symbol
    fo.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    fo.single_line_range.line = line
    fo.single_line_range.start_character = col
    fo.single_line_range.end_character = col + len(name)


def _populate(index: scip_pb2.Index, tmp_path: Path) -> SQLiteStore:
    scip_path = tmp_path / "index.scip"
    scip_path.write_bytes(index.SerializeToString())
    db_path = tmp_path / "kgraph.db"
    store = SQLiteStore(db_path)
    store.create_schema()
    for batch in SCIPParser(scip_path).parse():
        store.write_batch(batch)
    store.finalize()
    return store


class TestGetStructLayoutRealShape:

    def test_single_struct_fields_ordered(self, tmp_path):
        """One struct, 3 fields inserted out of order → returned ordered by line."""
        idx = scip_pb2.Index()
        doc = _doc(idx)
        sym = "cxx . . $ my_struct#"
        _struct(sym, "my_struct", doc, 100, 100, 110)
        _field(sym, "alpha", doc, 102)
        _field(sym, "gamma", doc, 106)
        _field(sym, "beta", doc, 104)
        store = _populate(idx, tmp_path)
        layout = store.get_struct_layout(sym)
        assert layout["struct_name"] == "my_struct"
        assert [f["name"] for f in layout["fields"]] == ["alpha", "beta", "gamma"]
        store.close()

    def test_multiple_structs_no_crosstalk(self, tmp_path):
        """Two structs in one file — each struct's fields must NOT leak into the other."""
        idx = scip_pb2.Index()
        doc = _doc(idx, "fs/multi.c")
        a = "cxx . . $ struct_a#"
        b = "cxx . . $ struct_b#"
        _struct(a, "struct_a", doc, 10, 10, 20)
        _struct(b, "struct_b", doc, 30, 30, 40)
        _field(a, "a1", doc, 12)
        _field(a, "a2", doc, 14)
        _field(b, "b1", doc, 32)
        _field(b, "b2", doc, 34)
        store = _populate(idx, tmp_path)
        la = store.get_struct_layout(a)
        lb = store.get_struct_layout(b)
        assert {f["name"] for f in la["fields"]} == {"a1", "a2"}, \
            f"struct_a picked up wrong fields: {[f['name'] for f in la['fields']]}"
        assert {f["name"] for f in lb["fields"]} == {"b1", "b2"}, \
            f"struct_b picked up wrong fields: {[f['name'] for f in lb['fields']]}"
        store.close()

    def test_empty_struct(self, tmp_path):
        """Struct with no fields → empty list, no crash."""
        idx = scip_pb2.Index()
        doc = _doc(idx)
        sym = "cxx . . $ empty_struct#"
        _struct(sym, "empty_struct", doc, 50, 50, 51)
        store = _populate(idx, tmp_path)
        layout = store.get_struct_layout(sym)
        assert layout["struct_name"] == "empty_struct"
        assert layout["fields"] == []
        store.close()

    def test_nonexistent_struct(self, tmp_path):
        """Unknown struct symbol → empty fields, no crash."""
        idx = scip_pb2.Index()
        _doc(idx)
        store = _populate(idx, tmp_path)
        layout = store.get_struct_layout("cxx . . $ does_not_exist#")
        assert layout["fields"] == []
        store.close()

    def test_fields_recovered_via_symbol_name_not_range(self, tmp_path):
        """contains edges come from the symbol-name descriptor hierarchy
        (Step 6: `struct#field.` → enclosing `struct#`), NOT only from the
        struct's enclosing_range. So even when scip-clang's range is
        inaccurate (body 200-201 but fields at 205-206), fields are still
        recovered — get_struct_layout is robust to range noise.

        Implication: enclosing_range inaccuracy is NOT the failure mode to
        chase; real failures must stem from a different shape."""
        idx = scip_pb2.Index()
        doc = _doc(idx, "fs/badrange.c")
        sym = "cxx . . $ bad_range_struct#"
        # struct body claimed to span only 200-201, but fields live at 205-206
        _struct(sym, "bad_range_struct", doc, 200, 200, 201)
        _field(sym, "f1", doc, 205)
        _field(sym, "f2", doc, 206)
        store = _populate(idx, tmp_path)
        layout = store.get_struct_layout(sym)
        assert [f["name"] for f in layout["fields"]] == ["f1", "f2"]
        store.close()

    def test_cross_document_struct_field_contains(self, tmp_path):
        """Regression for prepend_buffer: struct defined in Document A,
        fields in Document B. The field scip_symbols still carry the struct
        prefix, so enclosing IS recoverable from the name — but the
        parser's Step 6 only checks the CURRENT Document's symbol_map, so
        contains(struct→field) is missed when they're split across
        Documents. get_struct_layout must still recover the fields."""
        idx = scip_pb2.Index()
        idx.metadata.project_root = "/kernel"

        # Document A: struct definition only
        doc_a = idx.documents.add()
        doc_a.relative_path = "fs/def.c"
        doc_a.language = "C"
        sym = "cxx . . $ prepend_buffer#"
        _struct(sym, "prepend_buffer", doc_a, 10, 10, 20)

        # Document B: the fields (enclosing inferred from the symbol name,
        # but the struct is NOT in this document's symbol_map)
        doc_b = idx.documents.add()
        doc_b.relative_path = "fs/use.c"
        doc_b.language = "C"
        _field(sym, "buf", doc_b, 12)
        _field(sym, "len", doc_b, 14)

        store = _populate(idx, tmp_path)
        layout = store.get_struct_layout(sym)
        assert {f["name"] for f in layout["fields"]} == {"buf", "len"}
        store.close()
