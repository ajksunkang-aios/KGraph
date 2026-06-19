[English](scip-parser-design.md) | [中文](scip-parser-design.zh-CN.md)

# SCIP Protobuf Parser Design

> For review — only the approach and key decision points, no code yet.

## 1. What are we parsing?

The top-level structure of the SCIP protobuf is `Index`:

```
Index
  ├── Metadata          (1: version, tool_info, project_root)
  ├── Document[]        (N: one per source file)
  │     ├── relative_path
  │     ├── language
  │     ├── Occurrence[]   ← the bulk of the data (a kernel defconfig is ~millions)
  │     │     ├── range / typed_range (single_line_range / multi_line_range)
  │     │     ├── symbol (string, the SCIP global symbol name)
  │     │     ├── symbol_roles (bitmask: Definition=0x1, Import=0x2, ...)
  │     │     └── enclosing_range / typed_enclosing_range  ← ★ key to call-graph derivation
  │     └── SymbolInformation[]
  │           ├── symbol, display_name, documentation[], kind
  │           ├── signature_documentation
  │           ├── enclosing_symbol
  │           └── Relationship[]
  │                 ├── symbol, is_reference, is_implementation, is_type_definition, is_definition
  └── SymbolInformation[]  (external_symbols: symbols defined outside this index)
```

**Core scale**: a Linux x86_64 defconfig is about 30k files, several hundred occurrences each, ~1-2M occurrences in total.
SCIP protobuf file size: ~2-5GB (depending on whether source text is embedded).

## 2. Parsing approach

### Option comparison

| Option | Pros | Cons | Fit |
|---|---|---|---|
| **A: protobuf-c generated C code + streaming parse** | zero-copy, controllable memory, fastest | requires generating C from proto first (depends on the protobuf-c compiler); large code volume | C ingest hot path in libkgraph |
| **B: hand-written C parser (parse only the fields we need)** | no external deps, parses only the fields of interest, extremely lightweight | hand-written varint + tag parsing is error-prone; does not tolerate future proto changes | if we want maximum minimalism and no protobuf-c dependency |
| **C: Python protobuf bindings (google.protobuf)** | works out of the box, proto-generated code in one step; easy to debug | high memory use (loads the whole Index at once, ~2-5GB); slow | MVP validation / small subsystem indexing / prototype |
| **D: hand-written Python parser (pure struct decoding)** | no proto compiler dependency, controllable streaming | same hand-written risk as B; Python itself is slow | not recommended — hand-written Python is worse than just using C |

### Recommendation: two-track, phased

**MVP phase**: use **Option C (Python protobuf bindings)** to get the full pipeline working quickly.
- `pip install protobuf`, generate `scip_pb2.py` from `scip.proto`
- Stream-read in Python (the `Index` `metadata` + each `Document` processed one by one)
- Get fs/ext4 working first to validate the data model

**Production phase**: switch to **Option A (protobuf-c + C streaming parse)** for the performance hot path.
- Generate C structs from `scip.proto` (`protoc --c_out=scip.pb-c.h scip.pb-c.c`)
- Stream-parse Document by Document on the C side, batch-write to SQLite
- Python calls in via CFFI, or use a standalone CLI

**Option B (hand-written) is not recommended**: the protobuf wire format is simple (varint + tag-value), but the SCIP proto has 100+ enum values, nested oneofs, and deprecated-field compatibility — hand-writing the maintenance cost exceeds that of using protobuf-c-generated code. The proto also evolves across versions; generated code stays compatible automatically, while hand-written code must be maintained by hand.

## 3. Streaming parsing (key: cannot load the whole Index at once)

The SCIP proto's `Index` can reach 5GB, so we cannot `Index.FromString(data)` deserialize it all at once.

**Protobuf's natural advantage**: the proto3 wire format is a tag-value sequence, with each field encoded independently.
The `Index` field numbers are:
- `metadata = 1`
- `documents = 2`
- `external_symbols = 3`

This means we can **extract fields one tag at a time** from the byte stream: process and release memory as soon as each `Document` arrives.

### 3.1 C-side streaming parse (protobuf-c)

