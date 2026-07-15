from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider
from sesh.providers import opencode
from sesh.providers.opencode import OpencodeProvider
from tests.helpers import (
    create_opencode_db,
    write_opencode_json,
    write_opencode_storage_session,
)


def _assistant_info(
    mid: str,
    *,
    created: int = 1750000100000,
    model: str = "claude-opus-4",
    tokens: dict | None = None,
) -> dict:
    info = {
        "id": mid,
        "role": "assistant",
        "time": {"created": created},
        "modelID": model,
        "providerID": "anthropic",
    }
    if tokens is not None:
        info["tokens"] = tokens
    return info


def _user_info(mid: str, *, created: int = 1750000050000) -> dict:
    return {"id": mid, "role": "user", "time": {"created": created}}


def _tokens(inp: int, out: int, read: int = 0, write: int = 0) -> dict:
    return {
        "input": inp,
        "output": out,
        "reasoning": 0,
        "cache": {"read": read, "write": write},
    }


def test_parse_timestamp_epoch_millis() -> None:
    assert opencode._parse_timestamp(1750000000000) == datetime.fromtimestamp(
        1750000000.0, tz=timezone.utc
    )


def test_parse_timestamp_iso_z() -> None:
    assert opencode._parse_timestamp("2026-04-03T11:10:54.342Z") == datetime(
        2026, 4, 3, 11, 10, 54, 342000, tzinfo=timezone.utc
    )


# ----------------------------------------------------------------------
# Legacy JSON storage layout
# ----------------------------------------------------------------------


def test_storage_discovers_project_from_directory_field(tmp_opencode_dir: Path) -> None:
    write_opencode_storage_session(
        tmp_opencode_dir,
        project_id="proj_abc123",
        directory="/Users/me/myrepo",
    )
    provider = OpencodeProvider()
    projects = list(provider.discover_projects())
    assert projects == [("/Users/me/myrepo", "myrepo")]


def test_storage_session_metadata_and_tokens(tmp_opencode_dir: Path) -> None:
    write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_one",
        directory="/Users/me/repo",
        title="Refactor parser",
        created=1750000000000,
        updated=1750000600000,
        messages=[
            _user_info("msg_001"),
            _assistant_info(
                "msg_002", tokens=_tokens(100, 20, read=50, write=10)
            ),
            _user_info("msg_003"),
            _assistant_info(
                "msg_004", tokens=_tokens(200, 30, read=80, write=0)
            ),
        ],
    )

    provider = OpencodeProvider()
    sessions = provider.get_sessions("/Users/me/repo")
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == "ses_one"
    assert s.provider is Provider.OPENCODE
    assert s.summary == "Refactor parser"
    assert s.message_count == 4
    assert s.model == "claude-opus-4"
    assert s.timestamp == opencode._parse_timestamp(1750000600000)
    assert s.start_timestamp == opencode._parse_timestamp(1750000000000)
    # Last turn context: 200 + 80 + 0
    assert s.input_tokens == 280
    # Output summed across turns: 20 + 30
    assert s.output_tokens == 50
    # Cumulative input: (100+50+10) + (200+80)
    assert s.cumulative_input_tokens == 440
    assert s.source_path is not None and s.source_path.endswith("ses_one.json")


def test_storage_staged_revert_hides_tail_but_keeps_incurred_usage(
    tmp_opencode_dir: Path,
) -> None:
    info_file = write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_revert",
        directory="/Users/me/repo",
        messages=[
            _user_info("msg_001", created=1),
            _assistant_info(
                "msg_002", created=2, model="old-model",
                tokens=_tokens(100, 20, read=10),
            ),
            _user_info("msg_003", created=3),
            _assistant_info(
                "msg_004", created=4, model="undone-model",
                tokens=_tokens(200, 30, read=20),
            ),
        ],
        parts={
            "msg_001": [{"id": "prt_001", "type": "text", "text": "keep user"}],
            "msg_002": [{"id": "prt_002", "type": "text", "text": "keep answer"}],
            "msg_003": [{"id": "prt_003", "type": "text", "text": "undo user"}],
            "msg_004": [{"id": "prt_004", "type": "text", "text": "undo answer"}],
        },
    )

    live_provider = OpencodeProvider()
    live_session = live_provider.get_sessions("/Users/me/repo")[0]
    assert [m.content for m in live_provider.get_messages(live_session)] == [
        "keep user", "keep answer", "undo user", "undo answer"
    ]

    info = opencode._load_json(info_file)
    assert info is not None
    info["revert"] = {"messageID": "msg_003", "snapshot": "snap"}
    write_opencode_json(info_file, info)

    # The already-selected session object sees the newly staged undo on reload.
    assert [m.content for m in live_provider.get_messages(live_session)] == [
        "keep user", "keep answer"
    ]

    session = OpencodeProvider().get_sessions("/Users/me/repo")[0]
    assert session.message_count == 2
    assert session.model == "old-model"
    assert session.input_tokens == 110
    # Undo keeps physical records for redo, so incurred totals still include them.
    assert session.output_tokens == 50
    assert session.cumulative_input_tokens == 330


