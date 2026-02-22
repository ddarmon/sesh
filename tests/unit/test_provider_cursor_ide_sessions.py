from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider
from sesh.providers import cursor
from tests.helpers import create_store_db, make_session


def _create_state_vscdb(path: Path, composers: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("composer.composerData", json.dumps({"allComposers": composers})),
        )
        conn.commit()
    finally:
        conn.close()


def _write_workspace_json(base: Path, ws_hash: str, project_path: str) -> None:
    ws_dir = base / ws_hash
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "workspace.json").write_text(json.dumps({"folder": f"file://{project_path}"}))


def test_build_workspace_map_and_projects_dir_map(tmp_cursor_dirs) -> None:
    """Workspace JSON files are scanned to build path->hash and path->subdir mappings."""
    project_path = "/Users/me/repo"
    _write_workspace_json(tmp_cursor_dirs["workspace_storage"], "ws1", project_path)
    _write_workspace_json(tmp_cursor_dirs["workspace_storage"], "ws2", "/Users/me/other")
    (tmp_cursor_dirs["projects"] / cursor.encode_cursor_path(project_path)).mkdir(parents=True)

    provider = cursor.CursorProvider()
    workspace_map = provider._build_workspace_map()
    projects_map = provider._build_projects_dir_map()

    assert workspace_map[project_path] == "ws1"
    assert projects_map[project_path] == (
        tmp_cursor_dirs["projects"] / cursor.encode_cursor_path(project_path)
    )


def test_read_composer_data_from_state_vscdb(tmp_cursor_dirs) -> None:
    """Composer data (IDE session metadata) is read from the state.vscdb SQLite file."""
    project_path = "/Users/me/repo"
    _write_workspace_json(tmp_cursor_dirs["workspace_storage"], "ws1", project_path)
    _create_state_vscdb(
        tmp_cursor_dirs["workspace_storage"] / "ws1" / "state.vscdb",
        [{"composerId": "c1", "name": "Session 1"}],
    )

    data = cursor.CursorProvider()._read_composer_data(project_path)
    assert data == [{"composerId": "c1", "name": "Session 1"}]


