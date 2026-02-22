from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, Provider, SessionMeta


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def create_store_db(
    db_path: Path, blobs: list[dict], meta: dict[str, str] | None = None
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE blobs (id TEXT, data BLOB)")
        conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        for i, blob in enumerate(blobs):
            conn.execute(
                "INSERT INTO blobs (id, data) VALUES (?, ?)",
                (str(i), json.dumps(blob).encode("utf-8")),
            )
        if meta:
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                list(meta.items()),
            )
        conn.commit()
    finally:
        conn.close()


def make_session(**overrides) -> SessionMeta:
    data = {
        "id": "session-1",
        "project_path": "/tmp/project",
        "provider": Provider.CLAUDE,
        "summary": "summary",
        "timestamp": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "start_timestamp": None,
        "message_count": 1,
        "model": None,
        "source_path": None,
    }
    data.update(overrides)
    return SessionMeta(**data)


def make_message(**overrides) -> Message:
    data = {
        "role": "user",
        "content": "hello",
        "timestamp": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "tool_name": None,
        "is_system": False,
        "tool_input": None,
        "tool_output": None,
        "thinking": None,
        "content_type": "text",
    }
    data.update(overrides)
    return Message(**data)


def make_index(projects, sessions) -> dict:
    from sesh.cache import _session_to_dict

    if isinstance(projects, dict):
        project_items = list(projects.values())
    else:
        project_items = list(projects)

    if isinstance(sessions, dict):
        session_items = []
        for sess_list in sessions.values():
            session_items.extend(sess_list)
    else:
        session_items = list(sessions)

    return {
        "refreshed_at": datetime.now(tz=timezone.utc).isoformat(),
        "projects": [
            {
                "path": p.path,
                "display_name": p.display_name,
                "providers": sorted(provider.value for provider in p.providers),
                "session_count": p.session_count,
                "latest_activity": p.latest_activity.isoformat() if p.latest_activity else None,
            }
            for p in project_items
        ],
        "sessions": [_session_to_dict(s) for s in session_items],
    }
