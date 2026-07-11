from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider
from sesh.providers import codex
from tests.helpers import make_session, write_jsonl


def test_parse_timestamp_z() -> None:
    """ISO timestamps with 'Z' suffix parse as UTC datetime."""
    assert codex._parse_timestamp("2025-02-01T12:00:00Z") == datetime(
        2025, 2, 1, 12, 0, 0, tzinfo=timezone.utc
    )


def test_extract_text_from_content() -> None:
    """Content list items with text/input_text/output_text keys are joined with newlines."""
    content = [
        {"text": "one"},
        {"input_text": "two"},
        {"output_text": "three"},
        {"x": "ignored"},
    ]
    assert codex._extract_text_from_content(content) == "one\ntwo\nthree"


def test_stringify_tool_value() -> None:
    """None returns empty string; dicts are JSON-stringified."""
    assert codex._stringify_tool_value(None) == ""
    rendered = codex._stringify_tool_value({"a": 1})
    assert isinstance(rendered, str)
    assert '"a"' in rendered


def test_parse_session_file_new_format(tmp_path: Path) -> None:
    """New-format Codex JSONL (with session_meta entry) extracts id, cwd, model, and summary."""
    file_path = tmp_path / "new.jsonl"
    first_prompt = "p" * 90
    write_jsonl(
        file_path,
        [
            {
                "type": "session_meta",
                "timestamp": "2025-02-01T00:00:00Z",
                "payload": {"id": "sess-1", "cwd": "/repo", "model": "gpt-4.1"},
            },
            {
                "type": "event_msg",
                "timestamp": "2025-02-01T00:00:01Z",
                "payload": {"type": "user_message", "message": first_prompt},
            },
            {
                "type": "response_item",
                "timestamp": "2025-02-01T00:00:02Z",
                "payload": {"role": "assistant", "content": [{"text": "ok"}]},
            },
        ],
    )

    data = codex.CodexProvider()._parse_session_file(file_path)
    assert data is not None
    assert data["id"] == "sess-1"
    assert data["cwd"] == "/repo"
    assert data["model"] == "gpt-4.1"
    assert data["timestamp"] == datetime(2025, 2, 1, 0, 0, 2, tzinfo=timezone.utc)
    assert data["start_timestamp"] == datetime(2025, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert data["message_count"] == 2
    assert data["summary"] == first_prompt[:80] + "..."
    assert data["file_path"] == str(file_path)


def test_parse_session_file_gets_model_from_turn_context(tmp_path: Path) -> None:
    """Current Codex stores the provider in session_meta and model in turn_context."""
    file_path = tmp_path / "current.jsonl"
    write_jsonl(
        file_path,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-10T00:00:00Z",
                "payload": {
                    "id": "sess-current",
                    "cwd": "/repo",
                    "model_provider": "openai",
                },
            },
            {
                "type": "turn_context",
                "timestamp": "2026-07-10T00:00:01Z",
                "payload": {"model": "gpt-5.6-sol"},
            },
        ],
    )

    data = codex.CodexProvider()._parse_session_file(file_path)

    assert data is not None
    assert data["model"] == "gpt-5.6-sol"


