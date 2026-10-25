"""Session provider base class and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from sesh.models import Message, MoveReport, Project, SessionMeta
from sesh.prompt_history import HistoryReport


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

    def diagnostic_issues(self) -> list[str]:
        """Return warnings about on-disk data the provider cannot read.

        Non-abstract for the same reason as ``diagnostic_paths``; providers
        override it when an unreadable format would otherwise look like an
        empty one (e.g. an unrecognized database schema).
        """
        return []

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a session's stored data. Override per provider."""
        raise NotImplementedError

    def delete_history(
        self, session: SessionMeta, dry_run: bool = False
    ) -> HistoryReport:
        """Remove a session's entries from the provider CLI's prompt history.

        Non-abstract: providers whose history carries no session id (or that
        keep no history) report ``supported=False``.
        """
        return HistoryReport(supported=False)

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Update metadata when a project moves. Override per provider."""
        raise NotImplementedError


_providers: list[SessionProvider] = []


def register_provider(provider: SessionProvider) -> None:
    _providers.append(provider)


def get_providers() -> list[SessionProvider]:
    return list(_providers)