```c
// Core idea: don't call Index_unpack() for a full deserialize.
// Instead, read tag by tag, and process each Document as we encounter it.

typedef struct {
    sqlite3 *db;
    // current batch state
    uint64_t current_file_id;
    // batch write buffers
    symbol_batch_t symbols;
    occurrence_batch_t occurrences;
} ingest_state_t;

int ingest_scip_stream(ingest_state_t *state, const uint8_t *buf, size_t len) {
    ProtobufCBufferSimple buffer;  // or a custom buffer
    size_t pos = 0;

    while (pos < len) {
        uint32_t tag = read_varint(buf, &pos);
        uint32_t field_number = tag >> 3;
        uint32_t wire_type = tag & 0x7;

        switch (field_number) {
        case 1:  // metadata
            // read once, record project_root / tool_info
            skip_or_read_metadata(buf, &pos, wire_type);
            break;
        case 2:  // documents
            // ★ core: parse Document by Document + process immediately
            Document *doc = read_document_submessage(buf, &pos);
            process_document(state, doc);
            protobuf_c_message_free_unpacked(doc, NULL);  // free immediately
            break;
        case 3:  // external_symbols
            SymbolInformation *sym = read_symbol_info_submessage(buf, &pos);
            process_external_symbol(state, sym);
            protobuf_c_message_free_unpacked(sym, NULL);
            break;
        default:
            skip_field(buf, &pos, wire_type);
        }
    }
    // finally flush the batch buffers to SQLite
    flush_batches(state);
}
```

### 3.2 Python-side streaming parse (MVP)

```python
# protobuf-c-generated Python bindings do not support streaming directly.
# But we can manually read tag by tag via the low-level wire-format API.

# A more practical approach: use protobuf's MessageToDict + sharding,
# or scip-clang's own Python bindings (if available).

# Simplest MVP: for a small subsystem like fs/ext4, the Index is small enough to load whole.
from scip_pb2 import Index

with open("index.scip", "rb") as f:
    index = Index()
    index.ParseFromString(f.read())  # OK for a small subsystem

for doc in index.documents:
    for occ in doc.occurrences:
        process_occurrence(occ)
    for sym_info in doc.symbols:
        process_symbol_info(sym_info)
```

For the **full kernel** (5GB scale), Python must stream:
```python
# Read tag by tag via protobuf's low-level decoder,
# or more practically: slice index.scip on Document boundaries (the SCIP proto's
# repeated Document is a run of contiguous tag=2 submessages, cuttable by length prefix).

def stream_documents(filepath):
    """Stream Documents one by one from index.scip."""
    with open(filepath, "rb") as f:
        # 1. read metadata first (tag=1)
        # 2. loop reading Documents (tag=2)
        # 3. read external_symbols last (tag=3)
        while True:
            tag, wire_type = read_tag(f)
            field_num = tag >> 3
            if field_num == 2:  # Document
                msg_len = read_varint(f)
                raw = f.read(msg_len)
                doc = Document()
                doc.ParseFromString(raw)
                yield doc
            elif field_num == 1:
                # metadata
                msg_len = read_varint(f)
                raw = f.read(msg_len)
                meta = Metadata()
                meta.ParseFromString(raw)
                # record project_root etc.
            elif field_num == 3:
                # external_symbols
                ...
            else:
                skip_field(f, wire_type)
```

## 4. Mapping logic from SCIP to SQLite

### 4.1 Processing flow for a single Document