def test_parse_session_file_legacy_format(tmp_path: Path) -> None:
    """Legacy Codex JSONL (with <cwd> tags) extracts the project path from the tag."""
    file_path = tmp_path / "legacy.jsonl"
    write_jsonl(
        file_path,
        [
            {
                "type": "response_item",
                "timestamp": "2025-02-01T00:00:00Z",
                "payload": {"content": [{"text": "<cwd>/legacy/repo</cwd>"}]},
            },
            {
                "type": "event_msg",
                "timestamp": "2025-02-01T00:00:01Z",
                "payload": {"type": "user_message", "message": "Hello world"},
            },
        ],
    )

    data = codex.CodexProvider()._parse_session_file(file_path)
    assert data is not None
    assert data["id"] == "legacy"
    assert data["cwd"] == "/legacy/repo"
    assert data["model"] == ""
    assert data["start_timestamp"] == datetime(2025, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert data["message_count"] == 1
    assert data["summary"] == "Hello world"


def test_parse_session_file_legacy_without_cwd_returns_none(tmp_path: Path) -> None:
    """Legacy file without a <cwd> tag returns None (can't determine project)."""
    file_path = tmp_path / "legacy.jsonl"
    write_jsonl(
        file_path,
        [
            {"type": "response_item", "payload": {"content": [{"text": "no cwd tag"}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
        ],
    )
    assert codex.CodexProvider()._parse_session_file(file_path) is None


def test_discover_projects_skips_invalid_paths_and_get_sessions_sorted(tmp_codex_dir) -> None:
    """Root '/' cwd is skipped; valid sessions are sorted newest-first per project."""
    write_jsonl(
        tmp_codex_dir / "a.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2025-02-01T00:00:00Z",
                "payload": {"id": "a", "cwd": "/repo", "model": "gpt"},
            },
            {
                "type": "event_msg",
                "timestamp": "2025-02-01T00:00:01Z",
                "payload": {"type": "user_message", "message": "First"},
            },
        ],
    )
    write_jsonl(
        tmp_codex_dir / "nested" / "b.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2025-02-02T00:00:00Z",
                "payload": {"id": "b", "cwd": "/repo", "model": "gpt"},
            },
            {
                "type": "event_msg",
                "timestamp": "2025-02-02T00:00:01Z",
                "payload": {"type": "user_message", "message": "Second"},
            },
        ],
    )
    write_jsonl(
        tmp_codex_dir / "root.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2025-02-03T00:00:00Z",
                "payload": {"id": "root", "cwd": "/", "model": "gpt"},
            },
        ],
    )

    provider = codex.CodexProvider()
    projects = list(provider.discover_projects())
    assert projects == [("/repo", "repo")]

    sessions = provider.get_sessions("/repo")
    assert [s.id for s in sessions] == ["b", "a"]
    assert all(s.provider is Provider.CODEX for s in sessions)
    assert [s.start_timestamp for s in sessions] == [
        datetime(2025, 2, 2, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2025, 2, 1, 0, 0, 0, tzinfo=timezone.utc),
    ]


def test_build_index_uses_cached_sessions(tmp_codex_dir) -> None:
    """Per-file cache hit skips JSONL parsing and uses the cached session data."""
    file_path = tmp_codex_dir / "cached.jsonl"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("{}\n")
    cached_session = make_session(
        id="cached",
        project_path="/cached-repo",
        provider=Provider.CODEX,
        source_path=str(file_path),
        timestamp=datetime(2025, 2, 1, tzinfo=timezone.utc),
        start_timestamp=datetime(2025, 2, 1, 0, 0, 0, tzinfo=timezone.utc),
    )

    class FakeCache:
        def get_sessions(self, path: str):
            assert path == str(file_path)
            return [cached_session]

        def put_sessions(self, path: str, sessions) -> None:
            raise AssertionError("should not write cache on cache hit")

    provider = codex.CodexProvider(cache=FakeCache())
    provider._parse_session_file = lambda _p: (_ for _ in ()).throw(AssertionError("should not parse"))
    index = provider._build_index()

    assert "/cached-repo" in index
    assert index["/cached-repo"][0]["id"] == "cached"
    assert index["/cached-repo"][0]["start_timestamp"] == datetime(2025, 2, 1, tzinfo=timezone.utc)


def test_parse_session_file_extracts_token_count(tmp_path: Path) -> None:
    """input_tokens uses last_token_usage (per-turn context); output_tokens uses total_token_usage (cumulative)."""
    file_path = tmp_path / "tokens.jsonl"
    write_jsonl(
        file_path,
        [
            {
                "type": "session_meta",
                "timestamp": "2025-02-01T00:00:00Z",
                "payload": {"id": "sess-tok", "cwd": "/repo", "model": "gpt-4.1"},
            },
            {
                "type": "event_msg",
                "timestamp": "2025-02-01T00:00:01Z",
                "payload": {"type": "user_message", "message": "hello"},
            },
            {
                "type": "event_msg",
                "timestamp": "2025-02-01T00:00:02Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 500,
                            "output_tokens": 100,
                        },
                        "total_token_usage": {
                            "input_tokens": 500,
                            "output_tokens": 100,
                        },
                    },
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2025-02-01T00:00:03Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 800,
                            "output_tokens": 150,
                        },
                        "total_token_usage": {
                            "input_tokens": 1300,
                            "output_tokens": 250,
                        },
                    },
                },
            },
        ],
    )

    data = codex.CodexProvider()._parse_session_file(file_path)
    assert data is not None
    # input_tokens = last entry's last_token_usage (context size of final turn)
    assert data["input_tokens"] == 800
    # output_tokens = last entry's total_token_usage (cumulative output)
    assert data["output_tokens"] == 250
    # cumulative_input_tokens = last entry's total_token_usage (cumulative input)
    assert data["cumulative_input_tokens"] == 1300


