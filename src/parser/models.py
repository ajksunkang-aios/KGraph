"""
KGraph — Compiler-Aware Kernel Graph Engine

Canonical data model for the code knowledge graph.
All components (parser, storage, query engine) operate on these types.
The parser produces them; the storage persists them; the query engine reads them.

This is the **stable contract** between parser and storage —
changing the storage backend (SQLite → Neo4j → custom) requires only
implementing a new GraphStore that consumes these same types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# ──────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────

class SymbolKind(str):
    """Node kind — maps from SCIP SymbolInformation.Kind enum."""
    FUNCTION = "function"
    STRUCT = "struct"
    FIELD = "field"
    MACRO = "macro"
    TYPEDEF = "typedef"
    GLOBAL_VAR = "global_var"
    ENUM = "enum"
    UNION = "union"
    MODULE = "module"
    INTERFACE = "interface"
    PARAMETER = "parameter"
    TYPE_PARAMETER = "type_parameter"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    VARIABLE = "variable"
    CONSTANT = "constant"
    PROPERTY = "property"


class EdgeType(str):
    """Edge kind — derived from SCIP occurrences + relationships."""
    CALLS = "calls"
    REFERENCES = "references"
    DEFINES = "defines"
    CONTAINS = "contains"
    INCLUDES = "includes"
    OPS_BIND = "ops_bind"        # ★ core differentiator: function-pointer table binding
    TYPE_OF = "type_of"
    MACRO_EXPANDS = "macro_expands"
    IMPLEMENTS = "implements"


class SymbolRole(IntEnum):
    """SCIP SymbolRole bitmask values."""
    DEFINITION = 0x1
    IMPORT = 0x2
    WRITE_ACCESS = 0x4
    READ_ACCESS = 0x8
    GENERATED = 0x10
    TEST = 0x20
    FORWARD_DEFINITION = 0x40


# ──────────────────────────────────────────────
# SCIP → SymbolKind mapping
# ──────────────────────────────────────────────

# Keys are SCIP SymbolInformation.Kind enum integer values.
# Only the kinds relevant to kernel C code are mapped.
SCIP_KIND_TO_SYMBOL_KIND = {
    17: SymbolKind.FUNCTION,       # Function
    26: SymbolKind.METHOD,         # Method
    9:  SymbolKind.CONSTRUCTOR,    # Constructor
    49: SymbolKind.STRUCT,         # Struct
    7:  SymbolKind.STRUCT,         # Class → struct in C
    21: SymbolKind.INTERFACE,      # Interface
    15: SymbolKind.FIELD,          # Field
    25: SymbolKind.MACRO,          # Macro
    55: SymbolKind.TYPEDEF,        # TypeAlias → typedef
    61: SymbolKind.VARIABLE,       # Variable
    8:  SymbolKind.CONSTANT,       # Constant → global_var
    11: SymbolKind.ENUM,           # Enum
    59: SymbolKind.UNION,          # Union
    29: SymbolKind.MODULE,         # Module
    37: SymbolKind.PARAMETER,      # Parameter
    58: SymbolKind.TYPE_PARAMETER, # TypeParameter
    41: SymbolKind.PROPERTY,       # Property
    60: SymbolKind.GLOBAL_VAR,     # Value → global_var
}

# Fallback: anything unmapped becomes global_var for C context
DEFAULT_SYMBOL_KIND = SymbolKind.GLOBAL_VAR


# ──────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────

@dataclass
class FileRecord:
    """A source file in the index."""
    path: str = ""
    language: str = "C"
    subsystem: str = ""
    sha: str = ""


@dataclass
class SymbolRecord:
    """A symbol node (function, struct, field, macro, etc.)."""
    scip_symbol: str          # SCIP globally-unique symbol string
    name: str                 # Short display name
    kind: str                 # SymbolKind value
    signature: str = ""
    documentation: str = ""
    def_file_path: str = ""   # Path of defining file (resolved later to file_id)
    def_start_line: int = -1
    def_end_line: int = -1
    is_external: bool = False
    subsystem: str = ""       # Written by KernelProfile.DomainEnrichment
    enclosing_symbol: str = ""  # SCIP symbol string of the enclosing symbol


@dataclass
class OccurrenceRecord:
    """A symbol occurrence (definition or reference) in a source file."""
    symbol: str               # SCIP symbol string
    file_path: str            # Source file path
    start_line: int = -1
    start_col: int = -1
    end_line: int = -1
    end_col: int = -1
    role: int = 0             # SymbolRole bitmask
    enclosing_symbol: str = ""  # ★ derived: which function/struct this reference sits in


@dataclass
class EdgeRecord:
    """A relationship edge between two symbols."""
    src_symbol: str           # SCIP symbol string of source
    dst_symbol: str           # SCIP symbol string of target
    type: str                 # EdgeType value
    file_path: str = ""       # Where this edge was observed
    line: int = -1
    weight: int = 1
    confidence: float = 1.0
    metadata: str = ""        # JSON string for edge-specific data


@dataclass
class MetadataRecord:
    """Index metadata (tool info, project root, etc.)."""
    key: str
    value: str


# ──────────────────────────────────────────────
# Batch: parser → storage transfer unit
# ──────────────────────────────────────────────

@dataclass
class IngestBatch:
    """
    A batch of parsed data ready for storage.

    The parser emits one batch per Document (source file).
    Each batch contains all symbols, occurrences, and edges
    derived from that single Document's SCIP data.

    This is the **transfer unit** between parser and storage:
    the parser fills it; the storage backend consumes it.
    """
    file: FileRecord = field(default_factory=FileRecord)
    symbols: list[SymbolRecord] = field(default_factory=list)
    occurrences: list[OccurrenceRecord] = field(default_factory=list)
    edges: list[EdgeRecord] = field(default_factory=list)
    metadata: list[MetadataRecord] = field(default_factory=list)