```
Document(relative_path="fs/ext4/file.c")
  │
  ├── Step 1: write the files table
  │     files(path="fs/ext4/file.c", language="C")
  │     → get file_id
  │
  ├── Step 2: process SymbolInformation[]
  │     for each sym_info in doc.symbols:
  │       ├── scip_symbol → symbols table (name, kind, signature, documentation)
  │       ├── display_name → symbols.name (fallback to the name parsed from symbol)
  │       ├── kind enum → symbols.kind mapping:
  │       │     Function/Method/Constructor → "function"
  │       │     Struct/Class/Interface → "struct"
  │       │     Field → "field"
  │       │     Macro → "macro"
  │       │     TypeAlias/Typedef → "typedef"
  │       │     Variable/Constant → "global_var"
  │       │     others → extend as needed
  │       ├── signature_documentation.text → symbols.signature
  │       ├── enclosing_symbol → record, used for occurrence enclosing mapping
  │       └── Relationship[] → edges table (is_implementation, is_reference, etc.)
  │            relationship type mapping:
  │            is_implementation → type "implements" (may later change to ops_bind judgment)
  │            is_reference → type "references"
  │            is_type_definition → type "type_of"
  │            is_definition → type "defines"
  │
  ├── Step 3: process Occurrence[]
  │     for each occ in doc.occurrences:
  │       ├── symbol → look up the symbols table for symbol_id (or cache a dict first)
  │       ├── symbol_roles bitmask parse:
  │       │     Definition (0x1) → role=1
  │       │     Import (0x2) → role=2
  │       │     WriteAccess (0x4) → role=4
  │       │     ReadAccess (0x8) → role=8
  │       │     ForwardDefinition (0x40) → role=64
  │       ├── range → start_line, start_col, end_line, end_col
  │       │     typed_range: single_line_range → (line, start, end)
  │       │     typed_range: multi_line_range → (s_line, s_col, e_line, e_col)
  │       │     deprecated range[3]: → (line, start, end) same line
  │       │     deprecated range[4]: → (s_line, s_col, e_line, e_col)
  │       ├── enclosing_range → ★ key!
  │       │     parse the enclosing line range
  │       │     look up: which SymbolInformation's definition range covers this enclosing range
  │       │     → get enclosing_symbol_id
  │       │     write to occurrences.enclosing_symbol_id
  │       │
  │       │  ★ Note: on a definition occurrence, enclosing_range means
  │       │    "the whole definition's range"; on a reference occurrence it means
  │       │    "which parent AST node the reference falls inside". The latter is core to call-graph derivation.
  │       │
  │       └── write the occurrences table
  │
  └── Step 4: derive call-graph edges (completed within the same Document)
        for each occ where symbol_roles & Reference and enclosing_symbol_id != NULL:
          edge_type = "calls"  (if the referenced symbol is a function/method)
          edge_type = "references" (if it's a struct/field/macro, etc.)
          write edges(src=enclosing_symbol, dst=referenced_symbol, type, file, line)
```

### 4.2 Symbol name parsing (SCIP symbol string → the metadata we need)

The SCIP symbol string format (from the proto comment):
```
<scheme> ' ' <package> ' ' <descriptor>+
e.g.: scip clang c linux v6.12 ext4_file_operations#read_iter().
```

