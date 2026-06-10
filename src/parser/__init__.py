"""KGraph parser — SCIP protobuf → IngestBatch."""

from .models import (
    EdgeRecord,
    EdgeType,
    FileRecord,
    IngestBatch,
    MetadataRecord,
    OccurrenceRecord,
    SymbolKind,
    SymbolRecord,
    SymbolRole,
)
from .scip_parser import SCIPParser
from .symbol_name import parse_scip_symbol

__all__ = [
    "SCIPParser",
    "parse_scip_symbol",
    "IngestBatch",
    "SymbolRecord",
    "OccurrenceRecord",
    "EdgeRecord",
    "FileRecord",
    "MetadataRecord",
    "SymbolKind",
    "SymbolRole",
    "EdgeType",
]