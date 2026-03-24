"""Tests for the Copilot provider: discovery, sessions, messages, and deletion."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sesh.models import Provider
from sesh.providers.copilot import CopilotProvider, _parse_workspace_yaml
from tests.helpers import make_session, write_copilot_events, write_workspace_yaml


# --- YAML parsing ---


def test_parse_workspace_yaml_basic(tmp_path: Path) -> None:
    yaml_path = tmp_path / "workspace.yaml"
    write_workspace_yaml(yaml_path, {
        "id": "abc-123",
        "cwd": "/Users/me/repo",
        "summary": "Test Session",
        "created_at": "2026-03-18T00:20:04.675Z",
        "updated_at": "2026-03-18T00:20:33.232Z",
    })
    meta = _parse_workspace_yaml(yaml_path)
    assert meta["id"] == "abc-123"
    assert meta["cwd"] == "/Users/me/repo"
    assert meta["summary"] == "Test Session"
    assert meta["created_at"] == "2026-03-18T00:20:04.675Z"


def test_parse_workspace_yaml_optional_keys(tmp_path: Path) -> None:
    yaml_path = tmp_path / "workspace.yaml"
    write_workspace_yaml(yaml_path, {
        "id": "abc-123",
        "cwd": "/Users/me/repo",
        "git_root": "/Users/me/repo",
        "repository": "me/repo",
        "host_type": "github",
        "branch": "main",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    })
    meta = _parse_workspace_yaml(yaml_path)
    assert meta["git_root"] == "/Users/me/repo"
    assert meta["repository"] == "me/repo"
    assert meta["branch"] == "main"


def test_parse_workspace_yaml_missing_file(tmp_path: Path) -> None:
    meta = _parse_workspace_yaml(tmp_path / "nonexistent.yaml")
    assert meta == {}


def test_parse_workspace_yaml_quoted_values(tmp_path: Path) -> None:
    yaml_path = tmp_path / "workspace.yaml"
    yaml_path.write_text('id: "abc-123"\ncwd: \'/Users/me/repo\'\n')
    meta = _parse_workspace_yaml(yaml_path)
    assert meta["id"] == "abc-123"
    assert meta["cwd"] == "/Users/me/repo"


# --- Discovery ---


def test_discover_projects(tmp_copilot_dir: Path) -> None:
    s1 = tmp_copilot_dir / "session-1"
    s2 = tmp_copilot_dir / "session-2"
    write_workspace_yaml(s1 / "workspace.yaml", {
        "id": "session-1", "cwd": "/repo/a",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    write_workspace_yaml(s2 / "workspace.yaml", {
        "id": "session-2", "cwd": "/repo/b",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })

    provider = CopilotProvider()
    projects = list(provider.discover_projects())
    paths = [p for p, _ in projects]
    assert "/repo/a" in paths
    assert "/repo/b" in paths


def test_discover_projects_deduplicates_cwd(tmp_copilot_dir: Path) -> None:
    """Two sessions with the same cwd yield one project."""
    for name in ("s1", "s2"):
        write_workspace_yaml(tmp_copilot_dir / name / "workspace.yaml", {
            "id": name, "cwd": "/same/repo",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        })

    provider = CopilotProvider()
    projects = list(provider.discover_projects())
    assert len(projects) == 1
    assert projects[0][0] == "/same/repo"


def test_discover_projects_missing_dir(tmp_copilot_dir: Path) -> None:
    """No crash when COPILOT_DIR does not exist."""
    provider = CopilotProvider()
    projects = list(provider.discover_projects())
    assert projects == []


# --- Sessions ---


def test_get_sessions(tmp_copilot_dir: Path) -> None:
    s_dir = tmp_copilot_dir / "abc-123"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "abc-123",
        "cwd": "/repo",
        "summary": "My Session",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    })
    write_copilot_events(s_dir / "events.jsonl", [
        {"type": "user.message", "data": {"content": "hello"}, "timestamp": "2026-01-01T00:00:01Z"},
        {"type": "assistant.message", "data": {"content": "hi"}, "timestamp": "2026-01-01T00:00:02Z"},
        {"type": "session.model_change", "data": {"newModel": "gpt-5"}, "timestamp": "2026-01-01T00:00:03Z"},
    ])

    provider = CopilotProvider()
    sessions = provider.get_sessions("/repo")
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == "abc-123"
    assert s.provider == Provider.COPILOT
    assert s.summary == "My Session"
    assert s.message_count == 2
    assert s.model == "gpt-5"
    assert s.source_path == str(s_dir)


def test_get_sessions_summary_fallback(tmp_copilot_dir: Path) -> None:
    """When workspace.yaml has no summary, first user message is used."""
    s_dir = tmp_copilot_dir / "abc-456"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "abc-456",
        "cwd": "/repo",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    })
    write_copilot_events(s_dir / "events.jsonl", [
        {"type": "user.message", "data": {"content": "What is the meaning?"}, "timestamp": "2026-01-01T00:00:01Z"},
    ])

    provider = CopilotProvider()
    sessions = provider.get_sessions("/repo")
    assert sessions[0].summary == "What is the meaning?"


# --- Messages ---


def _make_event(etype, data, ts="2026-01-01T00:00:00Z"):
    return {"type": etype, "data": data, "id": "evt-1", "timestamp": ts, "parentId": None}


def test_get_messages_user_and_assistant(tmp_copilot_dir: Path) -> None:
    s_dir = tmp_copilot_dir / "s1"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "s1", "cwd": "/repo",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    write_copilot_events(s_dir / "events.jsonl", [
        _make_event("user.message", {"content": "hello"}),
        _make_event("assistant.message", {"content": "hi", "toolRequests": []}),
    ])

    provider = CopilotProvider()
    session = make_session(id="s1", provider=Provider.COPILOT, source_path=str(s_dir))
    msgs = provider.get_messages(session)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello"
    assert msgs[1].role == "assistant"
    assert msgs[1].content == "hi"


def test_get_messages_thinking(tmp_copilot_dir: Path) -> None:
    s_dir = tmp_copilot_dir / "s1"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "s1", "cwd": "/repo",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    write_copilot_events(s_dir / "events.jsonl", [
        _make_event("assistant.message", {
            "content": "answer",
            "toolRequests": [],
            "reasoningText": "let me think",
        }),
    ])

    provider = CopilotProvider()
    session = make_session(id="s1", provider=Provider.COPILOT, source_path=str(s_dir))
    msgs = provider.get_messages(session)
    # Should produce thinking + text messages
    assert len(msgs) == 2
    assert msgs[0].content_type == "thinking"
    assert msgs[0].thinking == "let me think"
    assert msgs[1].content_type == "text"
    assert msgs[1].content == "answer"


def test_get_messages_tool_requests(tmp_copilot_dir: Path) -> None:
    s_dir = tmp_copilot_dir / "s1"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "s1", "cwd": "/repo",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    write_copilot_events(s_dir / "events.jsonl", [
        _make_event("assistant.message", {
            "content": "",
            "toolRequests": [
                {"toolCallId": "call-1", "name": "view", "arguments": {"path": "/foo.py"}},
            ],
        }),
        _make_event("tool.execution_complete", {
            "toolCallId": "call-1",
            "success": True,
            "result": {"content": "file contents here"},
        }),
    ])

    provider = CopilotProvider()
    session = make_session(id="s1", provider=Provider.COPILOT, source_path=str(s_dir))
    msgs = provider.get_messages(session)

    tool_use = [m for m in msgs if m.content_type == "tool_use"]
    assert len(tool_use) == 1
    assert tool_use[0].tool_name == "view"
    assert '"path"' in tool_use[0].tool_input

    tool_result = [m for m in msgs if m.content_type == "tool_result"]
    assert len(tool_result) == 1
    assert tool_result[0].role == "tool"
    assert tool_result[0].tool_name == "view"
    assert tool_result[0].tool_output == "file contents here"


def test_get_messages_system_events_skipped(tmp_copilot_dir: Path) -> None:
    s_dir = tmp_copilot_dir / "s1"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "s1", "cwd": "/repo",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    write_copilot_events(s_dir / "events.jsonl", [
        _make_event("session.start", {"sessionId": "s1", "context": {"cwd": "/repo"}}),
        _make_event("session.info", {"infoType": "folder_trust", "message": "trusted"}),
        _make_event("session.model_change", {"newModel": "gpt-5"}),
        _make_event("assistant.turn_start", {"turnId": "0"}),
        _make_event("user.message", {"content": "hello"}),
        _make_event("assistant.turn_end", {"turnId": "0"}),
        _make_event("session.shutdown", {"shutdownType": "routine"}),
    ])

    provider = CopilotProvider()
    session = make_session(id="s1", provider=Provider.COPILOT, source_path=str(s_dir))
    msgs = provider.get_messages(session)
    assert len(msgs) == 1
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello"


def test_get_messages_accepts_events_jsonl_path(tmp_copilot_dir: Path) -> None:
    """get_messages works when source_path points to events.jsonl directly."""
    s_dir = tmp_copilot_dir / "s1"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "s1", "cwd": "/repo",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    events_file = s_dir / "events.jsonl"
    write_copilot_events(events_file, [
        _make_event("user.message", {"content": "hello"}),
    ])

    provider = CopilotProvider()
    session = make_session(id="s1", provider=Provider.COPILOT, source_path=str(events_file))
    msgs = provider.get_messages(session)
    assert len(msgs) == 1
    assert msgs[0].content == "hello"


# --- Token usage ---


def test_get_sessions_extracts_token_usage(tmp_copilot_dir: Path) -> None:
    """Token usage is summed across all models in session.shutdown modelMetrics."""
    s_dir = tmp_copilot_dir / "tok-1"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "tok-1",
        "cwd": "/repo",
        "summary": "Token test",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    })
    write_copilot_events(s_dir / "events.jsonl", [
        {"type": "user.message", "data": {"content": "hi"}, "timestamp": "2026-01-01T00:00:01Z"},
        {"type": "assistant.message", "data": {"content": "hello"}, "timestamp": "2026-01-01T00:00:02Z"},
        {
            "type": "session.shutdown",
            "data": {
                "currentModel": "gpt-5",
                "modelMetrics": {
                    "gpt-5": {
                        "requests": {"count": 5, "cost": 1},
                        "usage": {
                            "inputTokens": 10000,
                            "outputTokens": 2000,
                            "cacheReadTokens": 5000,
                            "cacheWriteTokens": 1000,
                        },
                    },
                    "gpt-4.1": {
                        "requests": {"count": 2, "cost": 0},
                        "usage": {
                            "inputTokens": 3000,
                            "outputTokens": 500,
                            "cacheReadTokens": 1000,
                            "cacheWriteTokens": 0,
                        },
                    },
                },
            },
            "timestamp": "2026-01-02T00:00:00Z",
        },
    ])

    provider = CopilotProvider()
    sessions = provider.get_sessions("/repo")
    assert len(sessions) == 1
    s = sessions[0]
    # gpt-5: 10000+5000+1000=16000 input, 2000 output
    # gpt-4.1: 3000+1000+0=4000 input, 500 output
    assert s.input_tokens == 20000
    assert s.output_tokens == 2500
    # Copilot only has cumulative totals
    assert s.cumulative_input_tokens == 20000


def test_get_sessions_no_shutdown_returns_none(tmp_copilot_dir: Path) -> None:
    """Sessions without a shutdown event have None token fields."""
    s_dir = tmp_copilot_dir / "no-tok"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "no-tok",
        "cwd": "/repo",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    })
    write_copilot_events(s_dir / "events.jsonl", [
        {"type": "user.message", "data": {"content": "hi"}, "timestamp": "2026-01-01T00:00:01Z"},
    ])

    provider = CopilotProvider()
    sessions = provider.get_sessions("/repo")
    assert len(sessions) == 1
    assert sessions[0].input_tokens is None
    assert sessions[0].output_tokens is None


# --- Deletion ---


def test_delete_session(tmp_copilot_dir: Path) -> None:
    s_dir = tmp_copilot_dir / "s1"
    write_workspace_yaml(s_dir / "workspace.yaml", {
        "id": "s1", "cwd": "/repo",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    write_copilot_events(s_dir / "events.jsonl", [])

    provider = CopilotProvider()
    session = make_session(id="s1", provider=Provider.COPILOT, source_path=str(s_dir))
    provider.delete_session(session)
    assert not s_dir.exists()
