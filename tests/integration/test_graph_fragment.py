"""Contract tests for the bounded graph fragment used by KGraph View."""

from __future__ import annotations

import logging
import sqlite3

from parser.models import EdgeRecord, EdgeType, FileRecord, IngestBatch, SymbolKind, SymbolRecord
from storage import SQLiteStore


VFS_READ = "scip clang c linux v6.12 vfs_read()."
EXT4_OPS = "scip clang c linux v6.12 ext4_file_operations#"
EXT4_READ = "scip clang c linux v6.12 ext4_file_read_iter()."


def _edge(fragment: dict, source: str, target: str, edge_type: str) -> dict:
    return next(
        edge for edge in fragment["edges"]
        if edge["source"] == source
        and edge["target"] == target
        and edge["type"] == edge_type
    )


def test_fragment_preserves_direction_and_evidence(populated_store: SQLiteStore):
    fragment = populated_store.get_neighborhood(VFS_READ, depth=1)

    assert fragment["center_symbol"] == VFS_READ
    assert fragment["center"]["name"] == "vfs_read"
    call = _edge(fragment, VFS_READ, EXT4_READ, "calls")
    assert call["evidence"] == {"file_path": "fs/read_write.c", "line": 210}
    assert call["confidence"] == 1.0
    assert not fragment["truncated"]


def test_fragment_keeps_parallel_edge_types_and_metadata(populated_store: SQLiteStore):
    fragment = populated_store.get_neighborhood(
        EXT4_OPS, depth=1, edge_types=["calls", "ops_bind"],
    )

    calls = _edge(fragment, EXT4_OPS, EXT4_READ, "calls")
    ops_bind = _edge(fragment, EXT4_OPS, EXT4_READ, "ops_bind")
    assert calls["id"] != ops_bind["id"]
    assert calls["evidence"] == ops_bind["evidence"] == {
        "file_path": "fs/ext4/file.c", "line": 105,
    }
    assert ops_bind["confidence"] == 1.0
    assert ops_bind["metadata"]["inferred_field"] is True
    assert ops_bind["metadata"]["field_name"] == "ext4_file_read_iter"


def test_fragment_filters_and_reports_node_bound(populated_store: SQLiteStore):
    calls_only = populated_store.get_neighborhood(
        EXT4_OPS, depth=1, edge_types=["calls"],
    )
    assert calls_only["edges"]
    assert {edge["type"] for edge in calls_only["edges"]} == {"calls"}

    bounded = populated_store.get_neighborhood(VFS_READ, depth=1, max_nodes=1)
    assert bounded["nodes"] == []
    assert bounded["edges"] == []
    assert bounded["truncated"]
    assert bounded["truncation"]["nodes"] is True


def test_fragment_unknown_symbol_and_summary_keep_machine_ids(populated_store: SQLiteStore):
    missing = populated_store.get_neighborhood("scip clang c linux v6.12 missing().")
    assert missing["nodes"] == []
    assert missing["edges"] == []

    summary = populated_store.get_neighborhood(VFS_READ, depth=1, summary=True)
    assert summary["nodes"]
    assert all(node["scip_symbol"] for node in summary["nodes"])


def test_global_network_aggregates_real_edges_by_directory(populated_store: SQLiteStore):
    network = populated_store.get_global_network(
        edge_types=["calls", "ops_bind"], include_internal=True,
    )

    assert network["scope"]["prefix"] is None
    assert {node["id"] for node in network["nodes"]} >= {"fs", "include"}
    assert all(node["id"] != "@root" for node in network["nodes"])
    fs = next(node for node in network["nodes"] if node["id"] == "fs")
    assert fs["files"] >= 2
    assert fs["symbols"] >= 5
    assert fs["can_drill"] is True
    assert fs["is_file"] is False
    calls = next(
        edge for edge in network["edges"]
        if edge["source"] == "fs" and edge["target"] == "fs" and edge["type"] == "calls"
    )
    assert calls["relationships"] >= 2

    drilled = populated_store.get_global_network(
        prefix="fs", edge_types=["calls", "ops_bind"], include_internal=True,
    )
    assert drilled["scope"]["prefix"] == "fs"
    assert any(node["id"].startswith("fs/") for node in drilled["nodes"])
    assert drilled["totals"]["files"] >= 2
    file_node = next(node for node in drilled["nodes"] if node["id"] == "fs/read_write.c")
    assert file_node["is_file"] is True
    assert file_node["can_drill"] is False


def test_global_network_respects_node_bound(populated_store: SQLiteStore):
    extra_symbol = "scip clang c linux v6.12 global_map_test_source()."
    populated_store.write_batch(IngestBatch(
        file=FileRecord(path="net/core/global_map_test.c"),
        symbols=[SymbolRecord(
            scip_symbol=extra_symbol,
            name="global_map_test_source",
            kind=SymbolKind.FUNCTION,
        )],
        edges=[EdgeRecord(
            src_symbol=extra_symbol,
            dst_symbol=VFS_READ,
            type=EdgeType.CALLS,
            file_path="net/core/global_map_test.c",
        )],
    ))
    network = populated_store.get_global_network(max_nodes=2, max_edges=1)

    assert len(network["nodes"]) <= 2
    assert len(network["edges"]) <= 1
    assert network["truncated"]


