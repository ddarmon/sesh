from __future__ import annotations

import sqlite3
from pathlib import Path

from sesh.providers.opencode import OpencodeProvider
from tests.helpers import (
    create_opencode_db,
    write_opencode_json,
    write_opencode_storage_session,
)


def test_delete_storage_session_removes_all_files(tmp_opencode_dir: Path) -> None:
    storage = tmp_opencode_dir / "storage"
    write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_del",
        directory="/Users/me/repo",
        messages=[{"id": "msg_001", "role": "user", "time": {"created": 1}}],
        parts={"msg_001": [{"id": "prt_a", "type": "text", "text": "hi"}]},
    )
    write_opencode_json(
        storage / "session_diff" / "ses_del.json", {"diffs": []}
    )
    # A second session in the same project must survive.
    write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_keep",
        directory="/Users/me/repo",
        messages=[{"id": "msg_k", "role": "user", "time": {"created": 1}}],
        parts={"msg_k": [{"id": "prt_k", "type": "text", "text": "keep"}]},
    )

    provider = OpencodeProvider()
    sessions = provider.get_sessions("/Users/me/repo")
    target = next(s for s in sessions if s.id == "ses_del")
    provider.delete_session(target)

    assert not (storage / "session" / "proj_001" / "ses_del.json").exists()
    assert not (storage / "message" / "ses_del").exists()
    assert not (storage / "part" / "msg_001").exists()
    assert not (storage / "session_diff" / "ses_del.json").exists()

    # Sibling session is untouched.
    assert (storage / "session" / "proj_001" / "ses_keep.json").exists()
    assert (storage / "message" / "ses_keep").exists()
    assert (storage / "part" / "msg_k").exists()

    remaining = OpencodeProvider().get_sessions("/Users/me/repo")
    assert [s.id for s in remaining] == ["ses_keep"]


def test_delete_db_session_removes_rows(tmp_opencode_dir: Path) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_db(
        db,
        sessions=[
            {"id": "ses_a", "directory": "/Users/me/repo", "title": "a",
             "time_created": 1, "time_updated": 2},
            {"id": "ses_b", "directory": "/Users/me/repo", "title": "b",
             "time_created": 1, "time_updated": 2},
        ],
        messages=[
            {"id": "msg_a", "session_id": "ses_a", "data": {"role": "user"}},
            {"id": "msg_b", "session_id": "ses_b", "data": {"role": "user"}},
        ],
        parts=[
            {"id": "prt_a", "message_id": "msg_a", "session_id": "ses_a",
             "data": {"type": "text", "text": "x"}},
            {"id": "prt_b", "message_id": "msg_b", "session_id": "ses_b",
             "data": {"type": "text", "text": "y"}},
        ],
    )

    provider = OpencodeProvider()
    target = next(s for s in provider.get_sessions("/Users/me/repo") if s.id == "ses_a")
    provider.delete_session(target)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM session WHERE id='ses_a'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM message WHERE session_id='ses_a'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM part WHERE session_id='ses_a'"
        ).fetchone()[0] == 0
        # The other session's rows survive.
        assert conn.execute(
            "SELECT COUNT(*) FROM session WHERE id='ses_b'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM message WHERE session_id='ses_b'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM part WHERE session_id='ses_b'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
