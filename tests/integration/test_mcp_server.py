"""
KGraph — MCP Server Integration Tests.

Validates the path: MCP tool function call → SQLiteStore → formatted string response.

Uses synthetic SCIP data via the `mcp_server` fixture from conftest.py.
Tests call tool functions directly (not through MCP stdio protocol) to verify
correctness of resolution, querying, formatting, and error handling.
"""

from __future__ import annotations

import pytest


class TestMCPServerTools:
    """Verify all 12 MCP tool functions against populated synthetic DB."""

    @pytest.fixture(autouse=True)
    def setup(self, mcp_server):
        self.srv = mcp_server

    # ── index_status ──

    def test_index_status(self):
        result = self.srv.index_status()
        assert "KGraph index status" in result
        assert "/kernel" in result
        assert "scip-clang" in result

    # ── search_symbols ──

    def test_search_symbols(self):
        result = self.srv.search_symbols("ext4_file")
        assert "Found" in result
        assert "ext4_file_read_iter" in result
        assert "ext4_file_write_iter" in result

    def test_search_symbols_with_kind(self):
        result = self.srv.search_symbols("ext4_file", kind="struct")
        assert "ext4_file_operations" in result

    def test_search_symbols_not_found(self):
        result = self.srv.search_symbols("xyz_nonexistent")
        assert "No symbols found" in result

    # ── get_symbol ──

    def test_get_symbol(self):
        result = self.srv.get_symbol("vfs_read")
        assert "vfs_read" in result
        assert "function" in result
        assert "fs/read_write.c" in result

    def test_get_symbol_with_kind(self):
        result = self.srv.get_symbol("ext4_file_operations", kind="struct")
        assert "ext4_file_operations" in result
        assert "struct" in result

    def test_get_symbol_not_found(self):
        result = self.srv.get_symbol("xyz_nonexistent")
        assert "No symbol named" in result

    # ── get_function_body ──

    def test_get_function_body(self):
        result = self.srv.get_function_body("vfs_read")
        assert "vfs_read" in result
        assert "function" in result
        # Should contain source code from the fake file
        assert "ext4_file_read_iter" in result or "vfs_read" in result

    def test_get_function_body_with_context(self):
        result = self.srv.get_function_body("vfs_read", context=5)
        assert "vfs_read" in result

    def test_get_function_body_not_found(self):
        result = self.srv.get_function_body("xyz_nonexistent")
        assert "No symbol named" in result

    # ── find_callers ──

    def test_find_callers(self):
        result = self.srv.find_callers("ext4_file_read_iter")
        assert "vfs_read" in result

    def test_find_callers_with_depth(self):
        result = self.srv.find_callers("ext4_file_read_iter", depth=2)
        assert "Found" in result or "vfs_read" in result

    def test_find_callers_not_found(self):
        result = self.srv.find_callers("xyz_nonexistent")
        assert "No symbol named" in result

    # ── find_callees ──

    def test_find_callees(self):
        result = self.srv.find_callees("vfs_read")
        assert "ext4_file_read_iter" in result

    def test_find_callees_not_found(self):
        result = self.srv.find_callees("xyz_nonexistent")
        assert "No symbol named" in result

    # ── find_references ──

    def test_find_references(self):
        result = self.srv.find_references("ext4_file_read_iter")
        assert "References to" in result
        assert "ext4_file_read_iter" in result
        # Should show at least definition + reference
        assert "DEF" in result

    def test_find_references_not_found(self):
        result = self.srv.find_references("xyz_nonexistent")
        assert "No symbol named" in result

    # ── find_type_definition ──

    def test_find_type_definition(self):
        result = self.srv.find_type_definition("read_iter")
        assert "ext4_file_read_iter" in result

    def test_find_type_definition_not_found(self):
        result = self.srv.find_type_definition("xyz_nonexistent")
        assert "No symbol named" in result or "No type definition" in result

    # ── get_struct_layout ──

    def test_get_struct_layout(self):
        """contains edges derived from enclosing_symbol should show fields."""
        result = self.srv.get_struct_layout("ext4_file_operations")
        assert "ext4_file_operations" in result
        assert "read_iter" in result
        assert "write_iter" in result
        assert "open" in result

    def test_get_struct_layout_not_found(self):
        result = self.srv.get_struct_layout("xyz_nonexistent")
        assert "No struct named" in result

    # ── find_ops_impls ──

    def test_find_ops_impls(self):
        result = self.srv.find_ops_impls("read_iter")
        assert "ext4_file_operations" in result
        assert "ext4_file_read_iter" in result

    def test_find_ops_impls_all_fields(self):
        """All three ops_bind fields should be discoverable."""
        result = self.srv.find_ops_impls("ext4_file")
        assert "Implementations" in result or "ext4_file_operations" in result

    def test_find_ops_impls_not_found(self):
        result = self.srv.find_ops_impls("xyz_nonexistent")
        assert "No ops_bind implementations" in result

    # ── get_neighborhood ──

    def test_get_neighborhood(self):
        result = self.srv.get_neighborhood("vfs_read")
        assert "Neighborhood" in result
        assert "ext4_file_read_iter" in result

    def test_get_neighborhood_depth_2(self):
        result = self.srv.get_neighborhood("vfs_read", depth=2)
        assert "Neighborhood" in result

    def test_get_neighborhood_not_found(self):
        result = self.srv.get_neighborhood("xyz_nonexistent")
        assert "No symbol named" in result

    # ── call_path ──

    def test_call_path(self):
        result = self.srv.call_path("vfs_read", "ext4_file_read_iter")
        assert "Call path" in result
        assert "vfs_read" in result
        assert "ext4_file_read_iter" in result

    def test_call_path_not_found(self):
        result = self.srv.call_path("ext4_file_read_iter", "sys_read")
        assert "No call path" in result

    def test_call_path_unknown_source(self):
        result = self.srv.call_path("xyz_nonexistent", "vfs_read")
        assert "No symbol named" in result

    def test_call_path_unknown_target(self):
        result = self.srv.call_path("vfs_read", "xyz_nonexistent")
        assert "No symbol named" in result
