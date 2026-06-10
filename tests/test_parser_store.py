"""
KGraph — Integration test for SCIP parser → SQLite store pipeline.

Creates a minimal synthetic SCIP index in-memory, parses it,
writes to SQLite, and verifies the stored data via query methods.
"""

import os
import sys
import tempfile
from pathlib import Path

# Ensure scip_pb2 is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scip_pb2  # noqa: E402

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from parser import SCIPParser, SymbolKind, EdgeType  # noqa: E402
from storage import SQLiteStore  # noqa: E402


def create_test_index() -> bytes:
    """
    Create a minimal synthetic SCIP index for testing.

    Simulates a kernel file with:
    - One struct: ext4_file_operations
    - One function: ext4_file_read_iter
    - One ops_bind: ext4_file_operations .read_iter = ext4_file_read_iter
    - One caller: vfs_read calls ext4_file_read_iter
    """
    index = scip_pb2.Index()

    # Metadata
    index.metadata.project_root = "/kernel"
    index.metadata.tool_info.name = "scip-clang"
    index.metadata.tool_info.version = "0.3.5"

    # Document: fs/ext4/file.c
    doc = index.documents.add()
    doc.relative_path = "fs/ext4/file.c"
    doc.language = "C"

    # SymbolInformation: ext4_file_operations (struct → ops table)
    ops_sym = doc.symbols.add()
    ops_sym.symbol = "scip clang c linux v6.12 ext4_file_operations#"
    ops_sym.display_name = "ext4_file_operations"
    ops_sym.kind = scip_pb2.SymbolInformation.Kind.Value("Struct")
    ops_sym.documentation.append("ext4 file operations table")

    # SymbolInformation: ext4_file_read_iter (function)
    read_sym = doc.symbols.add()
    read_sym.symbol = "scip clang c linux v6.12 ext4_file_operations#read_iter()."
    read_sym.display_name = "ext4_file_read_iter"
    read_sym.kind = scip_pb2.SymbolInformation.Kind.Value("Method")
    read_sym.signature_documentation.text = "static ssize_t ext4_file_read_iter(struct kiocb *iocb)"
    read_sym.enclosing_symbol = "scip clang c linux v6.12 ext4_file_operations#"

    # SymbolInformation: vfs_read (function, in different conceptual file but same doc for test)
    vfs_sym = doc.symbols.add()
    vfs_sym.symbol = "scip clang c linux v6.12 vfs_read()."
    vfs_sym.display_name = "vfs_read"
    vfs_sym.kind = scip_pb2.SymbolInformation.Kind.Value("Function")

    # Occurrence: definition of ext4_file_operations
    ops_def = doc.occurrences.add()
    ops_def.symbol = "scip clang c linux v6.12 ext4_file_operations#"
    ops_def.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    ops_def.single_line_range.line = 100
    ops_def.single_line_range.start_character = 30
    ops_def.single_line_range.end_character = 55
    # enclosing_range for definition = full struct definition range (lines 100-115)
    ops_def.multi_line_enclosing_range.start_line = 100
    ops_def.multi_line_enclosing_range.start_character = 0
    ops_def.multi_line_enclosing_range.end_line = 115
    ops_def.multi_line_enclosing_range.end_character = 0

    # Occurrence: definition of ext4_file_read_iter (inside the ops struct)
    read_def = doc.occurrences.add()
    read_def.symbol = "scip clang c linux v6.12 ext4_file_operations#read_iter()."
    read_def.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    read_def.single_line_range.line = 105
    read_def.single_line_range.start_character = 40
    read_def.single_line_range.end_character = 60
    # enclosing: this definition is inside ext4_file_operations struct (lines 100-115)
    read_def.multi_line_enclosing_range.start_line = 100
    read_def.multi_line_enclosing_range.start_character = 0
    read_def.multi_line_enclosing_range.end_line = 115
    read_def.multi_line_enclosing_range.end_character = 0

    # Occurrence: definition of vfs_read (full function body lines 200-230)
    vfs_def = doc.occurrences.add()
    vfs_def.symbol = "scip clang c linux v6.12 vfs_read()."
    vfs_def.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    vfs_def.single_line_range.line = 200
    vfs_def.single_line_range.start_character = 10
    vfs_def.single_line_range.end_character = 20
    # enclosing_range = full function body (lines 200-230)
    vfs_def.multi_line_enclosing_range.start_line = 200
    vfs_def.multi_line_enclosing_range.start_character = 0
    vfs_def.multi_line_enclosing_range.end_line = 230
    vfs_def.multi_line_enclosing_range.end_character = 0

    # Occurrence: ext4_file_operations .read_iter = ext4_file_read_iter (ops_bind)
    # This reference is INSIDE the ext4_file_operations struct definition
    ops_bind_occ = doc.occurrences.add()
    ops_bind_occ.symbol = "scip clang c linux v6.12 ext4_file_operations#read_iter()."
    ops_bind_occ.symbol_roles = scip_pb2.SymbolRole.Value("ReadAccess")
    ops_bind_occ.single_line_range.line = 108  # inside the struct body (lines 100-115)
    ops_bind_occ.single_line_range.start_character = 15
    ops_bind_occ.single_line_range.end_character = 40

    # Occurrence: vfs_read calls ext4_file_read_iter (direct call, separate from ops)
    call_occ = doc.occurrences.add()
    call_occ.symbol = "scip clang c linux v6.12 ext4_file_operations#read_iter()."
    call_occ.symbol_roles = scip_pb2.SymbolRole.Value("ReadAccess")
    call_occ.single_line_range.line = 210  # inside vfs_read body (lines 200-230)
    call_occ.single_line_range.start_character = 15
    call_occ.single_line_range.end_character = 40

    # External symbol: sys_read (syscall entry)
    ext_sym = index.external_symbols.add()
    ext_sym.symbol = "scip clang c linux v6.12 sys_read()."
    ext_sym.display_name = "sys_read"
    ext_sym.kind = scip_pb2.SymbolInformation.Kind.Value("Function")

    return index.SerializeToString()


