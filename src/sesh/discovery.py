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


PROVIDER_NAMES = ("claude", "codex", "cursor", "copilot", "pi", "gemini", "opencode")


def construct_provider(name: str, *, cache=None, base_dir: Path | None = None,
                       host: str | None = None):
    """Construct one provider independently (shared by discovery and doctor)."""
    import importlib

    class_names = {
        "claude": "ClaudeProvider", "codex": "CodexProvider",
        "cursor": "CursorProvider", "copilot": "CopilotProvider",
        "pi": "PiProvider", "gemini": "GeminiProvider",
        "opencode": "OpencodeProvider",
    }
    module = importlib.import_module(f"sesh.providers.{name}")
    cls = getattr(module, class_names[name])
    kwargs = {}
    if base_dir is not None:
        kwargs["base_dir"] = base_dir
    if host is not None:
        kwargs["host"] = host
    if name in {"codex", "pi", "gemini", "opencode"}:
        kwargs["cache"] = cache
    return cls(**kwargs)


def _build_providers(cache, *, base_dir=None, host=None) -> list:
    providers = []
    for name in PROVIDER_NAMES:
        try:
            providers.append(construct_provider(
                name, cache=cache, base_dir=base_dir, host=host,
            ))
        except Exception:
            pass
    return providers


def _local_providers(cache) -> list:
    return _build_providers(cache)


def _aggregated_providers(host_dir: Path, host: str, cache) -> list:
    return _build_providers(cache, base_dir=host_dir, host=host)


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
