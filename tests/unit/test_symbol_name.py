"""
Unit tests for src/parser/symbol_name.py — parse_scip_symbol.

These tests use the REAL scip-clang symbol scheme ("cxx . . $ <descriptors>"),
which differs from the legacy synthetic format used elsewhere. They exist to
guard against regressions in package-header splitting: a prior bug caused the
4-field header ("scheme manager name version") to be mis-split, producing
garbage enclosing_symbol strings and breaking struct-field containment.
"""

import sys
from pathlib import Path

# tests/conftest.py already puts src/ and scripts/ on sys.path.
from parser.symbol_name import parse_scip_symbol
from parser.models import SymbolKind


# ── Real scip-clang scheme ("cxx . . $") ──

class TestRealScheme:
    """parse_scip_symbol must handle the real cxx . . $ scheme correctly."""

    def test_struct(self):
        p = parse_scip_symbol("cxx . . $ file_operations#")
        assert p["short_name"] == "file_operations"
        assert p["kind"] == SymbolKind.STRUCT
        assert p["enclosing_symbol"] == ""  # top-level type has no enclosing

    def test_field(self):
        p = parse_scip_symbol("cxx . . $ file_operations#read_iter.")
        assert p["short_name"] == "read_iter"
        # Descriptor suffix "." maps to GLOBAL_VAR at the parser-name layer;
        # the FIELD refinement lives in scip_parser._parse_symbol_info.
        assert p["kind"] == SymbolKind.GLOBAL_VAR
        # ★ the critical assertion: enclosing must be the struct symbol, intact
        assert p["enclosing_symbol"] == "cxx . . $ file_operations#"

    def test_function(self):
        p = parse_scip_symbol("cxx . . $ ext4_file_read_iter().")
        assert p["short_name"] == "ext4_file_read_iter"
        assert p["kind"] == SymbolKind.FUNCTION
        assert p["enclosing_symbol"] == ""  # file-scope function

    def test_nested_anonymous_struct_field(self):
        # Fields under nested anonymous types (common in kernel unions/unions).
        p = parse_scip_symbol("cxx . . $ __sifields#$anonymous_type_0#_pid.")
        assert p["short_name"] == "_pid"
        # direct enclosing is the innermost anonymous type
        assert p["enclosing_symbol"] == "cxx . . $ __sifields#$anonymous_type_0#"

    def test_typedef_term(self):
        p = parse_scip_symbol("cxx . . $ loff_t.")
        assert p["short_name"] == "loff_t"
        assert p["enclosing_symbol"] == ""  # top-level term

    def test_scheme_and_package_preserved(self):
        p = parse_scip_symbol("cxx . . $ file_operations#read_iter.")
        assert p["scheme"] == "cxx"
        assert p["package"] == ". . $"

    def test_descriptors_list_shape(self):
        p = parse_scip_symbol("cxx . . $ file_operations#read_iter.")
        descs = p["descriptors"]
        assert len(descs) == 2
        assert descs[0]["name"] == "file_operations"
        assert descs[0]["suffix"] == "#"
        assert descs[1]["name"] == "read_iter"
        assert descs[1]["suffix"] == "."


# ── Enclosing round-trip ──

class TestEnclosingRoundTrip:
    """The enclosing_symbol must itself be re-parseable (round-trip)."""

    def test_field_enclosing_reparses_as_struct(self):
        p = parse_scip_symbol("cxx . . $ file_operations#read_iter.")
        enc = parse_scip_symbol(p["enclosing_symbol"])
        assert enc["short_name"] == "file_operations"
        assert enc["kind"] == SymbolKind.STRUCT


# ── Edge cases / graceful degradation ──

class TestEdgeCases:
    def test_local_symbol(self):
        p = parse_scip_symbol("local 42")
        assert p["short_name"] == "42"
        assert p["kind"] == SymbolKind.VARIABLE
        assert p["enclosing_symbol"] == ""

    def test_empty(self):
        assert parse_scip_symbol("") == {}

    def test_truncated_header_does_not_raise(self):
        # Fewer than the 4 required leading fields — must degrade gracefully.
        for s in ("cxx", "cxx .", "cxx . .", "cxx . . $"):
            p = parse_scip_symbol(s)
            assert p["descriptors"] == []
            assert p["enclosing_symbol"] == ""

    def test_malformed_no_crash(self):
        p = parse_scip_symbol("malformed")
        assert p["short_name"] == ""
        assert p["kind"] == SymbolKind.GLOBAL_VAR


# ── Legacy synthetic scheme (regression: must still not crash) ──

class TestLegacySyntheticScheme:
    """The legacy 'scip clang c linux v6.12 ...' format used in integration
    fixtures must continue to parse without raising, even though its header
    does not match the 4-field canonical layout."""

    def test_synthetic_struct(self):
        p = parse_scip_symbol("scip clang c linux v6.12 ext4_file_operations#")
        assert p["scheme"] == "scip"
        # short_name should still be recoverable
        assert p["short_name"] == "ext4_file_operations"

    def test_synthetic_function(self):
        p = parse_scip_symbol("scip clang c linux v6.12 ext4_file_read_iter().")
        assert p["short_name"] == "ext4_file_read_iter"
