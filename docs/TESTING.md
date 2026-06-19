[English](TESTING.md) | [中文](TESTING.zh-CN.md)

# KGraph Test Design

## Directory Structure

```
tests/
├── conftest.py                          # shared fixtures (sys.path, synthetic SCIP, populated_store, mcp_server)
├── unit/                                # unit tests — pure functions / single modules, zero external deps
│   ├── test_symbol_name.py              # parse_scip_symbol and sub-functions
│   ├── test_scip_parser_helpers.py      # _match_ops_pattern / range extraction / protobuf wire helpers
│   ├── test_models.py                   # enum values, mapping tables, dataclass defaults
│   ├── test_source_reader.py            # read_source_range / read_source_with_lineno
│   └── test_mcp_helpers.py              # _format_symbol_list / _format_edge_list
├── integration/                         # integration tests — synthetic SCIP data, no real kernel needed
│   ├── test_scip_pipeline.py            # index.scip → parser → store full pipeline (41 tests)
│   └── test_mcp_server.py               # MCP tools → kgraph.db (31 tests)
└── real/                                # real-world cases — requires a real kernel index.scip
    └── ingest_real.py                   # manual script, not pytest
```

## Running

```bash
# all tests
.venv/bin/python -m pytest tests/ -v

# unit tests only
.venv/bin/python -m pytest tests/unit/ -v

# integration tests only
.venv/bin/python -m pytest tests/integration/ -v

# real-world case (manual)
cd /path/to/linux && .venv/bin/python tests/real/ingest_real.py
```

---

## Unit Test Design

### 1. `test_symbol_name.py` — SCIP Symbol String Parser

> Source: `src/parser/symbol_name.py`
> Characteristic: all pure functions, ideal for `@pytest.mark.parametrize`

| Target | Test points | parametrize |
|----------|--------|:-----------:|
| `parse_scip_symbol` | standard function `ext4_file_read_iter().` → short_name, kind=function | ✅ |
| | struct `ext4_file_operations#` → kind=struct | ✅ |
| | struct field `ext4_file_operations#read_iter()` → kind=function, enclosing correct | ✅ |
| | Term descriptor `ext4_file_operations#read_iter.` → kind=global_var | ✅ |
| | local symbol `local foo` → kind=variable | ✅ |
| | empty string → `{}` | ✅ |
| | scheme+package only, no descriptor → short_name="" | ✅ |
| | Macro `kmalloc!` → kind=macro | ✅ |
| | escaped identifier with backticks | ✅ |
| `_parse_descriptors` | single descriptor, multiple nested descriptors, Method with disambiguator `(+1).` | ✅ |
| | TypeParameter `[T]`, Parameter `(name)` | ✅ |
| | no trailing suffix, malformed open paren | ✅ |
| `_extract_name` | simple identifier, escaped backtick, unclosed backtick, special chars `+-$` | ✅ |
| `_is_identifier_char` | letter/digit/underscore → True, `/ # .` → False | ✅ |
| `_reconstruct_descriptors` | empty list → "", escaped name gets backticks | ✅ |
| **round-trip** | `parse_scip_symbol` → `enclosing_symbol` re-parseable by `parse_scip_symbol` | ✅ |

### 2. `test_scip_parser_helpers.py` — Parser Helper Functions

> Source: `src/parser/scip_parser.py` (module-level functions)
> Characteristic: all pure functions, no protobuf objects needed

| Target | Test points | parametrize |
|----------|--------|:-----------:|
| `_match_ops_pattern` | `_operations` / `_ops` / `_handler` / `_table` → True | ✅ |
| | `_callbacks` / `_hooks` / `_methods` / `_funcs` / `_fops` → True | ✅ |
| | normal function name → False, empty string → False, case-insensitive | ✅ |
| | partial match in the middle, not at the end → False (e.g. `operations_foo`) | ✅ |
| `_language_enum_to_str` | `C` / `CPP` / `C_CPP` / `OBJECTIVE_C` → `"C"` | ✅ |
| | unknown language returned as-is, empty string → `""` | ✅ |
| `_get_symbol_kind` | in symbol_map → returns the map's kind | ✅ |
| | not in map → falls back to parse_scip_symbol | ✅ |
| | neither → DEFAULT_SYMBOL_KIND | ✅ |
| `_get_symbol_name` | in map → returns the map's name | ✅ |
| | not in map → falls back to parse_scip_symbol's short_name | ✅ |
| | neither → returns the raw scip_symbol string | ✅ |
| `_json_metadata` | normal dict → JSON string, empty dict → `"{}"` | ✅ |
| `_read_varint32` | single byte (<128), multi-byte, zero, truncated buf → ValueError | ✅ |
| `_read_tag` | field_number + wire_type parsed correctly | ✅ |
| `_skip_field` | wire_type 0/1/2/5 → skipped correctly, unknown wire_type → ValueError | ✅ |

### 3. `test_models.py` — Data Model Validation

> Source: `src/parser/models.py`
> Characteristic: constant/enum validation, no dependencies

