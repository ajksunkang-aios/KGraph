"""
KGraph installer — orchestrator.

The `detect()` function the spec asks for: reads each agent's config
file/dir, identifies which AI agents are present on this system, and
returns the detection results. `install()` then auto-configures the
detected (or explicitly targeted) agents.

This is the entry point behind `kgraph install`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import AgentTarget, DetectionResult, Location, WriteResult
from .targets import ALL_TARGETS, get_target


@dataclass
class TargetDetection:
    """Detection result paired with its target, for reporting."""
    target: AgentTarget
    result: DetectionResult


def detect(loc: Location = "global") -> list[TargetDetection]:
    """
    Detect which AI agents are installed on this system.

    Reads each known agent's config file/dir and reports whether it
    looks installed and whether kgraph is already configured.

    Returns one TargetDetection per known agent (whether installed or not),
    so the caller can show the full picture.
    """
    out = []
    for target in ALL_TARGETS:
        if not target.supports_location(loc):
            continue
        out.append(TargetDetection(target, target.detect(loc)))
    return out


def detect_installed(loc: Location = "global") -> list[AgentTarget]:
    """Return only the targets that appear installed on this system."""
    return [d.target for d in detect(loc) if d.result.installed]


@dataclass
class InstallReport:
    """Aggregate of an install/uninstall run across targets."""
    target_id: str
    display_name: str
    result: WriteResult


def install(target_ids: list[str] | None = None,
            loc: Location = "global",
            auto_detect: bool = True) -> list[InstallReport]:
    """
    Install (configure) the kgraph MCP server into agents.

    Args:
        target_ids: explicit agent ids to configure. If None and
            auto_detect is True, configures every detected-installed agent.
        loc: "global" or "local" install location.
        auto_detect: when target_ids is None, auto-select installed agents.

    Returns one InstallReport per configured agent.
    """
    targets = _resolve_targets(target_ids, loc, auto_detect)
    reports = []
    for t in targets:
        result = t.install(loc)
        reports.append(InstallReport(t.id, t.display_name, result))
    return reports


def uninstall(target_ids: list[str] | None = None,
              loc: Location = "global") -> list[InstallReport]:
    """Remove kgraph config from agents (inverse of install)."""
    if target_ids:
        targets = [t for tid in target_ids if (t := get_target(tid))]
    else:
        # Uninstall from every known target (idempotent — not-found is safe)
        targets = [t for t in ALL_TARGETS if t.supports_location(loc)]
    reports = []
    for t in targets:
        result = t.uninstall(loc)
        reports.append(InstallReport(t.id, t.display_name, result))
    return reports


def _resolve_targets(target_ids: list[str] | None,
                     loc: Location,
                     auto_detect: bool) -> list[AgentTarget]:
    """Resolve which targets to act on."""
    if target_ids:
        out = []
        for tid in target_ids:
            t = get_target(tid)
            if t and t.supports_location(loc):
                out.append(t)
        return out
    if auto_detect:
        return detect_installed(loc)
    # No explicit targets, no auto-detect → all supported
    return [t for t in ALL_TARGETS if t.supports_location(loc)]