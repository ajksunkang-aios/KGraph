"""
KGraph installer — auto-configure the kgraph MCP server into AI agents.

Public API:
    detect(loc)            → list[TargetDetection]  (which agents are present)
    detect_installed(loc)  → list[AgentTarget]      (only installed ones)
    install(ids, loc)      → list[InstallReport]    (configure agents)
    uninstall(ids, loc)    → list[InstallReport]    (remove config)

Supported agents: claude, cursor, codex, opencode, hermes.
"""

from .base import (
    AgentTarget, DetectionResult, FileChange, Location, WriteResult,
)
from .orchestrator import (
    InstallReport, TargetDetection,
    detect, detect_installed, install, uninstall,
)
from .targets import ALL_TARGETS, TARGETS_BY_ID, get_target

__all__ = [
    "detect", "detect_installed", "install", "uninstall",
    "TargetDetection", "InstallReport",
    "AgentTarget", "DetectionResult", "WriteResult", "FileChange", "Location",
    "ALL_TARGETS", "TARGETS_BY_ID", "get_target",
]