def test_parse_session_file_no_token_count(tmp_path: Path) -> None:
    """Sessions without token_count events have None token fields."""
    file_path = tmp_path / "notokens.jsonl"
    write_jsonl(
        file_path,
        [
            {
                "type": "session_meta",
                "timestamp": "2025-02-01T00:00:00Z",
                "payload": {"id": "sess-no", "cwd": "/repo", "model": "gpt-4.1"},
            },
            {
                "type": "event_msg",
                "timestamp": "2025-02-01T00:00:01Z",
                "payload": {"type": "user_message", "message": "hello"},
            },
        ],
    )

    data = codex.CodexProvider()._parse_session_file(file_path)
    assert data is not None
    assert data["input_tokens"] is None
    assert data["output_tokens"] is None


def test_delete_session_unlinks_source_file(tmp_path: Path) -> None:
    """Codex delete_session removes the entire session JSONL file."""
    file_path = tmp_path / "session.jsonl"
    file_path.write_text("{}\n")
    session = make_session(id="s1", provider=Provider.CODEX, source_path=str(file_path))
    codex.CodexProvider().delete_session(session)
    assert not file_path.exists()


def _codex_subagent_entries(
    *, child_id: str, root_id: str, agent_path: str, timestamp: str
) -> list[dict]:
    return [
        {
            "type": "session_meta",
            "timestamp": timestamp,
            "payload": {
                "id": child_id,
                "session_id": root_id,
                "parent_thread_id": root_id,
                "cwd": "/repo",
                "thread_source": "subagent",
                "agent_path": agent_path,
                "agent_nickname": "Sagan",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": root_id,
                            "depth": 1,
                            "agent_path": agent_path,
                            "agent_nickname": "Sagan",
                        }
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": timestamp,
            "payload": {"type": "user_message", "message": "inherited parent prompt"},
        },
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "type": "agent_message",
                "author": "/root",
                "recipient": agent_path,
                "content": [
                    {"type": "input_text", "text": "Message Type: NEW_TASK\nPayload:\n"}
                ],
            },
        },
        {
            "type": "event_msg",
            "timestamp": timestamp,
            "payload": {"type": "agent_reasoning", "text": "child reasoning"},
        },
        {
            "type": "response_item",
            "timestamp": timestamp,
            "payload": {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "child answer"}],
            },
        },
    ]


def test_native_subagents_are_attached_not_listed(tmp_codex_dir) -> None:
    """Native child rollouts become lazy subagents rather than standalone sessions."""
    write_jsonl(
        tmp_codex_dir / "root.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-07-11T18:00:00Z",
                "payload": {"id": "root-id", "cwd": "/repo"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-11T18:00:01Z",
                "payload": {"type": "user_message", "message": "root prompt"},
            },
        ],
    )
    write_jsonl(
        tmp_codex_dir / "child.jsonl",
        _codex_subagent_entries(
            child_id="child-id",
            root_id="root-id",
            agent_path="/root/inspect_tests",
            timestamp="2026-07-11T18:00:02Z",
        ),
    )

    provider = codex.CodexProvider()
    sessions = provider.get_sessions("/repo")

    assert [session.id for session in sessions] == ["root-id"]
    assert sessions[0].subagent_count == 1

    loaded = provider.load_subagents(sessions[0])
    assert len(loaded) == 1
    meta, messages = loaded[0]
    assert meta.agent_id == "child-id"
    assert meta.description == "/root/inspect_tests"
    assert meta.agent_type == "Sagan"
    assert meta.is_fork is True
    assert [message.content for message in messages if message.content] == ["child answer"]
    assert [message.thinking for message in messages if message.thinking] == ["child reasoning"]
    assert all(message.content != "inherited parent prompt" for message in messages)


