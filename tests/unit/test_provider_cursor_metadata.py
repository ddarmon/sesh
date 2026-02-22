from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider
from sesh.providers import cursor
from tests.helpers import create_store_db, make_session


def test_stringify_tool_value() -> None:
    """None returns empty string; strings pass through; dicts are JSON-stringified."""
    assert cursor._stringify_tool_value(None) == ""
    assert cursor._stringify_tool_value("x") == "x"
    rendered = cursor._stringify_tool_value({"a": 1})
    assert isinstance(rendered, str)
    assert '"a"' in rendered


def test_decode_value_hex_encoded_json(tmp_cursor_dirs) -> None:
    """Hex-encoded JSON in the SQLite meta table is decoded and parsed."""
    provider = cursor.CursorProvider()
    payload = {"name": "Session", "createdAt": 1735689600000}
    value = json.dumps(payload).encode("utf-8").hex()
    assert provider._decode_value(value) == payload


def test_decode_value_plain_json(tmp_cursor_dirs) -> None:
    """Plain JSON string in the meta table is parsed directly."""
    provider = cursor.CursorProvider()
    assert provider._decode_value('{"x":1}') == {"x": 1}


def test_decode_value_fallback_text(tmp_cursor_dirs) -> None:
    """Non-JSON, non-hex values are returned as raw text."""
    provider = cursor.CursorProvider()
    assert provider._decode_value("not json") == "not json"


def test_read_session_meta_from_store_db(tmp_path: Path, tmp_cursor_dirs) -> None:
    """Session metadata (title, model, timestamp, message count) is extracted from store.db."""
    db_path = tmp_path / "store.db"
    meta_payload = {
        "name": "Debug session",
        "createdAt": 1_735_689_600_000,
        "lastUsedModel": "gpt-4.1",
    }
    create_store_db(
        db_path,
        blobs=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"foo": "bar"},
        ],
        meta={"0": json.dumps(meta_payload).encode("utf-8").hex()},
    )

    meta = cursor.CursorProvider()._read_session_meta(db_path)
    assert meta is not None
    assert meta["title"] == "Debug session"
    assert meta["model"] == "gpt-4.1"
    assert meta["message_count"] == 2
    assert meta["timestamp"] == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_read_session_meta_falls_back_to_mtime(tmp_path: Path, tmp_cursor_dirs) -> None:
    """Without createdAt in metadata, timestamp falls back to file mtime."""
    db_path = tmp_path / "store.db"
    create_store_db(db_path, blobs=[{"role": "user", "content": "hi"}], meta={"0": "{}"})
    os.utime(db_path, (1_735_689_600, 1_735_689_600))

    meta = cursor.CursorProvider()._read_session_meta(db_path)
    assert meta is not None
    assert meta["title"] == "Untitled Session"
    assert meta["message_count"] == 1
    assert meta["timestamp"] == datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_extract_workspace_path_from_chat_hash_dir(tmp_path: Path, tmp_cursor_dirs) -> None:
    """Workspace path is extracted from the first 'Workspace Path:' line in store.db blobs."""
    hash_dir = tmp_path / "hash"
    create_store_db(
        hash_dir / "sess-1" / "store.db",
        blobs=[
            {"content": "Workspace Path: /Users/me/repo\nOther: x"},
        ],
    )

    assert cursor.CursorProvider._extract_workspace_path(hash_dir) == "/Users/me/repo"


def test_first_user_message_parses_transcript(tmp_path: Path) -> None:
    """First user message is extracted from <user_query> tags in a Cursor transcript."""
    transcript = tmp_path / "t.txt"
    transcript.write_text(
        "\n".join(
            [
                "system:",
                "ignored",
                "user:",
                "<user_query>",
                "First line",
                "Second line",
                "</user_query>",
                "",
                "assistant:",
                "Reply",
            ]
        )
        + "\n"
    )

    assert cursor.CursorProvider._first_user_message(transcript) == "First line Second line"


def test_count_transcript_messages_counts_user_and_assistant(tmp_path: Path) -> None:
    """Only 'user:' and 'assistant:' role lines are counted (system: lines excluded)."""
    transcript = tmp_path / "t.txt"
    transcript.write_text(
        "user:\nhello\nassistant:\nhi\nsystem:\nnope\nuser:\nagain\n"
    )
    assert cursor.CursorProvider._count_transcript_messages(transcript) == 3


def test_delete_session_txt_removes_file(tmp_path: Path, tmp_cursor_dirs) -> None:
    """Deleting a .txt transcript session removes the file."""
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("user:\nhi\n")
    session = make_session(
        id="s1", provider=Provider.CURSOR, source_path=str(transcript)
    )
    cursor.CursorProvider().delete_session(session)
    assert not transcript.exists()


def test_delete_session_store_db_removes_parent_dir(tmp_path: Path, tmp_cursor_dirs) -> None:
    """Deleting a store.db session removes the parent directory (session container)."""
    db_path = tmp_path / "hash" / "session-1" / "store.db"
    create_store_db(db_path, blobs=[{"role": "user", "content": "hi"}])
    session = make_session(
        id="s1", provider=Provider.CURSOR, source_path=str(db_path)
    )
    cursor.CursorProvider().delete_session(session)
    assert not db_path.parent.exists()
