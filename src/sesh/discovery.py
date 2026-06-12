"""Shared discovery logic used by both the TUI and CLI."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Project, SessionMeta

_DATETIME_MIN = datetime.min.replace(tzinfo=timezone.utc)


def discover_all(
    cache=None,
    aggregation_root: Path | None = None,
) -> tuple[dict[str, Project], dict[str, list[SessionMeta]]]:
    """Discover all projects and sessions across providers.

    Returns (projects, sessions) where projects is keyed by project path
    and sessions is a dict of project_path -> list of SessionMeta.

    If *cache* is a SessionCache instance, providers will use it to skip
    re-parsing unchanged files.

    If *aggregation_root* is set, discovery scans per-host subdirectories
    under that root instead of the local ``$HOME``. Project paths from
    different hosts stay separate (composite key ``"{host}::{path}"``)
    and every returned ``Project`` / ``SessionMeta`` is stamped with its
    ``host`` so downstream code can attribute provenance.
    """
    if aggregation_root is not None:
        return _discover_aggregated(Path(aggregation_root), cache)

    providers_list = _local_providers(cache)
    return _run_discovery(providers_list, cache)


def _local_providers(cache) -> list:
    """Build the default local provider list (one instance each)."""
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

    try:
        from sesh.providers.copilot import CopilotProvider
        providers_list.append(CopilotProvider())
    except Exception:
        pass

    try:
        from sesh.providers.pi import PiProvider
        providers_list.append(PiProvider(cache=cache))
    except Exception:
        pass

    try:
        from sesh.providers.gemini import GeminiProvider
        providers_list.append(GeminiProvider(cache=cache))
        from sesh.providers.opencode import OpencodeProvider
        providers_list.append(OpencodeProvider(cache=cache))
    except Exception:
        pass

    return providers_list


def _aggregated_providers(host_dir: Path, host: str, cache) -> list:
    """Build a provider list rooted at one per-host subtree."""
    from sesh.providers.claude import ClaudeProvider

    providers_list = [ClaudeProvider(base_dir=host_dir, host=host)]

    try:
        from sesh.providers.codex import CodexProvider
        providers_list.append(CodexProvider(cache=cache, base_dir=host_dir, host=host))
    except Exception:
        pass

    try:
        from sesh.providers.cursor import CursorProvider
        providers_list.append(CursorProvider(base_dir=host_dir, host=host))
    except Exception:
        pass

    try:
        from sesh.providers.copilot import CopilotProvider
        providers_list.append(CopilotProvider(base_dir=host_dir, host=host))
    except Exception:
        pass

    try:
        from sesh.providers.pi import PiProvider
        providers_list.append(PiProvider(cache=cache, base_dir=host_dir, host=host))
    except Exception:
        pass

    try:
        from sesh.providers.gemini import GeminiProvider
        providers_list.append(GeminiProvider(cache=cache, base_dir=host_dir, host=host))
        from sesh.providers.opencode import OpencodeProvider
        providers_list.append(OpencodeProvider(cache=cache, base_dir=host_dir, host=host))
    except Exception:
        pass

    return providers_list


def _run_discovery(
    providers_list: list,
    cache,
) -> tuple[dict[str, Project], dict[str, list[SessionMeta]]]:
    """Run discover_projects + get_sessions across a provider list."""
    projects: dict[str, Project] = {}
    sessions: dict[str, list[SessionMeta]] = {}

    for provider in providers_list:
        host = getattr(provider, "host", None)
        try:
            for project_path, display_name in provider.discover_projects():
                # In aggregation mode, key by host::path so identical
                # paths from different hosts stay separate.
                key = f"{host}::{project_path}" if host else project_path
                if key not in projects:
                    projects[key] = Project(
                        path=project_path,
                        display_name=display_name,
                        host=host,
                    )
                proj = projects[key]
                sess = provider.get_sessions(project_path, cache=cache)
                if sess:
                    proj.providers.add(sess[0].provider)
                    existing = sessions.get(key, [])
                    existing.extend(sess)
                    sessions[key] = existing
                    proj.session_count = len(sessions[key])
                    for s in sess:
                        if proj.latest_activity is None or s.timestamp > proj.latest_activity:
                            proj.latest_activity = s.timestamp
        except Exception:
            pass

    for path in sessions:
        sessions[path].sort(key=lambda s: s.timestamp, reverse=True)

    return projects, sessions


def _discover_aggregated(
    aggregation_root: Path,
    cache,
) -> tuple[dict[str, Project], dict[str, list[SessionMeta]]]:
    """Discover sessions across per-host subtrees under *aggregation_root*."""
    if not aggregation_root.is_dir():
        return {}, {}

    all_providers: list = []
    for host_dir in sorted(aggregation_root.iterdir()):
        if not host_dir.is_dir() or host_dir.name.startswith("."):
            continue
        all_providers.extend(_aggregated_providers(host_dir, host_dir.name, cache))

    return _run_discovery(all_providers, cache)