def test_storage_part_revert_keeps_only_earlier_target_parts(
    tmp_opencode_dir: Path,
) -> None:
    write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_part_revert",
        directory="/Users/me/repo",
        revert={"messageID": "msg_002", "partID": "prt_003"},
        messages=[
            _user_info("msg_001", created=1),
            _assistant_info("msg_002", created=2),
            _user_info("msg_003", created=3),
        ],
        parts={
            "msg_001": [{"id": "prt_001", "type": "text", "text": "prompt"}],
            "msg_002": [
                {"id": "prt_002", "type": "text", "text": "kept prefix"},
                {"id": "prt_003", "type": "text", "text": "removed suffix"},
            ],
            "msg_003": [{"id": "prt_004", "type": "text", "text": "removed turn"}],
        },
    )

    provider = OpencodeProvider()
    session = provider.get_sessions("/Users/me/repo")[0]
    assert session.message_count == 2
    assert [m.content for m in provider.get_messages(session)] == [
        "prompt", "kept prefix"
    ]


def test_storage_session_without_tokens_omits_token_fields(
    tmp_opencode_dir: Path,
) -> None:
    write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_two",
        directory="/Users/me/repo",
        messages=[_user_info("msg_001"), _assistant_info("msg_002")],
    )
    provider = OpencodeProvider()
    s = provider.get_sessions("/Users/me/repo")[0]
    assert s.input_tokens is None
    assert s.output_tokens is None
    assert s.cumulative_input_tokens is None


def test_storage_session_without_directory_is_skipped(tmp_opencode_dir: Path) -> None:
    storage = tmp_opencode_dir / "storage"
    write_opencode_json(
        storage / "session" / "proj_x" / "ses_bad.json",
        {"id": "ses_bad", "title": "No directory"},
    )
    provider = OpencodeProvider()
    assert list(provider.discover_projects()) == []


def test_storage_get_messages_maps_part_types(tmp_opencode_dir: Path) -> None:
    info_file = write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_msgs",
        directory="/Users/me/repo",
        messages=[
            _user_info("msg_001"),
            _assistant_info("msg_002"),
        ],
        parts={
            "msg_001": [
                {"id": "prt_a", "type": "text", "text": "please fix the bug"},
                {
                    "id": "prt_b",
                    "type": "text",
                    "text": "injected context",
                    "synthetic": True,
                },
            ],
            "msg_002": [
                {"id": "prt_c", "type": "reasoning", "text": "thinking hard"},
                {
                    "id": "prt_d",
                    "type": "tool",
                    "callID": "call1",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "ls"},
                        "output": "file.txt",
                        "title": "ls",
                        "metadata": {},
                        "time": {"start": 1, "end": 2},
                    },
                },
                {"id": "prt_e", "type": "text", "text": "done!"},
                {"id": "prt_f", "type": "step-finish", "reason": "stop"},
            ],
        },
    )

    provider = OpencodeProvider()
    session = provider.get_sessions("/Users/me/repo")[0]
    assert session.source_path == str(info_file)

    messages = provider.get_messages(session)
    kinds = [(m.role, m.content_type) for m in messages]
    assert kinds == [
        ("user", "text"),
        ("user", "text"),
        ("assistant", "thinking"),
        ("assistant", "tool_use"),
        ("tool", "tool_result"),
        ("assistant", "text"),
    ]
    assert messages[0].content == "please fix the bug"
    assert not messages[0].is_system
    assert messages[1].is_system  # synthetic text
    assert messages[2].thinking == "thinking hard"
    assert messages[3].tool_name == "bash"
    assert "ls" in (messages[3].tool_input or "")
    assert messages[4].tool_output == "file.txt"
    assert messages[5].content == "done!"


def test_storage_nested_part_layout_fallback(tmp_opencode_dir: Path) -> None:
    info_file = write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_nested",
        directory="/Users/me/repo",
        messages=[_user_info("msg_001")],
    )
    # Older layout: part/{sessionID}/{messageID}/{partID}.json
    write_opencode_json(
        tmp_opencode_dir / "storage" / "part" / "ses_nested" / "msg_001" / "prt_x.json",
        {"id": "prt_x", "type": "text", "text": "old layout text"},
    )

    provider = OpencodeProvider()
    session = provider.get_sessions("/Users/me/repo")[0]
    assert session.source_path == str(info_file)
    messages = provider.get_messages(session)
    assert [m.content for m in messages] == ["old layout text"]


