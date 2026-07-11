"""Session provider base class and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from sesh.models import Message, MoveReport, Project, SessionMeta


class SessionProvider(ABC):
    """Base class for session providers (Claude, Codex, Cursor)."""

    @abstractmethod
    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) pairs."""

    @abstractmethod
    def get_sessions(self, project_id: str, cache=None) -> list[SessionMeta]:
        """Return sessions for a given project identifier."""

    @abstractmethod
    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages for a session on demand."""

    def diagnostic_paths(self) -> list[tuple[str, Path]]:
        """Return the provider roots inspected during discovery.

        This is deliberately non-abstract so third-party providers and test
        doubles remain compatible.  Built-in providers expose their existing
        resolved roots through the conventional private path properties.
        """
        candidates = (
            ("projects", "_projects_dir"),
            ("sessions", "_sessions_dir"),
            ("sessions", "_codex_dir"),
            ("sessions", "_copilot_dir"),
            ("chats", "_chats_dir"),
            ("workspace_storage", "_workspace_storage"),
            ("data", "_data_dir"),
            ("tmp", "_tmp_dir"),
        )
        result: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        for label, attribute in candidates:
            if not hasattr(self, attribute):
                continue
            value = getattr(self, attribute)
            path = Path(value() if callable(value) else value)
            if path not in seen:
                result.append((label, path))
                seen.add(path)
        return result

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a session's stored data. Override per provider."""
        raise NotImplementedError

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Update metadata when a project moves. Override per provider."""
        raise NotImplementedError


_providers: list[SessionProvider] = []


def register_provider(provider: SessionProvider) -> None:
    _providers.append(provider)


def get_providers() -> list[SessionProvider]:
    return list(_providers)
