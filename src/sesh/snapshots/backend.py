"""Terminal-backend interface for the snapshot subsystem.

A backend captures the current state of the host's terminal emulator
(window/tab layout, scrollback, working directories) and reopens tabs
from a previously captured plan. The Darwin/Terminal.app implementation
lives in `terminal_app.py`; everything else in `snapshots/` is platform-
agnostic and only ever talks to a backend through this Protocol.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sesh.snapshots.core import RestoreItem


@dataclass
class TtyProcess:
    """One process attached to a tab's tty at capture time."""

    pid: int
    ppid: int
    start: float | None  # epoch seconds
    argv: list[str]


@dataclass
class CapturedTab:
    """One terminal tab as observed by a backend at capture time."""

    window: int
    tab: int
    tty: str | None
    cwd: str | None
    scrollback_tail: str  # truncated to SCROLLBACK_MAX_LINES by the backend
    # Every process on the tab's tty. Empty when the backend cannot list
    # them, which makes resume resolution fall back to scrollback alone.
    processes: list[TtyProcess] = field(default_factory=list)


@dataclass
class RestoreOutcome:
    """Result of asking a backend to spawn tabs from a restore plan."""

    launched: int
    fellback: bool
    note: str | None = None


class TerminalBackendError(Exception):
    """Raised when a backend could not talk to the terminal application.

    The message is shown to the user, so it should carry the underlying
    error text (e.g. osascript's stderr).
    """


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
