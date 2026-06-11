"""Target registry — all known agent targets, keyed by id."""

from __future__ import annotations

from ..base import AgentTarget
from .claude import claude_target
from .codex import codex_target
from .cursor import cursor_target
from .hermes import hermes_target
from .opencode import opencode_target

ALL_TARGETS: list[AgentTarget] = [
    claude_target,
    cursor_target,
    codex_target,
    opencode_target,
    hermes_target,
]

TARGETS_BY_ID: dict[str, AgentTarget] = {t.id: t for t in ALL_TARGETS}


def get_target(target_id: str) -> AgentTarget | None:
    return TARGETS_BY_ID.get(target_id)