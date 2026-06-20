"""
KGraph CLI — `kgraph init` command.

Orchestrates the full index pipeline:
  1. Pre-checks (compile_commands.json, existing artifacts)
  2. Run scip-clang → index.scip (skip if already exists unless --force)
  3. Parse index.scip → SQLite (.kgraph/kgraph.db)
  4. Print summary

Usage:
    kgraph init /path/to/kernel
    kgraph init . --force
    kgraph init . --skip-build
    kgraph init . --scip-clang /path/to/scip-clang
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Make parent packages importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # src/
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from parser import SCIPParser  # noqa: E402
from storage import SQLiteStore  # noqa: E402


# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

_THIRDPARTY_SCIP_CLANG = Path(__file__).resolve().parent.parent.parent / "thirdparty" / "scip-clang"

# Size threshold for choosing stream vs full-load mode (500 MB)
_STREAM_THRESHOLD = 500 * 1024 * 1024


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m" if sys.stdout.isatty() else text

def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m" if sys.stdout.isatty() else text

def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if sys.stdout.isatty() else text

def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m" if sys.stdout.isatty() else text

def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if sys.stdout.isatty() else text


def _find_scip_clang(custom_path: str | None) -> str:
    """Locate the scip-clang binary.

    Priority:
      1. --scip-clang argument
      2. thirdparty/scip-clang in the KGraph repo (bundled with npm)
      3. PATH lookup
    """
    if custom_path:
        p = Path(custom_path)
        if p.is_file():
            return str(p.resolve())
        print(_red(f"ERROR: --scip-clang not found: {custom_path}"))
        sys.exit(1)

    # Bundled binary (from npm or repo)
    if _THIRDPARTY_SCIP_CLANG.is_file():
        return str(_THIRDPARTY_SCIP_CLANG.resolve())

    # PATH lookup
    found = shutil.which("scip-clang")
    if found:
        return found

    print(_red("ERROR: scip-clang not found."))
    print("  Install options:")
    print("    1. npm install -g @ajksunkang-aios/kgraph  (includes scip-clang)")
    print("    2. Download from https://github.com/sourcegraph/scip-clang/releases")
    print("    3. Pass --scip-clang /path/to/scip-clang")
    sys.exit(1)


def _run_scip_clang(scip_clang: str, compdb_path: Path, output_dir: Path) -> Path:
    """Run scip-clang to produce index.scip.

    Returns the path to the produced index.scip.
    """
    scip_output = output_dir / "index.scip"
    cmd = [scip_clang, "--compdb-path", str(compdb_path)]

    print(_bold("Running scip-clang..."))
    print(f"  binary:  {scip_clang}")
    print(f"  compdb:  {compdb_path}")
    print(f"  output:  {scip_output}")
    print(_dim("  streaming scip-clang progress below — indexing is the long step, not a hang"))
    print()

    t0 = time.time()
    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(output_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr in → all progress streams live
            text=True,
            bufsize=1,                # line-buffered
        )
    except FileNotFoundError:
        print(_red(f"ERROR: Cannot execute scip-clang: {scip_clang}"))
        print("  scip-clang is a Linux x86-64 binary. Are you running on Linux?")
        sys.exit(1)

    # Forward scip-clang's output line-by-line so the user sees indexing
    # progress instead of a silent hang (scip-clang logs progress to stderr,
    # merged into stdout above). Also accumulate it for error reporting.
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            sys.stdout.write(f"  {line}")
            sys.stdout.flush()
            captured.append(line)
        proc.wait(timeout=3600)  # 1 hour max
    except subprocess.TimeoutExpired:
        proc.kill()
        print(_red("ERROR: scip-clang timed out after 1 hour."))
        sys.exit(1)

    elapsed = time.time() - t0
    output = "".join(captured)

    if proc.returncode != 0:
        print(_red(f"ERROR: scip-clang exited with code {proc.returncode}"))
        if output:
            for line in output.strip().split("\n")[-20:]:
                print(f"  {line}")
        sys.exit(1)

    if not scip_output.exists():
        print(_red("ERROR: scip-clang completed but index.scip was not produced."))
        if output:
            for line in output.strip().split("\n")[-10:]:
                print(f"  {line}")
        sys.exit(1)

    size_mb = scip_output.stat().st_size / 1024 / 1024
    print(f"  {_green('✓')} index.scip produced ({size_mb:.1f} MB) in {elapsed:.1f}s")
    return scip_output


def _ingest(scip_path: Path, db_path: Path) -> None:
    """Parse index.scip and store into SQLite."""
    size_mb = scip_path.stat().st_size / 1024 / 1024
    use_stream = scip_path.stat().st_size >= _STREAM_THRESHOLD

    print(_bold("Parsing index.scip → SQLite..."))
    print(f"  index:   {scip_path} ({size_mb:.1f} MB)")
    print(f"  mode:    {'stream' if use_stream else 'full-load'}")
    print(f"  db:      {db_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    parser = SCIPParser(scip_path)
    store = SQLiteStore(db_path)
    store.create_schema()

    total_symbols = 0
    total_occurrences = 0
    total_edges = 0
    total_files = 0
    batch_count = 0

    parse_fn = parser.parse_stream if use_stream else parser.parse
    t0 = time.time()

    for batch in parse_fn():
        batch_count += 1
        total_symbols += len(batch.symbols)
        total_occurrences += len(batch.occurrences)
        total_edges += len(batch.edges)
        if batch.file.path:
            total_files += 1

        store.write_batch(batch)

        if batch_count % 100 == 0:
            elapsed = time.time() - t0
            print(f"  [{batch_count} batches] "
                  f"files={total_files} sym={total_symbols} "
                  f"occ={total_occurrences} edges={total_edges} "
                  f"time={elapsed:.1f}s")

    store.finalize()
    elapsed = time.time() - t0

    # ── Summary ──

    db_size_mb = db_path.stat().st_size / 1024 / 1024

    print()
    print(_green("═" * 50))
    print(_green(f"  Ingestion complete"))
    print(_green("═" * 50))
    print(f"  Batches:      {batch_count}")
    print(f"  Files:         {total_files}")
    print(f"  Symbols:       {total_symbols}")
    print(f"  Occurrences:   {total_occurrences}")
    print(f"  Edges:         {total_edges}")
    print(f"  DB size:       {db_size_mb:.1f} MB")
    print(f"  Time:          {elapsed:.1f}s")

    # Edge type breakdown
    edge_counts = {}
    for row in store.conn.execute(
        "SELECT type, COUNT(*) FROM edges GROUP BY type"
    ).fetchall():
        edge_counts[row[0]] = row[1]

    if edge_counts:
        print(f"\n  {_bold('Edge breakdown:')}")
        for etype, count in sorted(edge_counts.items(), key=lambda x: -x[1]):
            print(f"    {etype}: {count}")

    store.close()


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

def _print_next_steps() -> None:
    """Print what to do after a successful `kgraph init`.

    `kgraph install` (wire the MCP server into AI agents) and `kgraph init`
    (build the graph) are independent — either may run first. So only
    suggest install when no agent is configured yet; otherwise just remind
    the user to restart their agent so it picks up the refreshed graph.
    """
    configured = []
    try:
        from installer import detect as _detect_agents
        configured = [d for d in _detect_agents("global")
                      if d.result.already_configured]
    except Exception:
        pass

    print()
    print(_green("✅ Done.") + " Graph written to .kgraph/kgraph.db.")
    if configured:
        names = ", ".join(d.target.display_name for d in configured)
        print(f"  KGraph is already wired into {names}.")
        print(_green("Next:") + " restart your agent to load the refreshed KGraph tools.")
    else:
        print(_green("Next steps:"))
        print(f"  1. kgraph install          # wire KGraph into your AI agent")
        print(f"  2. Restart your agent       # load KGraph MCP tools")
        print(_dim("  (kgraph install and kgraph init are independent — either order works)"))


def cmd_init(argv: list[str] | None = None) -> int:
    """`kgraph init` subcommand handler."""
    parser = argparse.ArgumentParser(
        prog="kgraph init",
        description="Build the code graph for a kernel source tree.",
    )
    parser.add_argument(
        "path",
        help="Path to the kernel source directory "
             "(must contain compile_commands.json)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force rebuild: overwrite existing index.scip and kgraph.db",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip scip-clang (use existing index.scip, only re-ingest into DB)",
    )
    parser.add_argument(
        "--scip-clang",
        default=None,
        help="Path to scip-clang binary (default: auto-detect)",
    )

    args = parser.parse_args(argv)
    kernel_dir = Path(args.path).resolve()

    # ── Pre-checks ──

    print(_bold(f"KGraph init: {kernel_dir}"))
    print()

    if not kernel_dir.is_dir():
        print(_red(f"ERROR: Not a directory: {kernel_dir}"))
        return 1

    compdb = kernel_dir / "compile_commands.json"
    if not compdb.exists():
        print(_red("ERROR: compile_commands.json not found."))
        print("  Build the kernel with: make CC=clang LLVM=1")
        print("  Then run: ./scripts/clang-tools/gen_compile_commands.py")
        return 1

    scip_output = kernel_dir / "index.scip"
    db_path = kernel_dir / ".kgraph" / "kgraph.db"

    # Check existing artifacts
    if scip_output.exists() and not args.force and not args.skip_build:
        size_mb = scip_output.stat().st_size / 1024 / 1024
        print(_yellow(f"index.scip already exists ({size_mb:.1f} MB), skipping scip-clang."))
        print("  Use --force to rebuild, or --skip-build to re-ingest only.")
        args.skip_build = True

    if db_path.exists() and not args.force:
        print(_yellow(f"kgraph.db already exists: {db_path}"))
        print("  Use --force to rebuild from scratch.")
        return 1

    # ── Step 1: scip-clang ──

    if not args.skip_build:
        scip_clang = _find_scip_clang(args.scip_clang)
        _run_scip_clang(scip_clang, compdb, kernel_dir)
    else:
        if not scip_output.exists():
            print(_red("ERROR: --skip-build but index.scip not found."))
            return 1
        size_mb = scip_output.stat().st_size / 1024 / 1024
        print(_yellow(f"Skipping scip-clang (using existing index.scip, {size_mb:.1f} MB)"))

    # ── Step 2: Parse → SQLite ──

    if not scip_output.exists():
        print(_red("ERROR: index.scip not found. scip-clang may have failed."))
        return 1

    _ingest(scip_output, db_path)

    _print_next_steps()

    return 0


if __name__ == "__main__":
    sys.exit(cmd_init())
