"""Shared discovery logic used by both the TUI and CLI."""

from __future__ import annotations

from datetime import datetime, timezone

from sesh.models import Project, SessionMeta

_DATETIME_MIN = datetime.min.replace(tzinfo=timezone.utc)


def discover_all(cache=None) -> tuple[dict[str, Project], dict[str, list[SessionMeta]]]:
    """Discover all projects and sessions across providers.

    Returns (projects, sessions) where projects is keyed by project path
    and sessions is a dict of project_path -> list of SessionMeta.

    If *cache* is a SessionCache instance, providers will use it to skip
    re-parsing unchanged files.
    """
    from sesh.providers.claude import ClaudeProvider

    providers_list = [ClaudeProvider()]

    try:
        from sesh.providers.codex import CodexProvider
        providers_list.append(CodexProvider(cache=cache))
    except Exception:
        pass

    try:
        from sesh.providers.cursor import CursorProvider
        providers_list.append(CursorProvider())
    except Exception:
        pass

    projects: dict[str, Project] = {}
    sessions: dict[str, list[SessionMeta]] = {}

    for provider in providers_list:
        try:
            for project_path, display_name in provider.discover_projects():
                if project_path not in projects:
                    projects[project_path] = Project(
                        path=project_path,
                        display_name=display_name,
                    )
                proj = projects[project_path]
                sess = provider.get_sessions(project_path, cache=cache)
                if sess:
                    proj.providers.add(sess[0].provider)
                    existing = sessions.get(project_path, [])
                    existing.extend(sess)
                    sessions[project_path] = existing
                    proj.session_count = len(sessions[project_path])
                    for s in sess:
                        if proj.latest_activity is None or s.timestamp > proj.latest_activity:
                            proj.latest_activity = s.timestamp
        except Exception:
            pass

    for path in sessions:
        sessions[path].sort(key=lambda s: s.timestamp, reverse=True)

    return projects, sessions
