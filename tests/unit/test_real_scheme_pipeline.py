"""
Pipeline tests against REAL scip-clang output shape.

Real scip-clang differs from the legacy synthetic fixtures in two ways that
broke struct-field discovery:

  1. Symbols use the "cxx . . $ <descriptors>" scheme (4-field canonical
     header), not "scip clang c linux v6.12 ...".
  2. SymbolInformation.kind is left as UnspecifiedKind (0) for everything,
     and enclosing_symbol is left EMPTY for non-local symbols (per the SCIP
     spec, enclosing_symbol is only for local symbols).

These tests build a Document in that real shape (kind unset, no
enclosing_symbol on fields) and verify that FIELD inference (Fix 2) and
occurrence-based contains derivation (Fix 3) recover struct fields so
get_struct_layout works end-to-end.
"""

from pathlib import Path

import scip_pb2
from parser import SCIPParser
from storage import SQLiteStore


def _build_real_shaped_index() -> bytes:
    """A 1-document index shaped like real scip-clang output."""
    index = scip_pb2.Index()
    index.metadata.project_root = "/kernel"

    doc = index.documents.add()
    doc.relative_path = "fs/myfs/ops.c"
    doc.language = "C"

    # Struct — kind is NOT set (UnspecifiedKind), matching real scip-clang.
    s = doc.symbols.add()
    s.symbol = "cxx . . $ my_file_operations#"
    s.display_name = "my_file_operations"

    # Fields — kind NOT set, enclosing_symbol NOT set (real scip-clang leaves
    # both empty; containment must be recovered positionally).
    for name in ("read_iter", "write_iter", "open"):
        f = doc.symbols.add()
        f.symbol = f"cxx . . $ my_file_operations#{name}."
        f.display_name = name

    # Struct definition occurrence: name at line 100, body spans 100–120.
    o = doc.occurrences.add()
    o.symbol = "cxx . . $ my_file_operations#"
    o.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    o.single_line_range.line = 100
    o.single_line_range.start_character = 30
    o.single_line_range.end_character = 49
    o.multi_line_enclosing_range.start_line = 100
    o.multi_line_enclosing_range.start_character = 0
    o.multi_line_enclosing_range.end_line = 120
    o.multi_line_enclosing_range.end_character = 1

    # Field definition occurrences INSIDE the struct body (105, 106, 107).
    for line, name in ((105, "read_iter"), (106, "write_iter"), (107, "open")):
        fo = doc.occurrences.add()
        fo.symbol = f"cxx . . $ my_file_operations#{name}."
        fo.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
        fo.single_line_range.line = line
        fo.single_line_range.start_character = 4
        fo.single_line_range.end_character = 4 + len(name)

    return index.SerializeToString()


def _populate(tmp_path: Path) -> SQLiteStore:
    scip_path = tmp_path / "index.scip"
    scip_path.write_bytes(_build_real_shaped_index())
    db_path = tmp_path / "kgraph.db"
    store = SQLiteStore(db_path)
    store.create_schema()
    for batch in SCIPParser(scip_path).parse():
        store.write_batch(batch)
    store.finalize()
    return store


class TestRealShapedPipeline:
    def test_fields_inferred_as_field_kind(self, tmp_path):
        """Fix 2: Term '.' under Type '#' with unset SCIP kind → FIELD."""
        store = _populate(tmp_path)
        rows = store.conn.execute(
            "SELECT name, kind FROM symbols WHERE kind = 'field'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert {"read_iter", "write_iter", "open"} <= names
        store.close()

    def test_struct_layout_returns_fields(self, tmp_path):
        """Fix 1+2+3 end-to-end: get_struct_layout recovers real fields."""
        store = _populate(tmp_path)
        layout = store.get_struct_layout("cxx . . $ my_file_operations#")
        assert layout["struct_name"] == "my_file_operations"
        field_names = [f["name"] for f in layout["fields"]]
        assert sorted(field_names) == ["open", "read_iter", "write_iter"]
        store.close()

    def test_contains_edges_exist(self, tmp_path):
        """Step 7 derived contains(struct → field) edges."""
        store = _populate(tmp_path)
        n = store.conn.execute(
            "SELECT COUNT(*) FROM edges WHERE type = 'contains'"
        ).fetchone()[0]
        assert n == 3
        store.close()

    def test_struct_kind_inferred(self, tmp_path):
        """The struct itself is inferred as STRUCT from its '#' descriptor."""
        store = _populate(tmp_path)
        row = store.conn.execute(
            "SELECT kind FROM symbols WHERE name = 'my_file_operations'"
        ).fetchone()
        assert row["kind"] == "struct"
        store.close()