def test_storage_tool_error_state_becomes_tool_result(tmp_opencode_dir: Path) -> None:
    write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_err",
        directory="/Users/me/repo",
        messages=[_assistant_info("msg_001")],
        parts={
            "msg_001": [
                {
                    "id": "prt_a",
                    "type": "tool",
                    "callID": "c1",
                    "tool": "bash",
                    "state": {
                        "status": "error",
                        "input": {"command": "boom"},
                        "error": "exploded",
                        "time": {"start": 1, "end": 2},
                    },
                },
            ],
        },
    )
    provider = OpencodeProvider()
    session = provider.get_sessions("/Users/me/repo")[0]
    messages = provider.get_messages(session)
    assert [m.content_type for m in messages] == ["tool_use", "tool_result"]
    assert messages[1].tool_output == "exploded"


# ----------------------------------------------------------------------
# SQLite database layout
# ----------------------------------------------------------------------


def test_db_discovers_sessions_and_tokens(tmp_opencode_dir: Path) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_db(
        db,
        sessions=[{
            "id": "ses_db1",
            "directory": "/Users/me/dbrepo",
            "title": "DB session",
            "time_created": 1750000000000,
            "time_updated": 1750000900000,
            "model": {"id": "gpt-5", "providerID": "openai"},
            "tokens_input": 300,
            "tokens_output": 55,
            "tokens_cache_read": 120,
            "tokens_cache_write": 30,
        }],
        messages=[
            {
                "id": "msg_001",
                "session_id": "ses_db1",
                "data": {"role": "user", "time": {"created": 1750000000000}},
            },
            {
                "id": "msg_002",
                "session_id": "ses_db1",
                "data": _assistant_info(
                    "msg_002", model="gpt-5", tokens=_tokens(200, 30, read=80)
                ),
            },
        ],
    )

    provider = OpencodeProvider()
    assert list(provider.discover_projects()) == [("/Users/me/dbrepo", "dbrepo")]
    s = provider.get_sessions("/Users/me/dbrepo")[0]
    assert s.id == "ses_db1"
    assert s.summary == "DB session"
    assert s.model == "gpt-5"
    assert s.message_count == 2
    assert s.source_path == str(db)
    # Last turn context from the last assistant message: 200 + 80
    assert s.input_tokens == 280
    # Session columns are cumulative sums
    assert s.output_tokens == 55
    assert s.cumulative_input_tokens == 300 + 120 + 30


def test_db_staged_revert_filters_messages_and_active_metadata(
    tmp_opencode_dir: Path,
) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_db(
        db,
        sessions=[{
            "id": "ses_db_revert",
            "directory": "/Users/me/dbrepo",
            "title": "undo",
            "time_created": 1,
            "time_updated": 4,
            "tokens_input": 300,
            "tokens_output": 50,
            "tokens_cache_read": 30,
        }],
        messages=[
            {"id": "msg_001", "session_id": "ses_db_revert",
             "data": _user_info("msg_001", created=1)},
            {"id": "msg_002", "session_id": "ses_db_revert",
             "data": _assistant_info("msg_002", created=2, model="old-model",
                                     tokens=_tokens(100, 20, read=10))},
            {"id": "msg_003", "session_id": "ses_db_revert",
             "data": _user_info("msg_003", created=3)},
            {"id": "msg_004", "session_id": "ses_db_revert",
             "data": _assistant_info("msg_004", created=4, model="undone-model",
                                     tokens=_tokens(200, 30, read=20))},
        ],
        parts=[
            {"id": f"prt_{i:03}", "message_id": f"msg_{i:03}",
             "session_id": "ses_db_revert",
             "data": {"type": "text", "text": text}}
            for i, text in enumerate(
                ("keep user", "keep answer", "undo user", "undo answer"), start=1
            )
        ],
    )
    live_provider = OpencodeProvider()
    live_session = live_provider.get_sessions("/Users/me/dbrepo")[0]
    assert [m.content for m in live_provider.get_messages(live_session)] == [
        "keep user", "keep answer", "undo user", "undo answer"
    ]

    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE session ADD COLUMN revert TEXT")
        conn.execute(
            "UPDATE session SET revert = ? WHERE id = ?",
            (json.dumps({"messageID": "msg_003", "snapshot": "snap"}),
             "ses_db_revert"),
        )

    # Follow reloads through the selected session and observes the new cutoff.
    assert [m.content for m in live_provider.get_messages(live_session)] == [
        "keep user", "keep answer"
    ]

    session = OpencodeProvider().get_sessions("/Users/me/dbrepo")[0]
    assert session.message_count == 2
    assert session.model == "old-model"
    assert session.input_tokens == 110
    assert session.output_tokens == 50
    assert session.cumulative_input_tokens == 330


