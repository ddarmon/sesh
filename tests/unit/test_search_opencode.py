from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from sesh import search
from sesh.models import Provider
from tests.helpers import create_opencode_db, write_opencode_storage_session


def _rg_match(file_path: str, line_text: str) -> str:
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": file_path},
                "lines": {"text": line_text},
            },
        }
    )


def test_search_opencode_db_matches_part_text(tmp_search_dirs) -> None:
    data_dir = tmp_search_dirs["opencode_data"]
    create_opencode_db(
        data_dir / "opencode.db",
        sessions=[{
            "id": "ses_db",
            "directory": "/Users/me/repo",
            "title": "t",
            "time_created": 1,
            "time_updated": 2,
        }],
        messages=[
            {"id": "msg_1", "session_id": "ses_db", "data": {"role": "user"}},
        ],
        parts=[
            {
                "id": "prt_1",
                "message_id": "msg_1",
                "session_id": "ses_db",
                "data": {"type": "text", "text": "the NeedleToken lives here"},
            },
        ],
    )

    results = search._search_opencode_db("needletoken", data_dir, None)
    assert len(results) == 1
    r = results[0]
    assert r.provider is Provider.OPENCODE
    assert r.session_id == "ses_db"
    assert r.project_path == "/Users/me/repo"
    assert "NeedleToken" in r.matched_line
    assert r.file_path.endswith("opencode.db")


def test_search_opencode_db_dedups_per_session(tmp_search_dirs) -> None:
    data_dir = tmp_search_dirs["opencode_data"]
    create_opencode_db(
        data_dir / "opencode.db",
        sessions=[{
            "id": "ses_db",
            "directory": "/Users/me/repo",
            "title": "t",
            "time_created": 1,
            "time_updated": 2,
        }],
        messages=[
            {"id": "msg_1", "session_id": "ses_db", "data": {"role": "user"}},
        ],
        parts=[
            {
                "id": f"prt_{i}",
                "message_id": "msg_1",
                "session_id": "ses_db",
                "data": {"type": "text", "text": f"needle occurrence {i}"},
            }
            for i in range(3)
        ],
    )

    results = search._search_opencode_db("needle", data_dir, None)
    assert len(results) == 1


def test_search_opencode_db_ignores_parts_hidden_by_staged_revert(
    tmp_search_dirs,
) -> None:
    data_dir = tmp_search_dirs["opencode_data"]
    db = data_dir / "opencode.db"
    create_opencode_db(
        db,
        sessions=[{
            "id": "ses_revert",
            "directory": "/Users/me/repo",
            "title": "t",
            "time_created": 1,
            "time_updated": 2,
        }],
        messages=[
            {"id": "msg_1", "session_id": "ses_revert", "data": {"role": "user"}},
            {"id": "msg_3", "session_id": "ses_revert", "data": {"role": "user"}},
        ],
        parts=[
            {"id": "prt_1", "message_id": "msg_1", "session_id": "ses_revert",
             "data": {"type": "text", "text": "active needle"}},
            {"id": "prt_3", "message_id": "msg_3", "session_id": "ses_revert",
             "data": {"type": "text", "text": "reverted needle"}},
        ],
    )
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE session ADD COLUMN revert TEXT")
        conn.execute(
            "UPDATE session SET revert = ? WHERE id = ?",
            (json.dumps({"messageID": "msg_3"}), "ses_revert"),
        )

    results = search._search_opencode_db("needle", data_dir, None)
    assert len(results) == 1
    assert results[0].matched_line == "active needle"