def test_get_ide_sessions_matches_composer_and_orphans(tmp_cursor_dirs) -> None:
    """IDE sessions match transcripts to composer metadata; unmatched transcripts become orphans."""
    project_path = "/Users/me/repo"
    encoded = cursor.encode_cursor_path(project_path)
    proj_dir = tmp_cursor_dirs["projects"] / encoded
    transcripts = proj_dir / "agent-transcripts"
    transcripts.mkdir(parents=True)

    _write_workspace_json(tmp_cursor_dirs["workspace_storage"], "ws1", project_path)
    _create_state_vscdb(
        tmp_cursor_dirs["workspace_storage"] / "ws1" / "state.vscdb",
        [
            {
                "composerId": "comp1",
                "name": "Named Session",
                "createdAt": 1_735_689_600_000,
                "lastUsedModel": "gpt-4.1",
            }
        ],
    )

    comp1 = transcripts / "comp1.txt"
    comp1.write_text("user:\nhello\nassistant:\nhi\n")
    os.utime(comp1, (1_735_689_660, 1_735_689_660))
    orphan = transcripts / "orphan.txt"
    orphan.write_text("user:\n<user_query>\norphan prompt\n</user_query>\nassistant:\nok\n")
    os.utime(orphan, (1_735_689_700, 1_735_689_700))

    sessions = cursor.CursorProvider()._get_ide_sessions(project_path)
    by_id = {s.id: s for s in sessions}

    assert set(by_id) == {"comp1", "orphan"}
    assert by_id["comp1"].summary == "Named Session"
    assert by_id["comp1"].model == "gpt-4.1"
    assert by_id["comp1"].timestamp == datetime(2025, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
    assert by_id["comp1"].start_timestamp == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert by_id["comp1"].message_count == 2
    assert by_id["comp1"].source_path.endswith("comp1.txt")

    assert by_id["orphan"].summary == "orphan prompt"
    assert by_id["orphan"].message_count == 2
    assert by_id["orphan"].timestamp == datetime(2025, 1, 1, 0, 1, 40, tzinfo=timezone.utc)
    assert by_id["orphan"].start_timestamp is None


def test_discover_projects_dedups_cli_and_ide(tmp_cursor_dirs) -> None:
    """Projects from CLI chats and IDE transcripts are deduped by path."""
    project_path = "/Users/me/repo"

    # CLI chats source
    md5 = hashlib.md5(project_path.encode()).hexdigest()
    create_store_db(
        tmp_cursor_dirs["chats"] / md5 / "sess1" / "store.db",
        blobs=[{"content": f"Workspace Path: {project_path}\n"}],
    )

    # IDE source for the same project + a second project
    second_project = "/Users/me/second"
    for ws_hash, path in [("ws1", project_path), ("ws2", second_project)]:
        _write_workspace_json(tmp_cursor_dirs["workspace_storage"], ws_hash, path)
        transcripts = (
            tmp_cursor_dirs["projects"]
            / cursor.encode_cursor_path(path)
            / "agent-transcripts"
        )
        transcripts.mkdir(parents=True, exist_ok=True)
        (transcripts / "one.txt").write_text("user:\nhi\n")

    discovered = list(cursor.CursorProvider().discover_projects())
    assert discovered == [
        (project_path, "repo"),
        (second_project, "second"),
    ]


def test_get_sessions_dedups_cli_and_ide_ids(tmp_cursor_dirs, monkeypatch) -> None:
    """When CLI and IDE have the same session ID, the CLI version wins (richer metadata)."""
    project_path = "/Users/me/repo"
    md5 = hashlib.md5(project_path.encode()).hexdigest()
    store_db = tmp_cursor_dirs["chats"] / md5 / "sameid" / "store.db"
    create_store_db(
        store_db,
        blobs=[{"role": "user", "content": "hi"}],
        meta={"0": json.dumps({"name": "CLI", "createdAt": 1_735_689_600_000}).encode().hex()},
    )
    os.utime(store_db, (1_735_689_600, 1_735_689_600))

    provider = cursor.CursorProvider()
    ide_same = make_session(
        id="sameid",
        project_path=project_path,
        provider=Provider.CURSOR,
        summary="IDE duplicate",
        timestamp=datetime(2025, 1, 2, tzinfo=timezone.utc),
        source_path="/tmp/sameid.txt",
    )
    ide_other = make_session(
        id="other",
        project_path=project_path,
        provider=Provider.CURSOR,
        summary="IDE other",
        timestamp=datetime(2025, 1, 3, tzinfo=timezone.utc),
        source_path="/tmp/other.txt",
    )
    monkeypatch.setattr(provider, "_get_ide_sessions", lambda _p: [ide_same, ide_other])

    sessions = provider.get_sessions(project_path)
    assert [s.id for s in sessions] == ["other", "sameid"]
    assert sum(1 for s in sessions if s.id == "sameid") == 1
    assert next(s for s in sessions if s.id == "sameid").summary == "CLI"


def test_get_sessions_uses_cache_for_cli_store_db(tmp_cursor_dirs) -> None:
    """Per-file cache hit on a store.db skips SQLite parsing."""
    project_path = "/Users/me/repo"
    md5 = hashlib.md5(project_path.encode()).hexdigest()
    store_db = tmp_cursor_dirs["chats"] / md5 / "sess1" / "store.db"
    create_store_db(store_db, blobs=[{"role": "user", "content": "hi"}])

    cached = make_session(
        id="sess1",
        project_path=project_path,
        provider=Provider.CURSOR,
        source_path=str(store_db),
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    class FakeCache:
        def get_sessions(self, path: str):
            return [cached] if path == str(store_db) else None

        def put_sessions(self, path: str, sessions):
            raise AssertionError("cache hit should not trigger put")

    provider = cursor.CursorProvider()
    provider._get_ide_sessions = lambda _p: []
    sessions = provider.get_sessions(project_path, cache=FakeCache())
    assert sessions == [cached]
