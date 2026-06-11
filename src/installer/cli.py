"""
KGraph installer CLI — `kgraph install` entry point.

Implements the user-facing flow:
  1. detect() auto-reads system agent config files
  2. identifies which AI agents are present
  3. auto-configures the kgraph MCP server into each

Usage:
    python -m installer.cli install            # auto-detect & configure
    python -m installer.cli install --target claude,cursor
    python -m installer.cli install --location local
    python -m installer.cli detect             # just show what's detected
    python -m installer.cli uninstall          # remove from all agents
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `installer` importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from installer import detect, install, uninstall  # noqa: E402
from installer.base import Location  # noqa: E402

# ── ANSI helpers ──
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _c(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}" if sys.stdout.isatty() else text


def cmd_detect(loc: Location) -> int:
    """Show which agents are detected on this system."""
    print(_c(f"Detecting AI agents (location={loc})...", _BOLD))
    results = detect(loc)
    any_installed = False
    for d in results:
        status = []
        if d.result.installed:
            any_installed = True
            status.append(_c("installed", _GREEN))
        else:
            status.append(_c("not found", _DIM))
        if d.result.already_configured:
            status.append(_c("kgraph configured", _YELLOW))
        print(f"  {d.target.display_name:14s} {' · '.join(status)}")
        if d.result.config_path:
            print(f"    {_c(d.result.config_path, _DIM)}")
    if not any_installed:
        print(_c("\nNo agents detected. You can still configure one explicitly:", _YELLOW))
        print("  kgraph install --target claude")
    return 0


def cmd_install(target_ids: list[str] | None, loc: Location) -> int:
    """Auto-detect (or explicitly target) and configure agents."""
    if target_ids:
        print(_c(f"Configuring kgraph for: {', '.join(target_ids)} (location={loc})", _BOLD))
    else:
        print(_c(f"Auto-detecting and configuring agents (location={loc})...", _BOLD))

    reports = install(target_ids=target_ids, loc=loc, auto_detect=True)

    if not reports:
        print(_c("No agents to configure.", _YELLOW))
        print("Detected nothing installed. Configure explicitly with --target <id>.")
        return 1

    for r in reports:
        print(f"\n{_c(r.display_name, _BOLD)}:")
        for fc in r.result.files:
            symbol = {
                "created": _c("+ created", _GREEN),
                "updated": _c("~ updated", _GREEN),
                "unchanged": _c("= unchanged", _DIM),
                "removed": _c("- removed", _YELLOW),
                "not-found": _c("? not-found", _DIM),
            }.get(fc.action, fc.action)
            print(f"  {symbol}  {fc.path}")
        for note in r.result.notes:
            print(f"  {_c('note:', _YELLOW)} {note}")

    print(_c("\n✅ Done. Restart your agent(s) for the MCP server to load.", _GREEN))
    return 0


def cmd_uninstall(target_ids: list[str] | None, loc: Location) -> int:
    """Remove kgraph config from agents."""
    print(_c(f"Removing kgraph config (location={loc})...", _BOLD))
    reports = uninstall(target_ids=target_ids, loc=loc)
    for r in reports:
        changed = [fc for fc in r.result.files if fc.action == "removed"]
        if changed:
            print(f"\n{_c(r.display_name, _BOLD)}:")
            for fc in changed:
                print(f"  {_c('- removed', _YELLOW)}  {fc.path}")
    print(_c("\n✅ Uninstall complete.", _GREEN))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kgraph",
        description="Configure the kgraph MCP server into AI code agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--target", help="comma-separated agent ids "
                       "(claude,cursor,codex,opencode,hermes)")
        p.add_argument("--location", choices=["global", "local"],
                       default="global", help="install scope (default: global)")

    p_install = sub.add_parser("install", help="auto-detect & configure agents")
    add_common(p_install)

    p_detect = sub.add_parser("detect", help="show detected agents")
    p_detect.add_argument("--location", choices=["global", "local"], default="global")

    p_uninstall = sub.add_parser("uninstall", help="remove kgraph from agents")
    add_common(p_uninstall)

    args = parser.parse_args(argv)
    loc: Location = args.location
    target_ids = None
    if getattr(args, "target", None):
        target_ids = [t.strip() for t in args.target.split(",") if t.strip()]

    if args.command == "detect":
        return cmd_detect(loc)
    if args.command == "install":
        return cmd_install(target_ids, loc)
    if args.command == "uninstall":
        return cmd_uninstall(target_ids, loc)
    return 1


if __name__ == "__main__":
    sys.exit(main())