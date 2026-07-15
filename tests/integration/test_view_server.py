"""HTTP contract tests for the local visual explorer API."""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
from http.server import HTTPServer
from pathlib import Path

import pytest

from storage import SQLiteStore


REPO = Path(__file__).resolve().parents[2]
VFS_READ = "scip clang c linux v6.12 vfs_read()."


@pytest.fixture
def view_handler(populated_store: SQLiteStore, project_root: Path):
    spec = importlib.util.spec_from_file_location(
        "kgraph_view_server_test", REPO / "view" / "server.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    module._store = None
    module._DB_PATH = populated_store.db_path
    module._ROOT_PATH = project_root
    handler = object.__new__(module.Handler)
    response: dict = {}

    def send_json(obj, status=200):
        response["status"] = status
        response["body"] = obj

    def send_error(message, status=400):
        response["status"] = status
        response["body"] = {"error": message}

    handler._send_json = send_json
    handler._send_err = send_error
    try:
        yield handler, response
    finally:
        if module._store is not None:
            module._store.close()


def test_fragment_accepts_scip_id_and_returns_real_edges(view_handler):
    handler, response = view_handler
    handler._api("/api/fragment", {"scip": VFS_READ, "depth": "1"})
    fragment = response["body"]

    assert response["status"] == 200
    assert fragment["center_symbol"] == VFS_READ
    assert any(
        edge["source"] == VFS_READ
        and edge["type"] == "calls"
        for edge in fragment["edges"]
    )


def test_fragment_rejects_unknown_symbols_and_edge_types(view_handler):
    handler, response = view_handler
    handler._api("/api/fragment", {"scip": "scip clang c linux v6.12 nope()."})
    assert response["status"] == 404

    handler._api("/api/fragment", {"scip": VFS_READ, "edge_types": "nope"})
    assert response["status"] == 400


def test_global_network_endpoint_returns_directory_aggregates(view_handler):
    handler, response = view_handler
    handler._api("/api/global-network", {
        "edge_types": "calls,ops_bind", "include_internal": "1",
    })

    assert response["status"] == 200
    network = response["body"]
    assert network["scope"]["label"] == "Linux"
    assert any(node["id"] == "fs" for node in network["nodes"])
    assert network["edges"]

    handler._api("/api/global-network", {"prefix": "../fs"})
    assert response["status"] == 400


def test_file_symbols_endpoint_returns_file_definitions(view_handler):
    handler, response = view_handler
    handler._api("/api/file-symbols", {
        "path": "fs/read_write.c", "limit": "1", "offset": "0",
    })

    assert response["status"] == 200
    payload = response["body"]
    assert payload["file"]["path"] == "fs/read_write.c"
    assert len(payload["symbols"]) == 1
    assert payload["totals"]["symbols"] >= 2
    assert payload["truncated"]
    assert payload["offset"] == 0
    assert payload["next_offset"] == 1
    assert payload["limits"] == {"max_symbols": 1}
    assert {
        "scip_symbol", "name", "kind", "signature", "def_start_line",
        "def_end_line", "is_external",
    } <= payload["symbols"][0].keys()

    first_scip = payload["symbols"][0]["scip_symbol"]
    handler._api("/api/file-symbols", {
        "path": "fs/read_write.c", "limit": "1", "offset": "1",
    })
    assert response["status"] == 200
    assert response["body"]["offset"] == 1
    assert response["body"]["symbols"][0]["scip_symbol"] != first_scip

    handler._api("/api/file-symbols", {"path": "../fs/read_write.c"})
    assert response["status"] == 400

    handler._api("/api/file-symbols", {"path": "fs/read_write.c", "offset": "-1"})
    assert response["status"] == 400

    handler._api("/api/file-symbols", {"path": "fs/read_write.c", "offset": "later"})
    assert response["status"] == 400

    handler._api("/api/file-symbols", {
        "path": "fs/read_write.c", "offset": "1000001",
    })
    assert response["status"] == 400

    handler._api("/api/file-symbols", {"path": "fs/missing.c"})
    assert response["status"] == 404


def test_idle_tcp_client_times_out_before_blocking_follow_up_requests():
    """A cancelled/slow browser request must not wedge the single server loop."""
    spec = importlib.util.spec_from_file_location(
        "kgraph_view_server_idle_client_test", REPO / "view" / "server.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    # Keep the production contract explicit while using a shorter timeout below
    # so this regression test remains fast.
    assert module.Handler.timeout == module._HTTP_CLIENT_TIMEOUT_SECONDS
    assert module.Handler.timeout > 0

    first_client_accepted = threading.Event()

    class RecordingHandler(module.Handler):
        timeout = 0.1

        def setup(self):
            super().setup()
            first_client_accepted.set()

    try:
        server = HTTPServer(("127.0.0.1", 0), RecordingHandler)
    except PermissionError:
        pytest.skip("sandbox does not permit a local TCP listener")
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()

    idle_client = socket.create_connection(server.server_address, timeout=1)
    try:
        # The connection is accepted and its handler is now blocked reading the
        # missing request line.  The second connection therefore queues behind
        # it until the per-client socket timeout releases the serving loop.
        assert first_client_accepted.wait(timeout=1)

        with socket.create_connection(server.server_address, timeout=1) as client:
            client.settimeout(2)
            started = time.monotonic()
            client.sendall(
                b"GET /not-a-real-kgraph-static-file HTTP/1.0\r\n"
                b"Host: localhost\r\n\r\n"
            )
            response = bytearray()
            while True:
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    pytest.fail("an idle TCP client blocked the next request")
                if not chunk:
                    break
                response.extend(chunk)

        assert time.monotonic() - started < 1
        assert response.startswith(b"HTTP/1.0 404")
    finally:
        idle_client.close()
        server.shutdown()
        thread.join(timeout=1)
        server.server_close()
        assert not thread.is_alive()
