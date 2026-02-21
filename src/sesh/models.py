"""Data models for sesh."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Provider(Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    CURSOR = "cursor"


@dataclass
class MoveReport:
    provider: Provider
    success: bool
    files_modified: int = 0
    dirs_renamed: int = 0
    error: str | None = None


@dataclass
class Project:
    path: str
    display_name: str
    providers: set[Provider] = field(default_factory=set)
    session_count: int = 0
    latest_activity: datetime | None = None
    # Provider-specific folder names (e.g. Claude's encoded project dir name)
    claude_project_name: str | None = None


@dataclass
class SessionMeta:
    id: str
    project_path: str
    provider: Provider
    summary: str
    timestamp: datetime
    message_count: int = 0
    model: str | None = None
    source_path: str | None = None  # File path for on-demand message loading


@dataclass
class Message:
    role: str  # "user", "assistant", "system", "tool"
    content: str
    timestamp: datetime | None = None
    tool_name: str | None = None
    is_system: bool = False


@dataclass
class SearchResult:
    session_id: str
    project_path: str
    provider: Provider
    matched_line: str
    file_path: str


def encode_cursor_path(path: str) -> str:
    """Encode a path the way Cursor does for ``~/.cursor/projects/``.

    Cursor strips the leading ``/`` then replaces ``/`` and spaces with
    ``-``::

        /Users/me/My Project  ->  Users-me-My-Project
    """
    return path.lstrip("/").replace("/", "-").replace(" ", "-")


def encode_claude_path(path: str) -> str:
    """Encode a path the way Claude Code does for ``~/.claude/projects/``.

    Claude keeps the leading ``/`` (which becomes a leading ``-``) and
    also replaces spaces with ``-``::

        /Users/me/My Project  ->  -Users-me-My-Project
    """
    return path.replace("/", "-").replace(" ", "-")


def workspace_uri(path: str) -> str:
    """Convert an absolute path to a Cursor workspace file URI."""
    return f"file://{path}"
