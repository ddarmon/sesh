"""Metadata cache for fast subsequent launches."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider, SessionMeta

CACHE_DIR = Path.home() / ".cache" / "sesh"
CACHE_FILE = CACHE_DIR / "sessions.json"


def _session_to_dict(s: SessionMeta) -> dict:
    return {
        "id": s.id,
        "project_path": s.project_path,
        "provider": s.provider.value,
        "summary": s.summary,
        "timestamp": s.timestamp.isoformat(),
        "message_count": s.message_count,
        "model": s.model,
        "source_path": s.source_path,
    }


def _dict_to_session(d: dict) -> SessionMeta:
    ts = d["timestamp"]
    if isinstance(ts, str):
        ts = ts.replace("Z", "+00:00")
        timestamp = datetime.fromisoformat(ts)
    else:
        timestamp = datetime.now(tz=timezone.utc)

    return SessionMeta(
        id=d["id"],
        project_path=d["project_path"],
        provider=Provider(d["provider"]),
        summary=d["summary"],
        timestamp=timestamp,
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