**Parsing rules**:
- scheme: `scip` (the clang indexer's scheme)
- package: `clang c linux v6.12` (manager=`clang`, name=`c`, version=`linux v6.12`)
- descriptors: parse in sequence; each descriptor's suffix determines its type:
  - `/` → namespace
  - `#` → type (struct/class)
  - `.` → term (variable/field)
  - `()` → method, `(disambiguator).` → method
  - `!` → macro
  - `:` → meta
  - `[]` → type parameter
  - `()` → parameter

**What we need to extract from the symbol string**:
1. **short name** (the last descriptor's name) → `symbols.name`
2. **kind** (the last descriptor's suffix) → map to our kind
3. **enclosing symbol** (the prefix string with the last descriptor removed) → associate the parent symbol

```
"scip clang c linux v6.12 ext4_file_operations#read_iter()."
  → name = "read_iter"
  → kind = "function"  (suffix is ().  = Method)
  → enclosing = "scip clang c linux v6.12 ext4_file_operations#"  (struct)
```

### 4.3 ops_bind derivation (core differentiating logic)

**Trigger condition**: an occurrence's enclosing symbol has kind `global_var`,
the referenced symbol's kind is `function`, and the enclosing symbol's name matches
the `*_operations / *_ops / *_handler / *_table` pattern.

```c
// pseudocode
if (enclosing_sym.kind == "global_var" &&
    match_ops_pattern(enclosing_sym.name) &&
    referenced_sym.kind == "function") {
    // extract the field name from the symbol string or occurrence context
    // method 1: check the SCIP Relationship for an is_implementation relation
    // method 2: infer from the symbol name (.read_iter = ext4_file_read_iter)
    //           → field_name = referenced_sym.name if it lines up with a field of the enclosing type

    write_edge(enclosing_sym_id, referenced_sym_id,
               "ops_bind", file_id, line,
               metadata = json({"field_name": inferred_field_name}),
               confidence = 0.5);
}
```

**A more precise method** (recommended): use the SCIP `SymbolInformation`'s `Relationship`.
If a `SymbolInformation` has kind `Field` and its `Relationship` has an `is_definition=true`
link to some function — that is an ops_bind.

## 5. Performance hotspots

| Stage | Estimated scale | Performance strategy |
|---|---|---|
| protobuf parse | ~2-5GB raw bytes | stream Document by Document, no whole load; zero-copy parse on the C side |
| symbol name dict | ~300k entries | maintain an in-memory dict during ingest (scip_symbol→symbol_id), avoid hitting SQLite for every occurrence |
| occurrence write | ~1-2M entries | batch INSERT (one transaction per 10k entries); SQLite WAL mode |
| enclosing match | within the same Document | do the enclosing match as soon as each Document is processed (build a range→symbol_id index over all definition occurrences in the Document, then look up enclosing for each reference occurrence) |
| ops_bind derivation | ~thousands of entries | flag candidates during the occurrence pass, batch-write after the Document is processed |

**Estimated throughput**:
- Python MVP (fs/ext4, ~5k files): a few minutes is acceptable
- C production build (full kernel, 30k files): target < 10 minutes

## 6. Tech dependencies and build flow

### MVP (Python)

```bash
# 1. generate Python bindings from scip.proto
pip install protobuf grpcio-tools
protoc --python_out=src/kgraph/scip scip.proto
# → generates src/kgraph/scip/scip_pb2.py

# 2. Python parse script
# src/kgraph/ingest.py — reads index.scip → writes SQLite
```

### Production (C)

```bash
# 1. install protobuf-c
# macOS: brew install protobuf-c
# Linux: apt install libprotobuf-c-dev

# 2. generate C bindings from scip.proto
protoc --c_out=src/libkgraph/scip scip.proto
# → generates scip.pb-c.h, scip.pb-c.c

# 3. C ingest library
# src/libkgraph/ingest.c — streams index.scip → batch-writes SQLite
# build: gcc -O2 -lprotobuf-c -lsqlite3 ingest.c scip.pb-c.c -o libkgraph.so
```

## 7. Decision points to review

| # | Decision | My lean | Alternative | Notes |
|---|---|---|---|---|
| R1 | MVP parser language | **Python protobuf bindings** (get it running fast) | write C directly | validate the data model in Python first, swap in C later |
| R2 | streaming vs whole load | **whole load for small subsystems, streaming for full** | whole load even for full (~5GB RAM) | fs/ext4's index.scip is about 50-100MB, whole load is fine |
| R3 | symbol name parsing | **Python regex parse of the SCIP symbol string** | use SCIP's built-in Symbol class (the proto has a `Symbol` message) | the SCIP proto has a `Symbol` message structure (scheme + package + descriptors), but scip-clang outputs symbol in string form, which we have to parse ourselves |
| R4 | enclosing match algorithm | **range→symbol index over definition occurrences within the same Document** | cross-Document global match | definitions and references are usually in the same Document; cross-Document enclosing is rare (C header inline functions might cross) |
| R5 | ops_bind derivation trigger | **name-pattern match on global_var + kind check** | based on SymbolInformation.Relationship's is_implementation | the two are complementary: Relationship is more precise but may be incomplete; name patterns cover more but require maintaining a pattern list |
| R6 | deprecated range field compat | **support both typed_range and deprecated range** | support only typed_range | the current scip-clang version may still use deprecated range; both encodings must be supported |

Sources:
- [SCIP Protocol - github.com/sourcegraph/scip](https://github.com/sourcegraph/scip)
- [SCIP Documentation - docs.sourcegraph.com](https://docs.sourcegraph.com)
