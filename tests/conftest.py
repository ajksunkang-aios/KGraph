"""
KGraph — pytest configuration and shared fixtures.

Provides:
  - sys.path setup for src/ and scripts/
  - Synthetic SCIP index builder (comprehensive kernel scenario)
  - Fixture: scip_file       — temp .scip file from synthetic index
  - Fixture: populated_store — SQLiteStore loaded from synthetic SCIP data
  - Fixture: project_root    — tmpdir with fake kernel source tree
  - Fixture: mcp_server      — loaded mcp/server.py module wired to test DB
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest

# ──────────────────────────────────────────────
# sys.path setup
# ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Ensure src/ and scripts/ are importable for all test modules
for _sub in ("src", "scripts"):
    _p = str(_PROJECT_ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Late imports after path setup
import scip_pb2  # noqa: E402
from parser import SCIPParser  # noqa: E402
from storage import SQLiteStore  # noqa: E402


# ──────────────────────────────────────────────
# Synthetic SCIP index builder
# ──────────────────────────────────────────────

def build_synthetic_scip_index() -> bytes:
    """
    Build a comprehensive synthetic SCIP index for testing.

    Simulates a kernel scenario with:
      - Document fs/ext4/file.c:
          ext4_file_operations (struct, ops table)
            → fields: read_iter, write_iter, open  (ops_bind)
          ext4_file_read_iter  (function, defined in ops struct body)
          ext4_file_write_iter (function, defined in ops struct body)
          ext4_file_open       (function, defined in ops struct body)
      - Document fs/read_write.c:
          vfs_read  (function, calls ext4_file_read_iter)
          vfs_write (function, calls ext4_file_write_iter)
      - Document include/linux/fs.h:
          file_operations (struct, generic type)
          loff_t (typedef, for type_of test)
      - External symbols:
          sys_read (function, no definition)
          __fdget_pos (function, no definition)
      - Relationships:
          ext4_file_operations implements file_operations (is_implementation)
      - Metadata:
          project_root="/kernel", tool_info
    """
    index = scip_pb2.Index()

    # ── Metadata ──
    index.metadata.project_root = "/kernel"
    index.metadata.tool_info.name = "scip-clang"
    index.metadata.tool_info.version = "0.3.5"

    # ============================================================
    # Document 1: fs/ext4/file.c
    # ============================================================
    doc1 = index.documents.add()
    doc1.relative_path = "fs/ext4/file.c"
    doc1.language = "C"

    # Symbol: ext4_file_operations (struct — ops table)
    ops_sym = doc1.symbols.add()
    ops_sym.symbol = "scip clang c linux v6.12 ext4_file_operations#"
    ops_sym.display_name = "ext4_file_operations"
    ops_sym.kind = scip_pb2.SymbolInformation.Kind.Value("Struct")
    ops_sym.documentation.append("ext4 file operations table")
    # Relationship: ext4_file_operations implements file_operations
    rel_impl = ops_sym.relationships.add()
    rel_impl.symbol = "scip clang c linux v6.12 file_operations#"
    rel_impl.is_implementation = True

    # Symbol: ext4_file_operations.read_iter (field)
    field_read = doc1.symbols.add()
    field_read.symbol = "scip clang c linux v6.12 ext4_file_operations#read_iter."
    field_read.display_name = "read_iter"
    field_read.kind = scip_pb2.SymbolInformation.Kind.Value("Field")
    field_read.enclosing_symbol = "scip clang c linux v6.12 ext4_file_operations#"
    # Relationship: field type_of → function pointer type
    # NOTE: target symbol must be in the SAME document; cross-document edges
    # are silently dropped because edges are written before later documents are parsed.
    rel_type = field_read.relationships.add()
    rel_type.symbol = "scip clang c linux v6.12 ext4_file_read_iter()."
    rel_type.is_type_definition = True

    # Symbol: ext4_file_operations.write_iter (field)
    field_write = doc1.symbols.add()
    field_write.symbol = "scip clang c linux v6.12 ext4_file_operations#write_iter."
    field_write.display_name = "write_iter"
    field_write.kind = scip_pb2.SymbolInformation.Kind.Value("Field")
    field_write.enclosing_symbol = "scip clang c linux v6.12 ext4_file_operations#"

    # Symbol: ext4_file_operations.open (field)
    field_open = doc1.symbols.add()
    field_open.symbol = "scip clang c linux v6.12 ext4_file_operations#open."
    field_open.display_name = "open"
    field_open.kind = scip_pb2.SymbolInformation.Kind.Value("Field")
    field_open.enclosing_symbol = "scip clang c linux v6.12 ext4_file_operations#"

    # Symbol: ext4_file_read_iter (function)
    read_fn = doc1.symbols.add()
    read_fn.symbol = "scip clang c linux v6.12 ext4_file_read_iter()."
    read_fn.display_name = "ext4_file_read_iter"
    read_fn.kind = scip_pb2.SymbolInformation.Kind.Value("Function")
    read_fn.signature_documentation.text = "static ssize_t ext4_file_read_iter(struct kiocb *iocb, struct iov_iter *to)"
    read_fn.enclosing_symbol = ""

    # Symbol: ext4_file_write_iter (function)
    write_fn = doc1.symbols.add()
    write_fn.symbol = "scip clang c linux v6.12 ext4_file_write_iter()."
    write_fn.display_name = "ext4_file_write_iter"
    write_fn.kind = scip_pb2.SymbolInformation.Kind.Value("Function")
    write_fn.signature_documentation.text = "static ssize_t ext4_file_write_iter(struct kiocb *iocb, struct iov_iter *from)"
    write_fn.enclosing_symbol = ""

    # Symbol: ext4_file_open (function)
    open_fn = doc1.symbols.add()
    open_fn.symbol = "scip clang c linux v6.12 ext4_file_open()."
    open_fn.display_name = "ext4_file_open"
    open_fn.kind = scip_pb2.SymbolInformation.Kind.Value("Function")
    open_fn.signature_documentation.text = "static int ext4_file_open(struct inode *inode, struct file *filp)"
    open_fn.enclosing_symbol = ""

    # ── Occurrences in fs/ext4/file.c ──

    # [DEF] ext4_file_operations struct body (lines 100–120)
    occ = doc1.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 ext4_file_operations#"
    occ.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    occ.single_line_range.line = 100
    occ.single_line_range.start_character = 30
    occ.single_line_range.end_character = 55
    occ.multi_line_enclosing_range.start_line = 100
    occ.multi_line_enclosing_range.start_character = 0
    occ.multi_line_enclosing_range.end_line = 120
    occ.multi_line_enclosing_range.end_character = 1

    # [DEF] ext4_file_read_iter function body (lines 50–80)
    occ = doc1.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 ext4_file_read_iter()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    occ.single_line_range.line = 50
    occ.single_line_range.start_character = 20
    occ.single_line_range.end_character = 42
    occ.multi_line_enclosing_range.start_line = 50
    occ.multi_line_enclosing_range.start_character = 0
    occ.multi_line_enclosing_range.end_line = 80
    occ.multi_line_enclosing_range.end_character = 1

    # [DEF] ext4_file_write_iter function body (lines 82–110)
    occ = doc1.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 ext4_file_write_iter()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    occ.single_line_range.line = 82
    occ.single_line_range.start_character = 20
    occ.single_line_range.end_character = 43
    occ.multi_line_enclosing_range.start_line = 82
    occ.multi_line_enclosing_range.start_character = 0
    occ.multi_line_enclosing_range.end_line = 110
    occ.multi_line_enclosing_range.end_character = 1

    # [DEF] ext4_file_open function body (lines 112–130)
    occ = doc1.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 ext4_file_open()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    occ.single_line_range.line = 112
    occ.single_line_range.start_character = 15
    occ.single_line_range.end_character = 31
    occ.multi_line_enclosing_range.start_line = 112
    occ.multi_line_enclosing_range.start_character = 0
    occ.multi_line_enclosing_range.end_line = 130
    occ.multi_line_enclosing_range.end_character = 1

    # [REF → ops_bind] .read_iter = ext4_file_read_iter  (line 105, inside ops struct 100-120)
    occ = doc1.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 ext4_file_read_iter()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("ReadAccess")
    occ.single_line_range.line = 105
    occ.single_line_range.start_character = 15
    occ.single_line_range.end_character = 37

    # [REF → ops_bind] .write_iter = ext4_file_write_iter  (line 106, inside ops struct 100-120)
    occ = doc1.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 ext4_file_write_iter()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("ReadAccess")
    occ.single_line_range.line = 106
    occ.single_line_range.start_character = 15
    occ.single_line_range.end_character = 38

    # [REF → ops_bind] .open = ext4_file_open  (line 107, inside ops struct 100-120)
    occ = doc1.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 ext4_file_open()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("ReadAccess")
    occ.single_line_range.line = 107
    occ.single_line_range.start_character = 15
    occ.single_line_range.end_character = 30

    # ============================================================
    # Document 2: fs/read_write.c
    # ============================================================
    doc2 = index.documents.add()
    doc2.relative_path = "fs/read_write.c"
    doc2.language = "C"

    # Symbol: vfs_read
    vfs_read_sym = doc2.symbols.add()
    vfs_read_sym.symbol = "scip clang c linux v6.12 vfs_read()."
    vfs_read_sym.display_name = "vfs_read"
    vfs_read_sym.kind = scip_pb2.SymbolInformation.Kind.Value("Function")
    vfs_read_sym.signature_documentation.text = "ssize_t vfs_read(struct file *file, char __user *buf, size_t count, loff_t *pos)"

    # Symbol: vfs_write
    vfs_write_sym = doc2.symbols.add()
    vfs_write_sym.symbol = "scip clang c linux v6.12 vfs_write()."
    vfs_write_sym.display_name = "vfs_write"
    vfs_write_sym.kind = scip_pb2.SymbolInformation.Kind.Value("Function")
    vfs_write_sym.signature_documentation.text = "ssize_t vfs_write(struct file *file, const char __user *buf, size_t count, loff_t *pos)"

    # [DEF] vfs_read function body (lines 200–230)
    occ = doc2.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 vfs_read()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    occ.single_line_range.line = 200
    occ.single_line_range.start_character = 10
    occ.single_line_range.end_character = 19
    occ.multi_line_enclosing_range.start_line = 200
    occ.multi_line_enclosing_range.start_character = 0
    occ.multi_line_enclosing_range.end_line = 230
    occ.multi_line_enclosing_range.end_character = 1

    # [DEF] vfs_write function body (lines 240–270)
    occ = doc2.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 vfs_write()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    occ.single_line_range.line = 240
    occ.single_line_range.start_character = 10
    occ.single_line_range.end_character = 20
    occ.multi_line_enclosing_range.start_line = 240
    occ.multi_line_enclosing_range.start_character = 0
    occ.multi_line_enclosing_range.end_line = 270
    occ.multi_line_enclosing_range.end_character = 1

    # [REF → calls] vfs_read calls ext4_file_read_iter  (line 210, inside vfs_read 200-230)
    occ = doc2.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 ext4_file_read_iter()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("ReadAccess")
    occ.single_line_range.line = 210
    occ.single_line_range.start_character = 8
    occ.single_line_range.end_character = 30

    # [REF → calls] vfs_write calls ext4_file_write_iter  (line 250, inside vfs_write 240-270)
    occ = doc2.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 ext4_file_write_iter()."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("ReadAccess")
    occ.single_line_range.line = 250
    occ.single_line_range.start_character = 8
    occ.single_line_range.end_character = 31

    # ============================================================
    # Document 3: include/linux/fs.h
    # ============================================================
    doc3 = index.documents.add()
    doc3.relative_path = "include/linux/fs.h"
    doc3.language = "C"

    # Symbol: file_operations (struct — generic type)
    fops_sym = doc3.symbols.add()
    fops_sym.symbol = "scip clang c linux v6.12 file_operations#"
    fops_sym.display_name = "file_operations"
    fops_sym.kind = scip_pb2.SymbolInformation.Kind.Value("Struct")
    fops_sym.documentation.append("VFS file operations")

    # Symbol: loff_t (typedef)
    loff_sym = doc3.symbols.add()
    loff_sym.symbol = "scip clang c linux v6.12 loff_t."
    loff_sym.display_name = "loff_t"
    loff_sym.kind = scip_pb2.SymbolInformation.Kind.Value("TypeAlias")

    # [DEF] file_operations struct body (lines 300–340)
    occ = doc3.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 file_operations#"
    occ.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    occ.single_line_range.line = 300
    occ.single_line_range.start_character = 20
    occ.single_line_range.end_character = 37
    occ.multi_line_enclosing_range.start_line = 300
    occ.multi_line_enclosing_range.start_character = 0
    occ.multi_line_enclosing_range.end_line = 340
    occ.multi_line_enclosing_range.end_character = 1

    # [DEF] loff_t typedef (line 10)
    occ = doc3.occurrences.add()
    occ.symbol = "scip clang c linux v6.12 loff_t."
    occ.symbol_roles = scip_pb2.SymbolRole.Value("Definition")
    occ.single_line_range.line = 10
    occ.single_line_range.start_character = 18
    occ.single_line_range.end_character = 25
    occ.multi_line_enclosing_range.start_line = 10
    occ.multi_line_enclosing_range.start_character = 0
    occ.multi_line_enclosing_range.end_line = 10
    occ.multi_line_enclosing_range.end_character = 30

    # ============================================================
    # External symbols
    # ============================================================
    ext1 = index.external_symbols.add()
    ext1.symbol = "scip clang c linux v6.12 sys_read()."
    ext1.display_name = "sys_read"
    ext1.kind = scip_pb2.SymbolInformation.Kind.Value("Function")

    ext2 = index.external_symbols.add()
    ext2.symbol = "scip clang c linux v6.12 __fdget_pos()."
    ext2.display_name = "__fdget_pos"
    ext2.kind = scip_pb2.SymbolInformation.Kind.Value("Function")

    return index.SerializeToString()


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture(scope="session")
def scip_index_bytes() -> bytes:
    """Raw bytes of the synthetic SCIP index."""
    return build_synthetic_scip_index()


@pytest.fixture()
def scip_file(scip_index_bytes: bytes, tmp_path: Path) -> Path:
    """Write synthetic SCIP bytes to a temp file and return its path."""
    path = tmp_path / "index.scip"
    path.write_bytes(scip_index_bytes)
    return path


@pytest.fixture()
def populated_store(scip_file: Path, tmp_path: Path) -> Generator[SQLiteStore, None, None]:
    """
    Parse synthetic SCIP → populate SQLiteStore → yield the store.

    The store is fully finalized and ready for querying.
    Cleanup: close connection and delete temp db file.
    """
    db_path = tmp_path / "kgraph.db"

    parser = SCIPParser(scip_file)
    store = SQLiteStore(db_path)
    store.create_schema()

    for batch in parser.parse():
        store.write_batch(batch)
    store.finalize()

    yield store

    store.close()


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """
    Create a fake kernel source tree with minimal C files.

    Matches the synthetic SCIP index file paths so MCP get_function_body works.
    """
    # fs/ext4/file.c — contains ext4_file_operations, ext4_file_read_iter, etc.
    ext4_dir = tmp_path / "fs" / "ext4"
    ext4_dir.mkdir(parents=True)
    (ext4_dir / "file.c").write_text(
        "/* fs/ext4/file.c */\n"
        + _make_lines(1, 49)
        + "static ssize_t ext4_file_read_iter(struct kiocb *iocb, struct iov_iter *to)\n"
        + "{\n"
        + "    return generic_file_read_iter(iocb, to);\n"
        + "}\n"
        + _make_lines(55, 81)
        + "static ssize_t ext4_file_write_iter(struct kiocb *iocb, struct iov_iter *from)\n"
        + "{\n"
        + "    return generic_file_write_iter(iocb, from);\n"
        + "}\n"
        + _make_lines(87, 99)
        + "const struct file_operations ext4_file_operations = {\n"
        + "    .read_iter   = ext4_file_read_iter,\n"
        + "    .write_iter  = ext4_file_write_iter,\n"
        + "    .open        = ext4_file_open,\n"
        + "};\n"
        + _make_lines(116, 130)
    )

    # fs/read_write.c — contains vfs_read, vfs_write
    rw_dir = tmp_path / "fs"
    rw_dir.mkdir(parents=True, exist_ok=True)
    (rw_dir / "read_write.c").write_text(
        "/* fs/read_write.c */\n"
        + _make_lines(1, 199)
        + "ssize_t vfs_read(struct file *file, char __user *buf, size_t count, loff_t *pos)\n"
        + "{\n"
        + "    ext4_file_read_iter(iocb, to);\n"
        + "    return 0;\n"
        + "}\n"
        + _make_lines(207, 239)
        + "ssize_t vfs_write(struct file *file, const char __user *buf, size_t count, loff_t *pos)\n"
        + "{\n"
        + "    ext4_file_write_iter(iocb, from);\n"
        + "    return 0;\n"
        + "}\n"
    )

    # include/linux/fs.h — contains file_operations, loff_t
    inc_dir = tmp_path / "include" / "linux"
    inc_dir.mkdir(parents=True)
    (inc_dir / "fs.h").write_text(
        "/* include/linux/fs.h */\n"
        + _make_lines(1, 9)
        + "typedef long long loff_t;\n"
        + _make_lines(11, 299)
        + "struct file_operations {\n"
        + "    ssize_t (*read_iter)(struct kiocb *, struct iov_iter *);\n"
        + "    ssize_t (*write_iter)(struct kiocb *, struct iov_iter *);\n"
        + "    int (*open)(struct inode *, struct file *);\n"
        + "};\n"
        + _make_lines(306, 340)
    )

    return tmp_path


@pytest.fixture()
def mcp_server(populated_store: SQLiteStore, project_root: Path):
    """
    Load mcp/server.py with its DB and root pointing at our test fixtures.

    The tool functions are callable directly as Python functions on the
    returned module object.
    """
    # Set env vars BEFORE loading the module so module-level path resolution works
    db_path = str(populated_store.db_path)
    root_path = str(project_root)

    old_db = os.environ.get("KGRAPH_DB")
    old_root = os.environ.get("KGRAPH_ROOT")
    os.environ["KGRAPH_DB"] = db_path
    os.environ["KGRAPH_ROOT"] = root_path

    server_py = _PROJECT_ROOT / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("kgraph_server", str(server_py))
    mod = importlib.util.module_from_spec(spec)

    # Inject our pre-populated store so the server doesn't open its own
    # We need to set it AFTER the module is loaded (which defines get_store)
    spec.loader.exec_module(mod)
    mod._store = populated_store

    yield mod

    # Cleanup
    mod._store = None
    if old_db is None:
        os.environ.pop("KGRAPH_DB", None)
    else:
        os.environ["KGRAPH_DB"] = old_db
    if old_root is None:
        os.environ.pop("KGRAPH_ROOT", None)
    else:
        os.environ["KGRAPH_ROOT"] = old_root


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _make_lines(start: int, end: int) -> str:
    """Generate placeholder lines from start to end (1-based, inclusive)."""
    return "".join(f"/* line {i} */\n" for i in range(start, end + 1))
