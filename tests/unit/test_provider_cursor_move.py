from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from sesh.models import Provider
from sesh.providers import cursor
from tests.helpers import create_store_db


def _read_blob_texts(store_db: Path) -> list[str]:
    conn = sqlite3.connect(store_db)
    try:
        cur = conn.cursor()
        cur.execute("SELECT data FROM blobs ORDER BY rowid")
        out = []
        for (blob_data,) in cur.fetchall():
            if isinstance(blob_data, bytes):
                out.append(blob_data.decode("utf-8"))
            else:
                out.append(str(blob_data))
        return out
    finally:
        conn.close()


def test_rewrite_workspace_json_updates_folder(tmp_path: Path) -> None:
    """workspace.json folder URI is rewritten from old to new."""
    workspace_json = tmp_path / "workspace.json"
    workspace_json.write_text(json.dumps({"folder": "file:///old"}))

    changed = cursor._rewrite_workspace_json(workspace_json, "file:///old", "file:///new")
    assert changed is True
    assert json.loads(workspace_json.read_text())["folder"] == "file:///new"


def test_rewrite_workspace_json_no_change_returns_false(tmp_path: Path) -> None:
    """When the folder URI doesn't match, the file is untouched and False is returned."""
    workspace_json = tmp_path / "workspace.json"
    workspace_json.write_text(json.dumps({"folder": "file:///other"}))
    assert cursor._rewrite_workspace_json(workspace_json, "file:///old", "file:///new") is False


def test_rewrite_store_db_blobs_replaces_paths(tmp_path: Path) -> None:
    """Old path references in store.db blob text are replaced with the new path."""
    store_db = tmp_path / "store.db"
    create_store_db(
        store_db,
        blobs=[
            {"role": "user", "content": "path /old/repo here"},
            {"role": "assistant", "content": "other"},
        ],
    )

    changed = cursor._rewrite_store_db_blobs(store_db, "/old/repo", "/new/repo")
    assert changed is True
    texts = _read_blob_texts(store_db)
    assert any("/new/repo" in t for t in texts)
    assert all("/old/repo" not in t for t in texts)


def test_rewrite_store_db_blobs_no_change_returns_false(tmp_path: Path) -> None:
    """When no blobs reference the old path, the DB is untouched and False is returned."""
    store_db = tmp_path / "store.db"
    create_store_db(store_db, blobs=[{"content": "nothing"}])
    assert cursor._rewrite_store_db_blobs(store_db, "/old", "/new") is False


def test_move_project_renames_dirs_and_rewrites_files(tmp_cursor_dirs) -> None:
    """Full Cursor move: renames chats + projects dirs, rewrites workspace.json and store.db blobs."""
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"

    old_md5 = hashlib.md5(old_path.encode()).hexdigest()
    old_chats_dir = tmp_cursor_dirs["chats"] / old_md5
    create_store_db(
        old_chats_dir / "sess1" / "store.db",
        blobs=[{"role": "user", "content": f"open {old_path}/file.py"}],
    )

    old_projects_dir = tmp_cursor_dirs["projects"] / cursor.encode_cursor_path(old_path)
    (old_projects_dir / "agent-transcripts").mkdir(parents=True)
    (old_projects_dir / "agent-transcripts" / "s1.txt").write_text("user:\nhi\n")

    ws_dir = tmp_cursor_dirs["workspace_storage"] / "ws1"
    ws_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(json.dumps({"folder": f"file://{old_path}"}))

    provider = cursor.CursorProvider()
    provider._workspace_map = {"stale": "x"}
    provider._projects_dir_map = {"stale": Path("/tmp/x")}
    report = provider.move_project(old_path, new_path)

    new_md5 = hashlib.md5(new_path.encode()).hexdigest()
    new_chats_dir = tmp_cursor_dirs["chats"] / new_md5
    new_projects_dir = tmp_cursor_dirs["projects"] / cursor.encode_cursor_path(new_path)

    assert report.provider is Provider.CURSOR
    assert report.success is True
    assert report.dirs_renamed == 2
    assert report.files_modified == 2
    assert report.error is None
    assert not old_chats_dir.exists()
    assert not old_projects_dir.exists()
    assert new_chats_dir.is_dir()
    assert new_projects_dir.is_dir()
    assert json.loads((ws_dir / "workspace.json").read_text())["folder"] == f"file://{new_path}"
    assert any(new_path in t for t in _read_blob_texts(new_chats_dir / "sess1" / "store.db"))
    assert provider._workspace_map is None
    assert provider._projects_dir_map is None


def test_move_project_conflict_returns_error(tmp_cursor_dirs) -> None:
    """Move fails when the target chats directory (md5 hash) already exists."""
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"
    (tmp_cursor_dirs["chats"] / hashlib.md5(old_path.encode()).hexdigest()).mkdir(parents=True)
    (tmp_cursor_dirs["chats"] / hashlib.md5(new_path.encode()).hexdigest()).mkdir(parents=True)

    report = cursor.CursorProvider().move_project(old_path, new_path)
    assert report.success is False
    assert report.provider is Provider.CURSOR
    assert report.error is not None
    assert "target chats directory exists" in report.error


def test_move_project_store_db_errors_become_warning(tmp_cursor_dirs, monkeypatch) -> None:
    """SQLite errors during store.db blob rewrite are collected as warnings, not failures."""
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"
    old_md5 = hashlib.md5(old_path.encode()).hexdigest()
    create_store_db(
        tmp_cursor_dirs["chats"] / old_md5 / "sess1" / "store.db",
        blobs=[{"content": old_path}],
    )

    def boom(_store_db: Path, _old: str, _new: str) -> bool:
        raise sqlite3.Error("db locked")

    monkeypatch.setattr(cursor, "_rewrite_store_db_blobs", boom)
    report = cursor.CursorProvider().move_project(old_path, new_path)
    assert report.success is True
    assert report.provider is Provider.CURSOR
    assert report.error is not None
    assert "Best-effort store.db update had errors" in report.error
