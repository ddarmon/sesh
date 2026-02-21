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


def encode_project_path(path: str) -> str:
    """Encode a filesystem path as a provider project directory name."""
    return path.lstrip("/").replace("/", "-")


def workspace_uri(path: str) -> str:
    """Convert an absolute path to a Cursor workspace file URI."""
    return f"file://{path}"
