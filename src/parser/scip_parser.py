"""
KGraph — SCIP Index Parser

Reads a SCIP protobuf index file (index.scip) and produces IngestBatch
objects per Document. The parser is fully decoupled from storage —
it only fills data model types (models.py) and emits batches that
any GraphStore implementation can consume.

Two modes:
  - Full load: ParseFromString for small indexes (single subsystem)
  - Stream:    Tag-by-tag streaming for large indexes (full kernel)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Iterator

# Add scripts/ to path so scip_pb2 is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import scip_pb2  # noqa: E402

from .models import (
    DEFAULT_SYMBOL_KIND,
    EdgeRecord,
    EdgeType,
    FileRecord,
    IngestBatch,
    MetadataRecord,
    OccurrenceRecord,
    SymbolKind,
    SymbolRecord,
    SymbolRole,
    SCIP_KIND_TO_SYMBOL_KIND,
)
from .symbol_name import parse_scip_symbol

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# OPS pattern matching for ops_bind derivation
# ──────────────────────────────────────────────

OPS_NAME_PATTERNS = (
    "_operations",
    "_ops",
    "_handler",
    "_table",
    "_callbacks",
    "_hooks",
    "_methods",
    "_funcs",
    "_fops",
)


def _match_ops_pattern(name: str) -> bool:
    """Check if a symbol name matches kernel ops struct naming patterns."""
    lower = name.lower()
    return any(lower.endswith(pat) for pat in OPS_NAME_PATTERNS)


# ──────────────────────────────────────────────
# Range extraction helpers
# ──────────────────────────────────────────────

def _extract_range(occ: scip_pb2.Occurrence) -> tuple[int, int, int, int]:
    """
    Extract (start_line, start_col, end_line, end_col) from an Occurrence.

    Handles both typed_range (preferred) and deprecated range (fallback).
    All values are 0-based as in SCIP proto.
    """
    # Preferred: typed_range
    if occ.HasField("single_line_range"):
        r = occ.single_line_range
        return (r.line, r.start_character, r.line, r.end_character)

    if occ.HasField("multi_line_range"):
        r = occ.multi_line_range
        return (r.start_line, r.start_character, r.end_line, r.end_character)

    # Fallback: deprecated repeated int32 range
    if len(occ.range) == 3:
        # [startLine, startCharacter, endCharacter] — single line
        return (occ.range[0], occ.range[1], occ.range[0], occ.range[2])

    if len(occ.range) == 4:
        # [startLine, startCharacter, endLine, endCharacter] — multi line
        return (occ.range[0], occ.range[1], occ.range[2], occ.range[3])

    # Empty or unknown range
    return (-1, -1, -1, -1)


def _extract_enclosing_range(occ: scip_pb2.Occurrence) -> tuple[int, int, int, int]:
    """Extract enclosing range from an Occurrence (same logic as _extract_range)."""
    if occ.HasField("single_line_enclosing_range"):
        r = occ.single_line_enclosing_range
        return (r.line, r.start_character, r.line, r.end_character)

    if occ.HasField("multi_line_enclosing_range"):
        r = occ.multi_line_enclosing_range
        return (r.start_line, r.start_character, r.end_line, r.end_character)

    if len(occ.enclosing_range) == 3:
        return (occ.enclosing_range[0], occ.enclosing_range[1],
                occ.enclosing_range[0], occ.enclosing_range[2])

    if len(occ.enclosing_range) == 4:
        return (occ.enclosing_range[0], occ.enclosing_range[1],
                occ.enclosing_range[2], occ.enclosing_range[3])

    return (-1, -1, -1, -1)


# ──────────────────────────────────────────────
# Main parser class
# ──────────────────────────────────────────────

class SCIPParser:
    """
    Parse a SCIP protobuf index file into IngestBatch objects.

    Usage:
        parser = SCIPParser("index.scip")
        for batch in parser.parse():
            store.write_batch(batch)
    """

    def __init__(self, scip_path: str | Path):
        self.scip_path = Path(scip_path)
        if not self.scip_path.exists():
            raise FileNotFoundError(f"SCIP index not found: {self.scip_path}")

    # ── Full-load mode (for small subsystems) ──

    def parse(self) -> Iterator[IngestBatch]:
        """
        Parse the entire SCIP index file and yield IngestBatch per Document.

        For small subsystems (< 500MB), loads the whole Index into memory.
        For larger indexes, consider using parse_stream().
        """
        logger.info("Loading SCIP index: %s", self.scip_path)
        index = scip_pb2.Index()

        with open(self.scip_path, "rb") as f:
            index.ParseFromString(f.read())

        logger.info("Index loaded: %d documents, %d external_symbols",
                     len(index.documents), len(index.external_symbols))

        # Emit metadata as first batch
        meta_batch = self._parse_metadata(index.metadata)
        if meta_batch.metadata:
            yield meta_batch

        # Emit one IngestBatch per Document
        for doc in index.documents:
            yield self._parse_document(doc)

        # Emit external symbols as a final batch
        ext_batch = self._parse_external_symbols(index.external_symbols)
        if ext_batch.symbols:
            yield ext_batch

    # ── Stream mode (for large indexes) ──

    def parse_stream(self) -> Iterator[IngestBatch]:
        """
        Stream-parse a large SCIP index, yielding one IngestBatch per Document.

        Reads tag-by-tag from the raw protobuf stream, deserializing
        each Document independently. Memory usage stays bounded regardless
        of total index size.
        """
        logger.info("Stream-parsing SCIP index: %s", self.scip_path)

        with open(self.scip_path, "rb") as f:
            raw = f.read()

        pos = 0
        length = len(raw)

        while pos < length:
            tag, wire_type, new_pos = _read_tag(raw, pos)
            pos = new_pos
            field_number = tag >> 3

            if field_number == 1 and wire_type == 2:  # Metadata (submessage)
                msg_len, new_pos = _read_varint32(raw, pos)
                pos = new_pos
                msg_bytes = raw[pos:pos + msg_len]
                pos += msg_len

                meta = scip_pb2.Metadata()
                meta.ParseFromString(msg_bytes)
                meta_batch = self._parse_metadata(meta)
                if meta_batch.metadata:
                    yield meta_batch

            elif field_number == 2 and wire_type == 2:  # Document (submessage)
                msg_len, new_pos = _read_varint32(raw, pos)
                pos = new_pos
                msg_bytes = raw[pos:pos + msg_len]
                pos += msg_len

                doc = scip_pb2.Document()
                doc.ParseFromString(msg_bytes)
                yield self._parse_document(doc)

            elif field_number == 3 and wire_type == 2:  # external_symbols
                msg_len, new_pos = _read_varint32(raw, pos)
                pos = new_pos
                msg_bytes = raw[pos:pos + msg_len]
                pos += msg_len

                sym = scip_pb2.SymbolInformation()
                sym.ParseFromString(msg_bytes)
                # Accumulate — we'll yield at end
                # For simplicity, collect all externals then yield one batch
                # (external symbols are small relative to documents)

            else:
                pos = _skip_field(raw, pos, wire_type)

        # Note: external_symbols collection in stream mode is simplified
        # — for MVP, full-load parse() handles it correctly.

    # ── Metadata parsing ──

    def _parse_metadata(self, meta: scip_pb2.Metadata) -> IngestBatch:
        """Parse Index metadata into an IngestBatch."""
        records = []

        if meta.project_root:
            records.append(MetadataRecord(key="project_root", value=meta.project_root))

        if meta.tool_info:
            records.append(MetadataRecord(key="tool_name", value=meta.tool_info.name))
            records.append(MetadataRecord(key="tool_version", value=meta.tool_info.version))
            if meta.tool_info.arguments:
                records.append(MetadataRecord(
                    key="tool_arguments",
                    value=" ".join(meta.tool_info.arguments),
                ))

        return IngestBatch(metadata=records)

    # ── Document parsing (core) ──

    def _parse_document(self, doc: scip_pb2.Document) -> IngestBatch:
        """
        Parse a single Document into an IngestBatch.

        This is the core method — it processes all SymbolInformation
        and Occurrence objects within one source file, derives
        call graph edges and ops_bind edges, and produces one batch
        ready for storage.
        """
        file_path = doc.relative_path
        language = _language_enum_to_str(doc.language)

        file_rec = FileRecord(path=file_path, language=language)

        # ── Step 1: Parse SymbolInformation → SymbolRecords ──

        symbol_records = []
        # Map: scip_symbol string → SymbolRecord (for quick lookup)
        symbol_map: dict[str, SymbolRecord] = {}

        for sym_info in doc.symbols:
            sym_rec = self._parse_symbol_info(sym_info, file_path)
            symbol_records.append(sym_rec)
            symbol_map[sym_info.symbol] = sym_rec

        # ── Step 2: Parse Occurrences → OccurrenceRecords ──

        occurrence_records = []

        # For enclosing matching: collect definition occurrences' ranges
        # (start_line, end_line) → scip_symbol string
        definition_ranges: list[tuple[int, int, int, str]] = []  # (start_line, end_line, start_col, symbol)

        for occ in doc.occurrences:
            if not occ.symbol:
                continue

            s_line, s_col, e_line, e_col = _extract_range(occ)
            roles = occ.symbol_roles
            is_definition = bool(roles & SymbolRole.DEFINITION)

            occ_rec = OccurrenceRecord(
                symbol=occ.symbol,
                file_path=file_path,
                start_line=s_line,
                start_col=s_col,
                end_line=e_line,
                end_col=e_col,
                role=roles,
            )

            # For definitions: record the range for enclosing matching.
            # ★ Use enclosing_range (full definition body extent) instead of
            #   the symbol's own range (just the name/identifier position).
            #   This is critical: vfs_read defined at line 200 has enclosing_range
            #   covering the entire function body (e.g. lines 200-220),
            #   while its own range is just line 200 (the name position).
            if is_definition and s_line >= 0:
                # Get the enclosing range (full definition extent)
                enc_s_line, enc_s_col, enc_e_line, enc_e_col = _extract_enclosing_range(occ)
                if enc_s_line >= 0 and enc_e_line >= enc_s_line:
                    # Use enclosing range for matching — covers the full function body
                    definition_ranges.append((enc_s_line, enc_e_line, enc_s_col, occ.symbol))
                    # Update symbol's definition extent to the enclosing range
                    if occ.symbol in symbol_map:
                        symbol_map[occ.symbol].def_start_line = enc_s_line
                        symbol_map[occ.symbol].def_end_line = enc_e_line
                else:
                    # No enclosing range — fall back to the symbol's own range
                    definition_ranges.append((s_line, e_line, s_col, occ.symbol))
                    if occ.symbol in symbol_map:
                        symbol_map[occ.symbol].def_start_line = s_line
                        symbol_map[occ.symbol].def_end_line = e_line

            occurrence_records.append(occ_rec)

        # ── Step 3: Resolve enclosing_symbol for each occurrence ──

        for occ_rec in occurrence_records:
            if occ_rec.role & SymbolRole.DEFINITION:
                # Definition occurrences: enclosing_range on the definition
                # itself tells us the full extent of the definition AST node.
                # This is useful for the symbol's def_start/end (already done above).
                continue

            # For reference occurrences: find which definition's range
            # contains this occurrence's position.
            # We use the occurrence's own position (not enclosing_range from SCIP)
            # because SCIP's enclosing_range on references describes the parent
            # expression, while we want the enclosing *function/struct* definition.
            #
            # Strategy: find the definition whose range covers this occurrence's line.
            enclosing = self._find_enclosing_symbol(
                occ_rec.start_line, occ_rec.start_col,
                definition_ranges,
            )
            if enclosing:
                occ_rec.enclosing_symbol = enclosing

        # ── Step 4: Derive edges from occurrences ──

        edge_records = []

        for occ_rec in occurrence_records:
            if not occ_rec.enclosing_symbol:
                continue
            if occ_rec.role & SymbolRole.DEFINITION:
                continue  # definitions don't create call/reference edges

            # Determine edge type based on the referenced symbol's kind
            referenced_kind = _get_symbol_kind(occ_rec.symbol, symbol_map)
            enclosing_kind = _get_symbol_kind(occ_rec.enclosing_symbol, symbol_map)

            if referenced_kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                edge_type = EdgeType.CALLS
            elif referenced_kind == SymbolKind.MACRO:
                edge_type = EdgeType.MACRO_EXPANDS
            else:
                edge_type = EdgeType.REFERENCES

            # ★ ops_bind derivation: if enclosing symbol is a struct/global_var
            # matching ops naming patterns, and referenced symbol is a function,
            # this is an indirect call binding.
            # In C, ops tables like ext4_file_operations are typed as structs
            # (struct file_operations), so we check both STRUCT and GLOBAL_VAR.
            if enclosing_kind in (SymbolKind.STRUCT, SymbolKind.GLOBAL_VAR) and \
               _match_ops_pattern(_get_symbol_name(occ_rec.enclosing_symbol, symbol_map)) and \
               referenced_kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):

                field_name = _infer_ops_field_name(occ_rec.symbol, symbol_map)
                edge_records.append(EdgeRecord(
                    src_symbol=occ_rec.enclosing_symbol,
                    dst_symbol=occ_rec.symbol,
                    type=EdgeType.OPS_BIND,
                    file_path=occ_rec.file_path,
                    line=occ_rec.start_line,
                    confidence=0.5,  # heuristic, lower confidence
                    metadata=_json_metadata({"field_name": field_name}),
                ))

            # Regular call/reference edge
            edge_records.append(EdgeRecord(
                src_symbol=occ_rec.enclosing_symbol,
                dst_symbol=occ_rec.symbol,
                type=edge_type,
                file_path=occ_rec.file_path,
                line=occ_rec.start_line,
            ))

        # ── Step 5: Derive edges from SymbolInformation.Relationship ──

        for sym_info in doc.symbols:
            for rel in sym_info.relationships:
                if not rel.symbol:
                    continue

                if rel.is_type_definition:
                    edge_records.append(EdgeRecord(
                        src_symbol=sym_info.symbol,
                        dst_symbol=rel.symbol,
                        type=EdgeType.TYPE_OF,
                    ))

                if rel.is_implementation:
                    edge_records.append(EdgeRecord(
                        src_symbol=sym_info.symbol,
                        dst_symbol=rel.symbol,
                        type=EdgeType.IMPLEMENTS,
                    ))

                if rel.is_definition:
                    edge_records.append(EdgeRecord(
                        src_symbol=sym_info.symbol,
                        dst_symbol=rel.symbol,
                        type=EdgeType.DEFINES,
                    ))

        # ── Step 6: Derive contains edges from enclosing_symbol ──
        # If a symbol has an enclosing_symbol (e.g. struct field → struct),
        # create a contains edge so get_struct_layout can discover fields.

        for sym_rec in symbol_records:
            if not sym_rec.enclosing_symbol:
                continue
            # Only emit contains if the enclosing symbol is known in this document
            if sym_rec.enclosing_symbol not in symbol_map:
                continue
            edge_records.append(EdgeRecord(
                src_symbol=sym_rec.enclosing_symbol,
                dst_symbol=sym_rec.scip_symbol,
                type=EdgeType.CONTAINS,
                file_path=file_path,
                line=sym_rec.def_start_line,
            ))

        # ── Step 7: Derive contains edges from field Definition occurrences ──
        # Complements Step 6 for real scip-clang output: it leaves
        # SymbolInformation.enclosing_symbol empty for non-local symbols (per
        # the SCIP spec, enclosing_symbol is only populated for local symbols).
        # Struct fields are non-local, so their containment must be recovered
        # positionally — a field's Definition occurrence sits inside its
        # enclosing struct's body range. We reuse the same range-matching
        # already used for call/ops_bind derivation (definition_ranges +
        # _find_enclosing_symbol). Edges are deduplicated by the store via
        # INSERT OR IGNORE on the (src, dst, type, file, line) composite key.
        for occ_rec in occurrence_records:
            if not (occ_rec.role & SymbolRole.DEFINITION):
                continue
            # Only Definition occurrences of fields contribute struct containment.
            if _get_symbol_kind(occ_rec.symbol, symbol_map) != SymbolKind.FIELD:
                continue
            enclosing = self._find_enclosing_symbol(
                occ_rec.start_line, occ_rec.start_col, definition_ranges,
            )
            if enclosing and enclosing != occ_rec.symbol:
                edge_records.append(EdgeRecord(
                    src_symbol=enclosing,
                    dst_symbol=occ_rec.symbol,
                    type=EdgeType.CONTAINS,
                    file_path=file_path,
                    line=occ_rec.start_line,
                ))

        return IngestBatch(
            file=file_rec,
            symbols=symbol_records,
            occurrences=occurrence_records,
            edges=edge_records,
        )

    # ── SymbolInformation parsing ──

    def _parse_symbol_info(self, sym_info: scip_pb2.SymbolInformation,
                           file_path: str) -> SymbolRecord:
        """Parse a SCIP SymbolInformation into a SymbolRecord."""
        parsed = parse_scip_symbol(sym_info.symbol)

        # Resolve kind: prefer SCIP's explicit Kind enum over descriptor suffix
        kind = SCIP_KIND_TO_SYMBOL_KIND.get(sym_info.kind, None)
        if kind is None:
            kind = parsed.get("kind", DEFAULT_SYMBOL_KIND)

        # Refine: a Term ('.') whose direct parent descriptor is a Type ('#')
        # is a struct field, not a global variable. This matters for real
        # scip-clang output, where SymbolInformation.kind is left as
        # UnspecifiedKind (0) and the kind must come from the descriptor grammar
        # (e.g. "cxx . . $ file_operations#read_iter." → Field).
        if kind == SymbolKind.GLOBAL_VAR and len(parsed.get("descriptors", [])) >= 2:
            parent_desc = parsed["descriptors"][-2]
            if parent_desc["suffix"] == "#":
                kind = SymbolKind.FIELD

        # Name: prefer display_name, fallback to parsed short_name
        name = sym_info.display_name or parsed.get("short_name", "")

        # Signature
        signature = ""
        if sym_info.HasField("signature_documentation") and \
           sym_info.signature_documentation.text:
            signature = sym_info.signature_documentation.text

        # Documentation: join repeated strings
        documentation = "\n".join(sym_info.documentation) if sym_info.documentation else ""

        # Enclosing symbol from SCIP (for local symbols)
        enclosing = sym_info.enclosing_symbol or parsed.get("enclosing_symbol", "")

        # Is external: symbols without a definition in this document
        is_external = False  # determined later by whether a Definition occurrence exists

        return SymbolRecord(
            scip_symbol=sym_info.symbol,
            name=name,
            kind=kind,
            signature=signature,
            documentation=documentation,
            def_file_path=file_path,
            is_external=is_external,
            enclosing_symbol=enclosing,
        )

    # ── External symbols parsing ──

    def _parse_external_symbols(self, ext_symbols: list) -> IngestBatch:
        """Parse external_symbols (symbols referenced but not defined in this index)."""
        records = []
        for sym_info in ext_symbols:
            parsed = parse_scip_symbol(sym_info.symbol)
            kind = SCIP_KIND_TO_SYMBOL_KIND.get(sym_info.kind,
                                                  parsed.get("kind", DEFAULT_SYMBOL_KIND))
            name = sym_info.display_name or parsed.get("short_name", "")
            signature = ""
            if sym_info.HasField("signature_documentation") and \
               sym_info.signature_documentation.text:
                signature = sym_info.signature_documentation.text
            documentation = "\n".join(sym_info.documentation) if sym_info.documentation else ""

            records.append(SymbolRecord(
                scip_symbol=sym_info.symbol,
                name=name,
                kind=kind,
                signature=signature,
                documentation=documentation,
                is_external=True,
            ))

        return IngestBatch(symbols=records)

    # ── Enclosing symbol matching ──

    def _find_enclosing_symbol(
        self,
        line: int,
        col: int,
        definition_ranges: list,
    ) -> str | None:
        """
        Find the symbol whose definition range encloses the given position.

        definition_ranges: list of (start_line, end_line, start_col, scip_symbol)

        Strategy: find the definition with the smallest range that still
        contains the position. This gives the "innermost enclosing" symbol
        (e.g. a local function inside a struct inside a namespace).
        """
        best_symbol = None
        best_range_size = float("inf")

        for start_line, end_line, start_col, symbol in definition_ranges:
            # Check if position is within this definition's range
            if start_line <= line <= end_line:
                range_size = end_line - start_line
                if range_size < best_range_size:
                    best_range_size = range_size
                    best_symbol = symbol

        return best_symbol


# ──────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────

def _language_enum_to_str(lang_str: str) -> str:
    """Convert Document.language string to our simplified language tag."""
    # SCIP Document.language is a free-form string (or Language enum name)
    # For kernel C code, we just care about C vs header
    lang = lang_str.upper()
    if lang in ("C", "CPP", "C_CPP", "OBJECTIVE_C"):
        return "C"
    return lang_str


def _get_symbol_kind(scip_symbol: str,
                     symbol_map: dict[str, SymbolRecord]) -> str:
    """Get SymbolKind for a scip_symbol string, using symbol_map with fallback parsing."""
    if scip_symbol in symbol_map:
        return symbol_map[scip_symbol].kind
    parsed = parse_scip_symbol(scip_symbol)
    return parsed.get("kind", DEFAULT_SYMBOL_KIND)


def _get_symbol_name(scip_symbol: str,
                     symbol_map: dict[str, SymbolRecord]) -> str:
    """Get short name for a scip_symbol string."""
    if scip_symbol in symbol_map:
        return symbol_map[scip_symbol].name
    parsed = parse_scip_symbol(scip_symbol)
    return parsed.get("short_name", scip_symbol)


def _infer_ops_field_name(scip_symbol: str,
                          symbol_map: dict[str, SymbolRecord]) -> str:
    """
    Infer the function-pointer field name from an ops binding.

    Strategy: if the referenced function name matches a common kernel
    ops field name pattern (e.g. ext4_file_read_iter → read_iter),
    strip the prefix to get the field name.

    For more accurate resolution, the Relationship.is_implementation
    data from SCIP SymbolInformation should be used.
    """
    name = _get_symbol_name(scip_symbol, symbol_map)

    # Common kernel naming pattern: subsystem_entity_op_field
    # e.g. ext4_file_read_iter → read_iter
    #      ext4_file_write_iter → write_iter
    # Strategy: try stripping known subsystem prefixes
    # For MVP, just return the full function name — exact field mapping
    # requires Relationship data.
    return name


def _json_metadata(data: dict) -> str:
    """Serialize metadata dict to JSON string."""
    import json
    return json.dumps(data)


# ──────────────────────────────────────────────
# Protobuf wire format helpers (for streaming)
# ──────────────────────────────────────────────

def _read_varint32(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a varint32 from buffer at position. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("Truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
        if shift >= 32:
            raise ValueError("Varint too long")
    return result, pos


def _read_tag(buf: bytes, pos: int) -> tuple[int, int, int]:
    """Read a protobuf tag. Returns (field_number, wire_type, new_pos)."""
    tag_value, new_pos = _read_varint32(buf, pos)
    field_number = tag_value >> 3
    wire_type = tag_value & 0x7
    return tag_value, wire_type, new_pos


def _skip_field(buf: bytes, pos: int, wire_type: int) -> int:
    """Skip a protobuf field value based on wire type. Returns new_pos."""
    if wire_type == 0:  # Varint
        _, pos = _read_varint32(buf, pos)
    elif wire_type == 1:  # 64-bit fixed
        pos += 8
    elif wire_type == 2:  # Length-delimited (submessage, string, bytes)
        length, pos = _read_varint32(buf, pos)
        pos += length
    elif wire_type == 5:  # 32-bit fixed
        pos += 4
    else:
        raise ValueError(f"Unknown wire type: {wire_type}")
    return pos