| Target | Test points |
|----------|--------|
| `SymbolKind` | each constant is the correct string value (`FUNCTION == "function"`) |
| `EdgeType` | each constant is the correct string value, `OPS_BIND == "ops_bind"` |
| `SymbolRole` | bitmask values correct: `DEFINITION=0x1`, `READ_ACCESS=0x8` |
| | combination: `DEFINITION | READ_ACCESS == 0x9`, `bool(roles & DEFINITION)` |
| `SCIP_KIND_TO_SYMBOL_KIND` | known SCIP kind int (17→function, 49→struct, 25→macro) mapped correctly |
| | C special mapping: 7→struct (not class) |
| | all values are valid `SymbolKind` values |
| Dataclass defaults | `FileRecord()` defaults `language="C"` |
| | `EdgeRecord()` defaults `weight=1, confidence=1.0` |
| | `IngestBatch()` defaults to empty lists |
| | `SymbolRecord` required fields `scip_symbol/name/kind` |

### 4. `test_source_reader.py` — Source Reader

> Source: `mcp/source_reader.py`
> Dependency: tmp_path creates fake files

| Target | Test points |
|----------|--------|
| `read_source_range` | normal range read (0-based line numbers) |
| | `start_line < 0` → None |
| | file does not exist → None |
| | `start_line` exceeds the file line count → None |
| | `context > 0` expands the range, clamped to `[0, len)` |
| | single-line range (`start == end`) |
| `read_source_with_lineno` | 1-based line numbers, 6-column right-aligned, tab-separated |
| | propagates None when `read_source_range` returns None |
| | context adjusts the start line number correctly |

### 5. `test_mcp_helpers.py` — MCP Formatting Functions

> Source: `mcp/server.py` (`_format_symbol_list` / `_format_edge_list` / `_resolve_one`)
> Characteristic: `_format_*` are pure functions; `_resolve_one` needs a mock store

| Target | Test points | parametrize |
|----------|--------|:-----------:|
| `_format_symbol_list` | normal result formatting, `def_start_line=-1` shows `(external)` | ✅ |
| | `include_scip=True` adds the `id:` line, with/without signature | ✅ |
| | empty list → `"Found 0 symbol(s):"` | ✅ |
| `_format_edge_list` | depth>1 indentation, `ops_bind` marked `[ops_bind]` | ✅ |
| | direction string in the heading, `line=None` shows `:?` | ✅ |
| `_resolve_one` | has a non-external definition → returns its scip_symbol | ✅ |
| | all external → returns the first one's scip_symbol | ✅ |
| | no candidates → None | ✅ |
| | `prefer_kind` filter yields nothing → retry without a kind filter | ✅ |

---

## Integration Test Design (complete)

### `test_scip_pipeline.py` — SCIP → Store Full Pipeline (41 tests)

**Parser output validation (TestParserOutput)**:
- batch count and types (metadata / document / external_symbols)
- symbol name and kind mapping correctness
- occurrence count
- derivation of calls / ops_bind / implements / type_of edges
- ops_bind confidence=0.5 and metadata JSON
- external symbol marking

**Store query validation (TestStoreQueries)**:
- `search_symbols`: exact / fuzzy / with kind filter / no result
- `get_symbol`: exact name / kind filter / not found
- `find_callers` / `find_callees`: direct calls / multi-level traversal / ops_bind edges
- `find_ops_impls`: field_name match / struct_type filter
- `find_references`: definition + reference / enclosing info
- `find_type_definition`: type_of edge traversal
- `get_struct_layout`: contains edge → struct fields
- `get_neighborhood`: depth=1/2, summary mode
- `call_path`: has path / no path
- `get_metadata`: project_root / tool_name / total_symbols
- `get_definition_location`: normal / external

### `test_mcp_server.py` — MCP Tools Integration (31 tests)

- normal-path validation of all 12 MCP tool functions
- not-found path validation (`"No symbol named"` / `"No symbols found"`)
- `get_function_body`: reads a fake source file from tmpdir
- `get_struct_layout`: contains edge shows fields
- `call_path`: source/target not found cases

---

## Synthetic Benchmark Data

`build_synthetic_scip_index()` in `conftest.py` builds a mock ext4 VFS scenario:

```
3 Documents:
  fs/ext4/file.c     — ext4_file_operations (struct, ops table)
                        ext4_file_read_iter / ext4_file_write_iter / ext4_file_open (function)
                        read_iter / write_iter / open (field)
  fs/read_write.c    — vfs_read / vfs_write (function, direct calls)
  include/linux/fs.h — file_operations (struct), loff_t (typedef)

2 External symbols:
  sys_read / __fdget_pos

Edge type coverage:
  calls       — vfs_read → ext4_file_read_iter
  ops_bind    — ext4_file_operations → ext4_file_read_iter (×3)
  implements  — ext4_file_operations → file_operations
  type_of     — read_iter → ext4_file_read_iter
  contains    — ext4_file_operations → {read_iter, write_iter, open} (parser Step 6)
```

---

## Fixture Dependency Graph

```
scip_index_bytes (session)
  └─ scip_file (function)
       └─ populated_store (function) ─── SQLiteStore instance, finalized
            │
            ├─ project_root (function) ─── fake kernel source file tree
            │    └─ mcp_server (function) ─── loads mcp/server.py, injects test DB + source root
            │
            └─ SQLiteStore unit tests: create an independent :memory: DB via tmp_path
```

---

## Known Pipeline Defects

| Defect | Impact | Status |
|------|------|------|
| **Cross-document edge loss**: edges are written within the same batch; when they reference a symbol from another Document that isn't in the store yet, the edge is silently dropped | type_of / implements and other cross-document relationships are lost | To fix |
| **Missing contains edges**: the parser resolved `enclosing_symbol` but didn't derive `contains` edges | `get_struct_layout` has no fields | ✅ Fixed (Step 6) |
