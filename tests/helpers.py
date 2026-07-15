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
        "input_tokens": None,
        "output_tokens": None,
        "cumulative_input_tokens": None,
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


def write_workspace_yaml(path: Path, fields: dict[str, str]) -> None:
    """Write a flat key-value workspace.yaml for Copilot tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for key, value in fields.items():
            f.write(f"{key}: {value}\n")


def write_copilot_events(path: Path, events: list[dict]) -> None:
    """Write Copilot events.jsonl for tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def write_gemini_session(
    path: Path,
    *,
    session_id: str = "11111111-2222-3333-4444-555555555555",
    project_hash: str = "deadbeef" * 8,
    start_time: str = "2026-01-01T00:00:00.000Z",
    last_updated: str = "2026-01-01T01:00:00.000Z",
    messages: list[dict] | None = None,
    summary: str | None = None,
) -> None:
    """Write a Gemini CLI chats/session-*.json fixture file."""
    data: dict = {
        "sessionId": session_id,
        "projectHash": project_hash,
        "startTime": start_time,
        "lastUpdated": last_updated,
        "messages": messages or [],
    }
    if summary is not None:
        data["summary"] = summary
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def write_gemini_projects(gemini_dir: Path, projects: dict[str, str]) -> None:
    """Write ~/.gemini/projects.json ({path: name}) for tests."""
    gemini_dir.mkdir(parents=True, exist_ok=True)
    with open(gemini_dir / "projects.json", "w") as f:
        json.dump({"projects": projects}, f)


def make_snapshot_resume(**overrides):
    """Build a SnapshotResume for tests."""
    from sesh.snapshots import SnapshotResume

    data = {
        "provider": Provider.CLAUDE,
        "session_id": "abc-123",
        "cmd_args": ["claude", "--resume", "abc-123"],
        "source": "explicit",
        "matched_phrase": None,
    }
    data.update(overrides)
    return SnapshotResume(**data)


def make_snapshot_tab(**overrides):
    """Build a SnapshotTab for tests."""
    from sesh.snapshots import SnapshotTab

    data = {
        "window": 1,
        "tab": 1,
        "tty": "/dev/ttys001",
        "cwd": "/tmp/proj",
        "scrollback_tail": "",
        "resume": None,
    }
    data.update(overrides)
    return SnapshotTab(**data)


def make_snapshot(**overrides):
    """Build a Snapshot for tests."""
    from sesh.snapshots import SCHEMA_VERSION, Snapshot

    data = {
        "schema_version": SCHEMA_VERSION,
        "id": "snapshot-20260424-152330",
        "created_at": "2026-04-24T15:23:30-04:00",
        "host": "test-host",
        "tabs": [],
    }
    data.update(overrides)
    return Snapshot(**data)


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


def write_opencode_json(path: Path, obj: dict) -> None:
    """Write a pretty-printed opencode storage JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def write_opencode_storage_session(
    data_dir: Path,
    *,
    session_id: str = "ses_001",
    project_id: str = "proj_001",
    directory: str = "/Users/me/repo",
    title: str = "Fix the bug",
    created: int = 1750000000000,
    updated: int = 1750000600000,
    revert: dict | None = None,
    messages: list[dict] | None = None,
    parts: dict[str, list[dict]] | None = None,
) -> Path:
    """Create one session in the legacy opencode JSON storage layout.

    *messages* is a list of message-info dicts (each needs at least
    ``id`` and ``role``); *parts* maps message id -> list of part dicts.
    Returns the session info file path.
    """
    storage = data_dir / "storage"
    info_file = storage / "session" / project_id / f"{session_id}.json"
    info = {
        "id": session_id,
        "projectID": project_id,
        "directory": directory,
        "title": title,
        "version": "1.0.0",
        "time": {"created": created, "updated": updated},
    }
    if revert is not None:
        info["revert"] = revert
    write_opencode_json(info_file, info)
    for msg in messages or []:
        mid = msg["id"]
        write_opencode_json(storage / "message" / session_id / f"{mid}.json", msg)
        for part in (parts or {}).get(mid, []):
            pid = part.get("id", f"prt_{mid}")
            write_opencode_json(storage / "part" / mid / f"{pid}.json", part)
    return info_file


def create_opencode_db(
    db_path: Path,
    sessions: list[dict],
    messages: list[dict] | None = None,
    parts: list[dict] | None = None,
) -> None:
    """Create a minimal opencode SQLite database.

    *sessions* rows need: id, directory, title, time_created,
    time_updated; optional model (dict), tokens_* ints.
    *messages* rows need: id, session_id, data (dict).
    *parts* rows need: id, message_id, session_id, data (dict).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE session ("
            " id TEXT PRIMARY KEY, project_id TEXT, directory TEXT,"
            " title TEXT, time_created INTEGER, time_updated INTEGER,"
            " model TEXT, tokens_input INTEGER DEFAULT 0,"
            " tokens_output INTEGER DEFAULT 0,"
            " tokens_reasoning INTEGER DEFAULT 0,"
            " tokens_cache_read INTEGER DEFAULT 0,"
            " tokens_cache_write INTEGER DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE message ("
            " id TEXT PRIMARY KEY,"
            " session_id TEXT NOT NULL"
            "  REFERENCES session(id) ON DELETE CASCADE,"
            " time_created INTEGER, data TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE part ("
            " id TEXT PRIMARY KEY,"
            " message_id TEXT NOT NULL"
            "  REFERENCES message(id) ON DELETE CASCADE,"
            " session_id TEXT NOT NULL, time_created INTEGER,"
            " data TEXT NOT NULL)"
        )
        for s in sessions:
            model = s.get("model")
            conn.execute(
                "INSERT INTO session (id, project_id, directory, title,"
                " time_created, time_updated, model, tokens_input,"
                " tokens_output, tokens_reasoning, tokens_cache_read,"
                " tokens_cache_write) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    s["id"], s.get("project_id", "proj"), s["directory"],
                    s.get("title", ""), s.get("time_created", 0),
                    s.get("time_updated", 0),
                    json.dumps(model) if model else None,
                    s.get("tokens_input", 0), s.get("tokens_output", 0),
                    s.get("tokens_reasoning", 0),
                    s.get("tokens_cache_read", 0),
                    s.get("tokens_cache_write", 0),
                ),
            )
        for m in messages or []:
            conn.execute(
                "INSERT INTO message (id, session_id, time_created, data)"
                " VALUES (?,?,?,?)",
                (
                    m["id"], m["session_id"], m.get("time_created", 0),
                    json.dumps(m["data"]),
                ),
            )
        for p in parts or []:
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created,"
                " data) VALUES (?,?,?,?,?)",
                (
                    p["id"], p["message_id"], p["session_id"],
                    p.get("time_created", 0), json.dumps(p["data"]),
                ),
            )
        conn.commit()
    finally:
        conn.close()
