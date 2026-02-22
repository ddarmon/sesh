from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider
from sesh.providers import codex
from tests.helpers import make_session, write_jsonl


def test_parse_timestamp_z() -> None:
    assert codex._parse_timestamp("2025-02-01T12:00:00Z") == datetime(
        2025, 2, 1, 12, 0, 0, tzinfo=timezone.utc
    )


def test_extract_text_from_content() -> None:
    content = [
        {"text": "one"},
        {"input_text": "two"},
        {"output_text": "three"},
        {"x": "ignored"},
    ]
    assert codex._extract_text_from_content(content) == "one\ntwo\nthree"


def test_stringify_tool_value() -> None:
    assert codex._stringify_tool_value(None) == ""
    rendered = codex._stringify_tool_value({"a": 1})
    assert isinstance(rendered, str)
    assert '"a"' in rendered


def test_parse_session_file_new_format(tmp_path: Path) -> None:
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
    assert data["message_count"] == 2
    assert data["summary"] == first_prompt[:80] + "..."
    assert data["file_path"] == str(file_path)


def test_parse_session_file_legacy_format(tmp_path: Path) -> None:
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
    assert data["message_count"] == 1
    assert data["summary"] == "Hello world"


def test_parse_session_file_legacy_without_cwd_returns_none(tmp_path: Path) -> None:
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


def test_build_index_uses_cached_sessions(tmp_codex_dir) -> None:
    file_path = tmp_codex_dir / "cached.jsonl"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("{}\n")
    cached_session = make_session(
        id="cached",
        project_path="/cached-repo",
        provider=Provider.CODEX,
        source_path=str(file_path),
        timestamp=datetime(2025, 2, 1, tzinfo=timezone.utc),
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


def test_delete_session_unlinks_source_file(tmp_path: Path) -> None:
    file_path = tmp_path / "session.jsonl"
    file_path.write_text("{}\n")
    session = make_session(id="s1", provider=Provider.CODEX, source_path=str(file_path))
    codex.CodexProvider().delete_session(session)
    assert not file_path.exists()