def test_db_part_revert_keeps_only_parts_before_cutoff(
    tmp_opencode_dir: Path,
) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_db(
        db,
        sessions=[{
            "id": "ses_db_part",
            "directory": "/Users/me/dbrepo",
            "title": "partial undo",
            "time_created": 1,
            "time_updated": 3,
        }],
        messages=[
            {"id": "msg_001", "session_id": "ses_db_part",
             "data": _user_info("msg_001", created=1)},
            {"id": "msg_002", "session_id": "ses_db_part",
             "data": _assistant_info("msg_002", created=2)},
            {"id": "msg_003", "session_id": "ses_db_part",
             "data": _user_info("msg_003", created=3)},
        ],
        parts=[
            {"id": "prt_001", "message_id": "msg_001", "session_id": "ses_db_part",
             "data": {"type": "text", "text": "prompt"}},
            {"id": "prt_002", "message_id": "msg_002", "session_id": "ses_db_part",
             "data": {"type": "text", "text": "kept prefix"}},
            {"id": "prt_003", "message_id": "msg_002", "session_id": "ses_db_part",
             "data": {"type": "text", "text": "removed suffix"}},
            {"id": "prt_004", "message_id": "msg_003", "session_id": "ses_db_part",
             "data": {"type": "text", "text": "removed turn"}},
        ],
    )
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE session ADD COLUMN revert TEXT")
        conn.execute(
            "UPDATE session SET revert = ? WHERE id = ?",
            (json.dumps({"messageID": "msg_002", "partID": "prt_003"}),
             "ses_db_part"),
        )

    provider = OpencodeProvider()
    session = provider.get_sessions("/Users/me/dbrepo")[0]
    assert session.message_count == 2
    assert [m.content for m in provider.get_messages(session)] == [
        "prompt", "kept prefix"
    ]


def test_db_get_messages_joins_parts(tmp_opencode_dir: Path) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_db(
        db,
        sessions=[{
            "id": "ses_db2",
            "directory": "/Users/me/dbrepo",
            "title": "t",
            "time_created": 1,
            "time_updated": 2,
        }],
        messages=[
            {
                "id": "msg_001",
                "session_id": "ses_db2",
                "data": {"role": "user", "time": {"created": 1750000000000}},
            },
            {
                "id": "msg_002",
                "session_id": "ses_db2",
                "data": _assistant_info("msg_002"),
            },
        ],
        parts=[
            {
                "id": "prt_a",
                "message_id": "msg_001",
                "session_id": "ses_db2",
                "data": {"type": "text", "text": "hello there"},
            },
            {
                "id": "prt_b",
                "message_id": "msg_002",
                "session_id": "ses_db2",
                "data": {"type": "reasoning", "text": "hmm"},
            },
            {
                "id": "prt_c",
                "message_id": "msg_002",
                "session_id": "ses_db2",
                "data": {"type": "text", "text": "hi!"},
            },
        ],
    )

    provider = OpencodeProvider()
    session = provider.get_sessions("/Users/me/dbrepo")[0]
    messages = provider.get_messages(session)
    assert [(m.role, m.content_type) for m in messages] == [
        ("user", "text"),
        ("assistant", "thinking"),
        ("assistant", "text"),
    ]
    assert messages[0].content == "hello there"


def test_db_takes_precedence_over_storage_for_same_session(
    tmp_opencode_dir: Path,
) -> None:
    create_opencode_db(
        tmp_opencode_dir / "opencode.db",
        sessions=[{
            "id": "ses_dup",
            "directory": "/Users/me/repo",
            "title": "From DB",
            "time_created": 1,
            "time_updated": 2,
        }],
    )
    write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_dup",
        directory="/Users/me/repo",
        title="From storage",
    )

    provider = OpencodeProvider()
    sessions = provider.get_sessions("/Users/me/repo")
    assert len(sessions) == 1
    assert sessions[0].summary == "From DB"
    assert sessions[0].source_path is not None
    assert sessions[0].source_path.endswith(".db")


def test_missing_data_dir_yields_nothing(tmp_opencode_dir: Path) -> None:
    provider = OpencodeProvider()
    assert list(provider.discover_projects()) == []
    assert provider.get_sessions("/Users/me/repo") == []


def test_aggregation_base_dir_and_host(tmp_path: Path) -> None:
    host_dir = tmp_path / "laptop"
    data_dir = host_dir / ".local" / "share" / "opencode"
    write_opencode_storage_session(
        data_dir,
        session_id="ses_agg",
        directory="/Users/me/repo",
    )
    provider = OpencodeProvider(base_dir=host_dir, host="laptop")
    assert list(provider.discover_projects()) == [("/Users/me/repo", "repo")]
    s = provider.get_sessions("/Users/me/repo")[0]
    assert s.host == "laptop"
