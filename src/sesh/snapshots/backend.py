"""Terminal-backend interface for the snapshot subsystem.

A backend captures the current state of the host's terminal emulator
(window/tab layout, scrollback, working directories) and reopens tabs
from a previously captured plan. The Darwin/Terminal.app implementation
lives in `terminal_app.py`; everything else in `snapshots/` is platform-
agnostic and only ever talks to a backend through this Protocol.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sesh.snapshots.core import RestoreItem


@dataclass
class CapturedTab:
    """One terminal tab as observed by a backend at capture time."""

    window: int
    tab: int
    tty: str | None
    cwd: str | None
    scrollback_tail: str  # truncated to SCROLLBACK_MAX_LINES by the backend


@dataclass
class RestoreOutcome:
    """Result of asking a backend to spawn tabs from a restore plan."""

    launched: int
    fellback: bool
    note: str | None = None


class TerminalBackend(Protocol):
    name: str

    def is_supported(self) -> bool: ...

    def capture(self) -> list[CapturedTab]: ...

    def restore(self, items: "list[RestoreItem]") -> RestoreOutcome: ...


def get_backend() -> TerminalBackend | None:
    """Return the active terminal backend, or None if none is supported."""
    if platform.system() == "Darwin":
        from sesh.snapshots.terminal_app import TerminalAppBackend

        return TerminalAppBackend()
    return None