def test_codex_subagents_are_sorted_by_child_start_time(tmp_codex_dir) -> None:
    write_jsonl(
        tmp_codex_dir / "root.jsonl",
        [{
            "type": "session_meta",
            "timestamp": "2026-07-11T18:00:00Z",
            "payload": {"id": "root-id", "cwd": "/repo"},
        }],
    )
    write_jsonl(
        tmp_codex_dir / "later.jsonl",
        _codex_subagent_entries(
            child_id="later", root_id="root-id", agent_path="/root/later",
            timestamp="2026-07-11T18:00:03Z",
        ),
    )
    write_jsonl(
        tmp_codex_dir / "earlier.jsonl",
        _codex_subagent_entries(
            child_id="earlier", root_id="root-id", agent_path="/root/earlier",
            timestamp="2026-07-11T18:00:01Z",
        ),
    )

    provider = codex.CodexProvider()
    session = provider.get_sessions("/repo")[0]
    assert [meta.agent_id for meta, _ in provider.load_subagents(session)] == [
        "earlier", "later"
    ]


def test_delete_root_removes_native_subagent_rollouts(tmp_codex_dir) -> None:
    root_file = tmp_codex_dir / "root.jsonl"
    child_file = tmp_codex_dir / "child.jsonl"
    write_jsonl(root_file, [{
        "type": "session_meta",
        "timestamp": "2026-07-11T18:00:00Z",
        "payload": {"id": "root-id", "cwd": "/repo"},
    }])
    write_jsonl(
        child_file,
        _codex_subagent_entries(
            child_id="child-id", root_id="root-id", agent_path="/root/child",
            timestamp="2026-07-11T18:00:01Z",
        ),
    )

    provider = codex.CodexProvider()
    session = provider.get_sessions("/repo")[0]
    provider.delete_session(session)

    assert not root_file.exists()
    assert not child_file.exists()


def test_get_messages_parses_current_custom_tool_records(tmp_path: Path) -> None:
    file_path = tmp_path / "custom-tools.jsonl"
    write_jsonl(file_path, [
        {
            "type": "session_meta",
            "timestamp": "2026-07-11T18:00:00Z",
            "payload": {"id": "root-id", "cwd": "/repo"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-11T18:00:01Z",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call-1",
                "name": "exec",
                "input": "await tools.exec_command({cmd: 'pwd'})",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-07-11T18:00:02Z",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": [{"type": "input_text", "text": "/repo\n"}],
            },
        },
    ])
    session = make_session(id="root-id", provider=Provider.CODEX, source_path=str(file_path))

    messages = codex.CodexProvider().get_messages(session)

    assert [(m.content_type, m.tool_name) for m in messages] == [
        ("tool_use", "exec"), ("tool_result", "exec")
    ]
    assert "exec_command" in (messages[0].tool_input or "")
    assert "/repo" in (messages[1].tool_output or "")


def test_index_reads_only_child_header(tmp_codex_dir, monkeypatch) -> None:
    root_file = tmp_codex_dir / "root.jsonl"
    child_file = tmp_codex_dir / "child.jsonl"
    write_jsonl(root_file, [{
        "type": "session_meta", "timestamp": "2026-07-11T18:00:00Z",
        "payload": {"id": "root-id", "cwd": "/repo"},
    }])
    write_jsonl(child_file, _codex_subagent_entries(
        child_id="child-id", root_id="root-id", agent_path="/root/child",
        timestamp="2026-07-11T18:00:01Z",
    ))
    provider = codex.CodexProvider()
    original = provider._parse_session_file
    parsed: list[Path] = []

    def tracking_parse(path: Path):
        parsed.append(path)
        return original(path)

    monkeypatch.setattr(provider, "_parse_session_file", tracking_parse)
    sessions = provider.get_sessions("/repo")

    assert [s.id for s in sessions] == ["root-id"]
    assert parsed == [root_file]
