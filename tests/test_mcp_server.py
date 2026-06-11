"""
KGraph MCP server — smoke test.

Loads server.py against the real kernel kgraph.db and exercises
every tool, printing results. Run from anywhere:

    KGRAPH_DB=/path/to/linux/.kgraph/kgraph.db \
    KGRAPH_ROOT=/path/to/linux \
    .venv/bin/python tests/test_mcp_server.py
"""

import importlib.util
import os
import sys
from pathlib import Path

# Resolve the kernel index from KGRAPH_ROOT (defaults to cwd). Run this
# test from the kernel source dir, or set KGRAPH_ROOT/KGRAPH_DB explicitly.
_root = Path(os.environ.get("KGRAPH_ROOT", Path.cwd()))
os.environ.setdefault("KGRAPH_ROOT", str(_root))
os.environ.setdefault("KGRAPH_DB", str(_root / ".kgraph" / "kgraph.db"))

SERVER_PY = Path(__file__).resolve().parent.parent / "mcp" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("kgraph_server", SERVER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    m = load_server()

    print("=" * 60)
    print("KGraph MCP server — tool smoke test")
    print("=" * 60)

    tests = [
        ("index_status", lambda: m.index_status()),
        ("get_symbol(ext4_file_read_iter)",
         lambda: m.get_symbol("ext4_file_read_iter")),
        ("get_function_body(ext4_file_read_iter)",
         lambda: m.get_function_body("ext4_file_read_iter")),
        ("find_callees(ext4_file_read_iter)",
         lambda: m.find_callees("ext4_file_read_iter", limit=10)),
        ("find_callers(generic_file_read_iter)",
         lambda: m.find_callers("generic_file_read_iter", limit=10)),
        ("find_references(ext4_file_read_iter)",
         lambda: m.find_references("ext4_file_read_iter", limit=10)),
        ("find_ops_impls(read_iter)",
         lambda: m.find_ops_impls("read_iter")),
        ("search_symbols(ext4_file)",
         lambda: m.search_symbols("ext4_file", limit=5)),
        ("get_neighborhood(ext4_file_read_iter)",
         lambda: m.get_neighborhood("ext4_file_read_iter", depth=1)),
    ]

    for title, fn in tests:
        print(f"\n{'─' * 60}")
        print(f"▶ {title}")
        print("─" * 60)
        try:
            result = fn()
            # Truncate long output
            if len(result) > 1000:
                result = result[:1000] + "\n  ... (truncated)"
            print(result)
        except Exception as e:
            print(f"  ❌ ERROR: {e}")

    print(f"\n{'=' * 60}")
    print("✅ Smoke test complete")
    print("=" * 60)


if __name__ == "__main__":
    main()