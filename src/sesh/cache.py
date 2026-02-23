"""Metadata cache for fast subsequent launches."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider, SessionMeta
from sesh.paths import CACHE_DIR

CACHE_FILE = CACHE_DIR / "sessions.json"
INDEX_FILE = CACHE_DIR / "index.json"


def _session_to_dict(s: SessionMeta) -> dict:
    return {
        "id": s.id,
        "project_path": s.project_path,
        "provider": s.provider.value,
        "summary": s.summary,
        "timestamp": s.timestamp.isoformat(),
        "start_timestamp": s.start_timestamp.isoformat() if s.start_timestamp else None,
        "message_count": s.message_count,
        "model": s.model,
        "source_path": s.source_path,
    }


def _dict_to_session(d: dict) -> SessionMeta:
    def _parse_dt(value, *, default: datetime | None) -> datetime | None:
        if isinstance(value, str):
            value = value.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return default
        return default

    ts = d["timestamp"]
    timestamp = _parse_dt(ts, default=datetime.now(tz=timezone.utc))
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc)

    start_timestamp = _parse_dt(d.get("start_timestamp"), default=None)

    return SessionMeta(
        id=d["id"],
        project_path=d["project_path"],
        provider=Provider(d["provider"]),
        summary=d["summary"],
        timestamp=timestamp,
        start_timestamp=start_timestamp,
        message_count=d.get("message_count", 0),
        model=d.get("model"),
        source_path=d.get("source_path"),
    )


class SessionCache:
    """File-based metadata cache keyed by source file path + mtime/size."""

    def __init__(self) -> None:
        self._cache: dict = {}
        self._load()

    def _load(self) -> None:
        if CACHE_FILE.is_file():
            try:
                with open(CACHE_FILE) as f:
                    self._cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    def save(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(self._cache, f)
        except OSError:
            pass

    def get_sessions(self, file_path: str) -> list[SessionMeta] | None:
        """Return cached sessions if the file hasn't changed, else None."""
        entry = self._cache.get(file_path)
        if not entry:
            return None

        try:
            stat = os.stat(file_path)
        except OSError:
            return None

        if stat.st_mtime != entry.get("mtime") or stat.st_size != entry.get("size"):
            return None

        try:
            return [_dict_to_session(d) for d in entry["sessions"]]
        except (KeyError, ValueError):
            return None

    def put_sessions(self, file_path: str, sessions: list[SessionMeta]) -> None:
        """Cache sessions for a file."""
        try:
            stat = os.stat(file_path)
        except OSError:
            return

        self._cache[file_path] = {
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "sessions": [_session_to_dict(s) for s in sessions],
        }

    def get_sessions_for_dir(self, dir_path: str) -> list[SessionMeta] | None:
        """Return cached sessions if no files in the directory have changed."""
        entry = self._cache.get(f"dir:{dir_path}")
        if not entry:
            return None

        current_fp = self._dir_fingerprint(dir_path)
        if current_fp is None or current_fp != entry.get("fingerprint"):
            return None

        try:
            return [_dict_to_session(d) for d in entry["sessions"]]
        except (KeyError, ValueError):
            return None

    def put_sessions_for_dir(self, dir_path: str, sessions: list[SessionMeta]) -> None:
        """Cache sessions for a directory with a fingerprint of its files."""
        fp = self._dir_fingerprint(dir_path)
        if fp is None:
            return

        self._cache[f"dir:{dir_path}"] = {
            "fingerprint": fp,
            "sessions": [_session_to_dict(s) for s in sessions],
        }

    @staticmethod
    def _dir_fingerprint(dir_path: str) -> list | None:
        """Return a fingerprint based on JSONL file mtimes and sizes."""
        try:
            base = Path(dir_path)
            if not base.is_dir():
                return None
            entries = []
            for f in sorted(base.glob("*.jsonl")):
                if f.name.startswith("agent-"):
                    continue
                stat = f.stat()
                entries.append([f.name, stat.st_mtime, stat.st_size])
            return entries
        except OSError:
            return None


PROJECT_PATHS_FILE = CACHE_DIR / "project_paths.json"


def load_project_paths() -> dict[str, dict]:
    """Load cached {encoded_dir_name: {path, mtime}} mapping."""
    if not PROJECT_PATHS_FILE.is_file():
        return {}
    try:
        with open(PROJECT_PATHS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_project_paths(mapping: dict[str, dict]) -> None:
    """Save the project path mapping to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(PROJECT_PATHS_FILE, "w") as f:
            json.dump(mapping, f)
    except OSError:
        pass


def save_index(
    projects: dict[str, "Project"],
    sessions: dict[str, list[SessionMeta]],
) -> None:
    """Write the index file for fast CLI reads."""
    from sesh.models import Project  # noqa: F811

    now = datetime.now(tz=timezone.utc).isoformat()

    proj_list = []
    for path, proj in sorted(projects.items()):
        proj_list.append({
            "path": proj.path,
            "display_name": proj.display_name,
            "providers": sorted(p.value for p in proj.providers),
            "session_count": proj.session_count,
            "latest_activity": proj.latest_activity.isoformat() if proj.latest_activity else None,
        })

    sess_list = []
    for path, sess in sessions.items():
        for s in sess:
            sess_list.append(_session_to_dict(s))

    data = {
        "refreshed_at": now,
        "projects": proj_list,
        "sessions": sess_list,
    }

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(INDEX_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def load_index() -> dict | None:
    """Load the index file. Returns the parsed dict or None if missing/corrupt."""
    if not INDEX_FILE.is_file():
        return None
    try:
        with open(INDEX_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
