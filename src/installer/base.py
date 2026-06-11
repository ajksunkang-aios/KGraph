"""
KGraph installer — agent target abstraction.

Each MCP-capable agent (Claude Code, Cursor, Codex CLI, opencode, Hermes)
implements AgentTarget so the orchestrator can detect it and write the
right MCP-server config without baking client-specific paths into core
code. Adding a new agent = one new file in targets/ + one registry entry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

Location = Literal["global", "local"]
FileAction = Literal["created", "updated", "unchanged", "removed", "not-found"]


@dataclass
class DetectionResult:
    """Result of target.detect(location).

    installed: best-effort heuristic that the agent is present on this
        system (its config dir/file exists). Drives whether the
        orchestrator auto-selects this target.
    already_configured: whether kgraph is already wired into this target.
    config_path: the path inspected (for diagnostics).
    """
    installed: bool
    already_configured: bool
    config_path: str = ""


@dataclass
class FileChange:
    """One file the installer touched."""
    path: str
    action: FileAction


@dataclass
class WriteResult:
    """What target.install()/uninstall() changed on disk."""
    files: list[FileChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class AgentTarget(ABC):
    """Interface every agent target implements."""

    id: str                 # stable lowercase id (claude/codex/cursor/...)
    display_name: str       # human-readable name for log lines
    docs_url: str = ""

    @abstractmethod
    def supports_location(self, loc: Location) -> bool:
        """Whether this target supports the given install location."""
        ...

    @abstractmethod
    def detect(self, loc: Location) -> DetectionResult:
        """Detect whether this agent is installed + already configured."""
        ...

    @abstractmethod
    def install(self, loc: Location) -> WriteResult:
        """Write the kgraph MCP server config for this agent."""
        ...

    @abstractmethod
    def uninstall(self, loc: Location) -> WriteResult:
        """Remove the kgraph MCP server config (inverse of install)."""
        ...

    @abstractmethod
    def describe_paths(self, loc: Location) -> list[str]:
        """Filesystem paths this target would write to."""
        ...