def test_search_opencode_storage_resolves_session_from_part_file(
    tmp_search_dirs, monkeypatch
) -> None:
    data_dir = tmp_search_dirs["opencode_data"]
    write_opencode_storage_session(
        data_dir,
        session_id="ses_st",
        project_id="proj_1",
        directory="/Users/me/storage-repo",
        messages=[{"id": "msg_1", "role": "user", "time": {"created": 1}}],
        parts={
            "msg_1": [{
                "id": "prt_1",
                "type": "text",
                "text": "a special needle in storage",
                "sessionID": "ses_st",
                "messageID": "msg_1",
            }],
        },
    )

    part_file = data_dir / "storage" / "part" / "msg_1" / "prt_1.json"
    stdout = _rg_match(str(part_file), '"text": "a special needle in storage",')
    monkeypatch.setattr(
        search.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout)
    )

    results = search._search_opencode_storage("rg", "needle", data_dir, None)
    assert len(results) == 1
    r = results[0]
    assert r.provider is Provider.OPENCODE
    assert r.session_id == "ses_st"
    assert r.project_path == "/Users/me/storage-repo"
    assert "needle" in r.matched_line
    # file_path points at the session info file so delete/clean can use it
    assert r.file_path.endswith("ses_st.json")


def test_search_opencode_storage_skips_reverted_match_then_finds_active_one(
    tmp_search_dirs, monkeypatch
) -> None:
    data_dir = tmp_search_dirs["opencode_data"]
    write_opencode_storage_session(
        data_dir,
        session_id="ses_revert",
        project_id="proj_1",
        directory="/Users/me/storage-repo",
        revert={"messageID": "msg_3"},
        messages=[
            {"id": "msg_1", "role": "user", "time": {"created": 1}},
            {"id": "msg_3", "role": "user", "time": {"created": 3}},
        ],
        parts={
            "msg_1": [{"id": "prt_1", "type": "text", "text": "active needle",
                       "sessionID": "ses_revert", "messageID": "msg_1"}],
            "msg_3": [{"id": "prt_3", "type": "text", "text": "reverted needle",
                       "sessionID": "ses_revert", "messageID": "msg_3"}],
        },
    )

    storage = data_dir / "storage" / "part"
    reverted = storage / "msg_3" / "prt_3.json"
    active = storage / "msg_1" / "prt_1.json"
    stdout = "\n".join([
        _rg_match(str(reverted), '"text": "reverted needle",'),
        _rg_match(str(active), '"text": "active needle",'),
    ])
    monkeypatch.setattr(
        search.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout)
    )

    results = search._search_opencode_storage("rg", "needle", data_dir, None)
    assert len(results) == 1
    assert results[0].matched_line == "active needle"


def test_search_opencode_storage_missing_dir_returns_empty(tmp_search_dirs) -> None:
    data_dir = tmp_search_dirs["opencode_data"]
    assert search._search_opencode_storage("rg", "x", data_dir, None) == []
    assert search._search_opencode_db("x", data_dir, None) == []


def test_search_one_host_dedups_db_and_storage(
    tmp_search_dirs, monkeypatch
) -> None:
    """A session present in both the DB and storage yields one result (DB wins)."""
    data_dir = tmp_search_dirs["opencode_data"]
    create_opencode_db(
        data_dir / "opencode.db",
        sessions=[{
            "id": "ses_dup",
            "directory": "/Users/me/repo",
            "title": "t",
            "time_created": 1,
            "time_updated": 2,
        }],
        messages=[
            {"id": "msg_1", "session_id": "ses_dup", "data": {"role": "user"}},
        ],
        parts=[
            {
                "id": "prt_1",
                "message_id": "msg_1",
                "session_id": "ses_dup",
                "data": {"type": "text", "text": "shared needle"},
            },
        ],
    )
    write_opencode_storage_session(
        data_dir,
        session_id="ses_dup",
        directory="/Users/me/repo",
        messages=[{"id": "msg_1", "role": "user", "time": {"created": 1}}],
        parts={
            "msg_1": [{
                "id": "prt_1",
                "type": "text",
                "text": "shared needle",
                "sessionID": "ses_dup",
            }],
        },
    )

    part_file = data_dir / "storage" / "part" / "msg_1" / "prt_1.json"
    stdout = _rg_match(str(part_file), '"text": "shared needle",')
    monkeypatch.setattr(
        search.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout)
    )
    monkeypatch.setattr(search.shutil, "which", lambda _: "rg")

    results = search.ripgrep_search("needle")
    opencode_results = [r for r in results if r.provider is Provider.OPENCODE]
    assert len(opencode_results) == 1
    assert opencode_results[0].file_path.endswith("opencode.db")