def test_parser_store_pipeline():
    """Full pipeline test: create SCIP → parse → store → query."""
    # Create test SCIP data
    scip_data = create_test_index()

    # Write to temp file (parser reads from file)
    with tempfile.NamedTemporaryFile(suffix=".scip", delete=False) as f:
        f.write(scip_data)
        scip_path = f.name

    # Parse
    parser = SCIPParser(scip_path)
    batches = list(parser.parse())

    # Verify parser output
    assert len(batches) >= 2, f"Expected >= 2 batches, got {len(batches)}"

    # Find the document batch (not metadata batch)
    doc_batch = None
    for b in batches:
        if b.file.path:
            doc_batch = b
            break
    assert doc_batch is not None, "No document batch found"
    assert doc_batch.file.path == "fs/ext4/file.c"

    # Check symbols
    print(f"Symbols: {len(doc_batch.symbols)}")
    assert len(doc_batch.symbols) >= 3

    sym_names = {s.name for s in doc_batch.symbols}
    assert "ext4_file_operations" in sym_names
    assert "ext4_file_read_iter" in sym_names
    assert "vfs_read" in sym_names

    # Check occurrences
    print(f"Occurrences: {len(doc_batch.occurrences)}")
    assert len(doc_batch.occurrences) >= 4

    # Check edges (calls + ops_bind)
    print(f"Edges: {len(doc_batch.edges)}")
    call_edges = [e for e in doc_batch.edges if e.type == EdgeType.CALLS]
    ops_edges = [e for e in doc_batch.edges if e.type == EdgeType.OPS_BIND]
    assert len(call_edges) >= 1, f"Expected >= 1 call edge, got {len(call_edges)}"
    assert len(ops_edges) >= 1, f"Expected >= 1 ops_bind edge, got {len(ops_edges)}"

    # ── Store in SQLite ──

    db_path = tempfile.mktemp(suffix=".db")
    store = SQLiteStore(db_path)
    store.create_schema()

    for batch in batches:
        store.write_batch(batch)
    store.finalize()

    # ── Query verification ──

    # 1. Search symbols
    results = store.search_symbols("ext4_file_read_iter")
    assert len(results) >= 1
    print(f"Search 'ext4_file_read_iter': {len(results)} results")

    # 2. Find callers
    callers = store.find_callers("scip clang c linux v6.12 ext4_file_operations#read_iter().")
    print(f"Callers of ext4_file_read_iter: {callers}")
    # Should find vfs_read as a caller

    # 3. Find callees
    callees = store.find_callees("scip clang c linux v6.12 vfs_read().")
    print(f"Callees of vfs_read: {callees}")

    # 4. Find ops_impls
    impls = store.find_ops_impls("read_iter")
    print(f"ops_bind impls for read_iter: {impls}")

    # 5. Get metadata
    meta = store.get_metadata()
    print(f"Metadata: {meta}")
    assert "project_root" in meta
    assert meta["project_root"] == "/kernel"

    # ── Cleanup ──

    store.close()
    os.unlink(scip_path)
    os.unlink(db_path)

    print("\n✅ All tests passed!")


if __name__ == "__main__":
    test_parser_store_pipeline()