def test_file_symbols_lists_concrete_indexed_definitions(populated_store: SQLiteStore):
    module_symbol = "scip clang c linux v6.12 <file>/fs/ext4/file.c"
    populated_store.write_batch(IngestBatch(
        file=FileRecord(path="fs/ext4/file.c"),
        symbols=[SymbolRecord(
            scip_symbol=module_symbol,
            name="fs/ext4/file.c",
            kind=SymbolKind.MODULE,
        )],
    ))
    result = populated_store.get_file_symbols("fs/ext4/file.c")

    assert result is not None
    assert result["file"]["path"] == "fs/ext4/file.c"
    assert result["totals"]["symbols"] >= 4
    by_name = {symbol["name"]: symbol for symbol in result["symbols"]}
    read_iter = by_name["ext4_file_read_iter"]
    assert read_iter["scip_symbol"] == EXT4_READ
    assert read_iter["kind"] == "function"
    assert read_iter["signature"]
    assert read_iter["def_start_line"] >= 0
    assert read_iter["def_end_line"] >= read_iter["def_start_line"]
    assert read_iter["is_external"] is False
    assert module_symbol not in {symbol["scip_symbol"] for symbol in result["symbols"]}
    assert all(symbol["kind"] != SymbolKind.MODULE for symbol in result["symbols"])
    assert not result["truncated"]
    assert populated_store.get_file_symbols("fs/missing.c") is None


def test_file_symbols_reports_bound_and_truncation(populated_store: SQLiteStore):
    first_page = populated_store.get_file_symbols("fs/ext4/file.c", limit=1)

    assert first_page is not None
    assert len(first_page["symbols"]) == 1
    assert first_page["totals"]["symbols"] > 1
    assert first_page["offset"] == 0
    assert first_page["truncated"]
    assert first_page["next_offset"] == 1
    assert first_page["limits"] == {"max_symbols": 1}

    second_page = populated_store.get_file_symbols(
        "fs/ext4/file.c", limit=1, offset=first_page["next_offset"],
    )
    assert second_page is not None
    assert second_page["offset"] == 1
    assert second_page["symbols"][0]["scip_symbol"] != first_page["symbols"][0]["scip_symbol"]

    final_page = populated_store.get_file_symbols(
        "fs/ext4/file.c", offset=first_page["totals"]["symbols"],
    )
    assert final_page is not None
    assert final_page["symbols"] == []
    assert final_page["truncated"] is False
    assert final_page["next_offset"] is None


def test_file_symbols_index_supports_existing_databases(tmp_path):
    db_path = tmp_path / "pre_file_symbols_index.db"
    bootstrap = SQLiteStore(db_path)
    bootstrap.create_schema()
    bootstrap.conn.execute("DROP INDEX idx_symbols_def_file_line_name")
    bootstrap.conn.commit()
    bootstrap.close()

    reopened = SQLiteStore(db_path)
    try:
        indexes = {
            row["name"]
            for row in reopened.conn.execute("PRAGMA index_list('symbols')").fetchall()
        }
        assert "idx_symbols_def_file_line_name" in indexes
    finally:
        reopened.close()


def test_file_symbols_index_migration_failure_is_nonfatal(caplog):
    class _Result:
        def fetchone(self):
            return (1,)

    class _LockedConnection:
        def __init__(self):
            self.rolled_back = False

        def execute(self, _sql):
            return _Result()

        def commit(self):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        def rollback(self):
            self.rolled_back = True

    store = object.__new__(SQLiteStore)
    store.conn = _LockedConnection()
    with caplog.at_level(logging.WARNING, logger="storage.sqlite_store"):
        store._ensure_file_symbols_index()

    assert store.conn.rolled_back
    assert "falling back to scan" in caplog.text


def test_file_symbols_query_uses_file_order_index(populated_store: SQLiteStore):
    file_id = populated_store.conn.execute(
        "SELECT id FROM files WHERE path = ?", ("fs/ext4/file.c",),
    ).fetchone()["id"]
    plan = populated_store.conn.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT s.scip_symbol, s.name, s.kind, s.signature,
               s.def_start_line, s.def_end_line, s.is_external
        FROM symbols s
        WHERE s.def_file_id = ? AND s.kind <> ?
        ORDER BY s.def_start_line, s.name, s.scip_symbol
        LIMIT ? OFFSET ?
        """,
        (file_id, SymbolKind.MODULE, 1, 0),
    ).fetchall()
    assert any(
        "idx_symbols_def_file_line_name" in row["detail"]
        for row in plan
    )
