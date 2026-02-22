"""Fixture-based tests for provider message parsing.

Each test creates inline fixture data matching the real session format and
verifies that the provider produces Message objects with the correct
content_type and populated fields.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sesh.models import Message, Provider, SessionMeta


# ---------------------------------------------------------------------------
# Claude provider
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture()
def claude_session_dir(tmp_path: Path):
    """Create a temp directory with a Claude-style JSONL file."""
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True)

    session_id = "test-session-123"
    ts = "2025-01-15T10:00:00Z"

    entries = [
        # Assistant message with text + thinking + tool_use
        {
            "sessionId": session_id,
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me think about this..."},
                    {"type": "text", "text": "I'll help with that."},
                    {
                        "type": "tool_use",
                        "id": "tool_123",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    },
                ],
            },
        },
        # User message with tool_result
        {
            "sessionId": session_id,
            "timestamp": ts,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_123",
                        "content": "file contents here",
                    },
                    {"type": "text", "text": "Now fix the bug."},
                ],
            },
        },
    ]

    _write_jsonl(project_dir / "session.jsonl", entries)

    return project_dir, session_id


def test_claude_splits_content_blocks(claude_session_dir):
    from sesh.providers.claude import ClaudeProvider

    project_dir, session_id = claude_session_dir
    session = SessionMeta(
        id=session_id,
        project_path="/test",
        provider=Provider.CLAUDE,
        summary="test",
        timestamp=datetime.now(tz=timezone.utc),
        source_path=str(project_dir),
    )

    provider = ClaudeProvider()
    messages = provider.get_messages(session)

    # Should produce: thinking, text, tool_use, tool_result, text
    types = [m.content_type for m in messages]
    assert types == ["thinking", "text", "tool_use", "tool_result", "text"]

    # Check thinking message
    thinking = messages[0]
    assert thinking.role == "assistant"
    assert thinking.thinking == "Let me think about this..."
    assert thinking.content == ""

    # Check text message
    text_msg = messages[1]
    assert text_msg.role == "assistant"
    assert text_msg.content == "I'll help with that."

    # Check tool_use message
    tool_use = messages[2]
    assert tool_use.role == "assistant"
    assert tool_use.tool_name == "Read"
    assert tool_use.tool_input is not None
    assert "/tmp/test.py" in tool_use.tool_input
    assert tool_use.content == ""

    # Check tool_result message — name resolved from tool_use_id
    tool_result = messages[3]
    assert tool_result.role == "tool"
    assert tool_result.tool_name == "Read"
    assert tool_result.tool_output == "file contents here"
    assert tool_result.content == ""

    # Check trailing user text
    user_text = messages[4]
    assert user_text.role == "user"
    assert user_text.content == "Now fix the bug."


def test_claude_tool_result_list_content(tmp_path: Path):
    """tool_result with content as list of text parts."""
    from sesh.providers.claude import ClaudeProvider

    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True)

    session_id = "tr-list-test"
    entries = [
        {
            "sessionId": session_id,
            "timestamp": "2025-01-15T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
        },
        {
            "sessionId": session_id,
            "timestamp": "2025-01-15T10:00:01Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [
                            {"type": "text", "text": "line1"},
                            {"type": "text", "text": "line2"},
                        ],
                    },
                ],
            },
        },
    ]

    _write_jsonl(project_dir / "session.jsonl", entries)

    session = SessionMeta(
        id=session_id,
        project_path="/test",
        provider=Provider.CLAUDE,
        summary="test",
        timestamp=datetime.now(tz=timezone.utc),
        source_path=str(project_dir),
    )

    messages = ClaudeProvider().get_messages(session)
    tool_result = [m for m in messages if m.content_type == "tool_result"][0]
    assert tool_result.tool_name == "Bash"
    assert tool_result.tool_output == "line1\nline2"


# ---------------------------------------------------------------------------
# Codex provider
# ---------------------------------------------------------------------------


@pytest.fixture()
def codex_session_file(tmp_path: Path):
    """Create a temp Codex JSONL session file."""
    session_id = "codex-sess-456"
    ts = "2025-02-01T12:00:00Z"

    entries = [
        {
            "type": "session_meta",
            "timestamp": ts,
            "payload": {"id": session_id, "cwd": "/test/project", "model": "gpt-4"},
        },
        {
            "type": "event_msg",
            "timestamp": ts,
            "payload": {"type": "user_message", "message": "Fix the tests"},
        },
        {
            "type": "event_msg",
            "timestamp": ts,
            "payload": {"type": "agent_reasoning", "text": "I should look at the test files first."},
        },
        {
            "type": "response_item",
            "timestamp": ts,
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_abc",
                "arguments": '{"cmd": "pytest"}',
            },
        },
        {
            "type": "response_item",
            "timestamp": ts,
            "payload": {
                "type": "function_call_output",
                "call_id": "call_abc",
                "output": "3 tests passed",
            },
        },
        {
            "type": "response_item",
            "timestamp": ts,
            "payload": {
                "role": "assistant",
                "content": [{"text": "All tests pass now."}],
            },
        },
    ]

    file_path = tmp_path / "session.jsonl"
    _write_jsonl(file_path, entries)
    return file_path, session_id


def test_codex_extracts_all_types(codex_session_file):
    from sesh.providers.codex import CodexProvider

    file_path, session_id = codex_session_file
    session = SessionMeta(
        id=session_id,
        project_path="/test/project",
        provider=Provider.CODEX,
        summary="test",
        timestamp=datetime.now(tz=timezone.utc),
        source_path=str(file_path),
    )

    provider = CodexProvider()
    messages = provider.get_messages(session)

    types = [m.content_type for m in messages]
    assert types == ["text", "thinking", "tool_use", "tool_result", "text"]

    # User message
    assert messages[0].role == "user"
    assert messages[0].content == "Fix the tests"

    # Reasoning/thinking
    assert messages[1].role == "assistant"
    assert messages[1].thinking == "I should look at the test files first."

    # Function call
    assert messages[2].role == "assistant"
    assert messages[2].tool_name == "shell"
    assert messages[2].tool_input == '{"cmd": "pytest"}'

    # Function call output — name resolved from call_id
    assert messages[3].role == "tool"
    assert messages[3].tool_name == "shell"
    assert messages[3].tool_output == "3 tests passed"

    # Assistant text
    assert messages[4].role == "assistant"
    assert messages[4].content == "All tests pass now."


# ---------------------------------------------------------------------------
# Cursor provider
# ---------------------------------------------------------------------------


def _create_store_db(db_path: Path, blobs: list[dict]) -> None:
    """Create a minimal store.db with the given blob dicts."""
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE blobs (id TEXT, data BLOB)")
    conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    for i, blob in enumerate(blobs):
        conn.execute(
            "INSERT INTO blobs (id, data) VALUES (?, ?)",
            (str(i), json.dumps(blob).encode("utf-8")),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def cursor_store_db(tmp_path: Path):
    """Create a temp Cursor store.db with mixed content blocks."""
    db_path = tmp_path / "store.db"

    blobs = [
        {
            "role": "user",
            "content": "What does this function do?",
        },
        {
            "role": "assistant",
            "content": [
                {"type": "reasoning", "text": "Let me analyze the code..."},
                {"type": "text", "text": "This function sorts an array."},
                {
                    "type": "tool-call",
                    "toolName": "codebase_search",
                    "args": {"query": "sort function"},
                },
                {
                    "type": "tool-result",
                    "toolName": "codebase_search",
                    "result": "Found 3 matches in sort.py",
                },
            ],
        },
    ]

    _create_store_db(db_path, blobs)
    return db_path


def test_cursor_splits_content_blocks(cursor_store_db):
    from sesh.providers.cursor import CursorProvider

    session = SessionMeta(
        id="cursor-test",
        project_path="/test",
        provider=Provider.CURSOR,
        summary="test",
        timestamp=datetime.now(tz=timezone.utc),
        source_path=str(cursor_store_db),
    )

    provider = CursorProvider()
    messages = provider.get_messages(session)

    types = [m.content_type for m in messages]
    assert types == ["text", "thinking", "text", "tool_use", "tool_result"]

    # User text
    assert messages[0].role == "user"
    assert messages[0].content == "What does this function do?"

    # Reasoning
    assert messages[1].role == "assistant"
    assert messages[1].thinking == "Let me analyze the code..."
    assert messages[1].content == ""

    # Assistant text
    assert messages[2].role == "assistant"
    assert messages[2].content == "This function sorts an array."

    # Tool call
    assert messages[3].role == "assistant"
    assert messages[3].tool_name == "codebase_search"
    assert messages[3].tool_input is not None
    assert "sort function" in messages[3].tool_input

    # Tool result
    assert messages[4].role == "tool"
    assert messages[4].tool_name == "codebase_search"
    assert messages[4].tool_output == "Found 3 matches in sort.py"


# ---------------------------------------------------------------------------
# filter_messages
# ---------------------------------------------------------------------------


def test_filter_messages_defaults():
    """Default filter hides system, tool, and thinking messages."""
    from sesh.models import filter_messages

    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="", content_type="thinking", thinking="hmm"),
        Message(role="assistant", content="hi"),
        Message(role="assistant", content="", content_type="tool_use", tool_name="Read"),
        Message(role="tool", content="", content_type="tool_result", tool_output="data"),
        Message(role="user", content="sys", is_system=True),
    ]

    filtered = filter_messages(messages)
    assert len(filtered) == 2
    assert filtered[0].content == "hello"
    assert filtered[1].content == "hi"


def test_filter_messages_include_all():
    from sesh.models import filter_messages

    messages = [
        Message(role="user", content="hello"),
        Message(role="assistant", content="", content_type="thinking", thinking="hmm"),
        Message(role="assistant", content="", content_type="tool_use", tool_name="Read"),
        Message(role="tool", content="", content_type="tool_result", tool_output="data"),
        Message(role="user", content="sys", is_system=True),
    ]

    filtered = filter_messages(
        messages, include_system=True, include_tools=True, include_thinking=True
    )
    assert len(filtered) == 5


# ---------------------------------------------------------------------------
# regressions
# ---------------------------------------------------------------------------


def test_search_extract_prefers_query_matching_candidate():
    from sesh.search import _extract_content_text

    entry = {
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "This is a longer text block without the target token, used to "
                        "ensure longest-candidate selection would pick the wrong block."
                    ),
                },
                {
                    "type": "tool_use",
                    "input": {"cmd": "echo needletoken"},
                },
            ]
        }
    }

    extracted = _extract_content_text(entry, "needletoken")
    assert "needletoken" in extracted.lower()


def test_codex_stringifies_structured_tool_io(tmp_path: Path):
    from sesh.providers.codex import CodexProvider

    ts = "2025-02-01T12:00:00Z"
    file_path = tmp_path / "structured-codex.jsonl"
    _write_jsonl(
        file_path,
        [
            {
                "type": "response_item",
                "timestamp": ts,
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "call_1",
                    "arguments": {"cmd": "pytest", "cwd": "/tmp/demo"},
                },
            },
            {
                "type": "response_item",
                "timestamp": ts,
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": {"status": "ok", "lines": ["a", "b"]},
                },
            },
        ],
    )

    session = SessionMeta(
        id="codex-structured",
        project_path="/test/project",
        provider=Provider.CODEX,
        summary="test",
        timestamp=datetime.now(tz=timezone.utc),
        source_path=str(file_path),
    )

    messages = CodexProvider().get_messages(session)
    tool_use = next(m for m in messages if m.content_type == "tool_use")
    tool_result = next(m for m in messages if m.content_type == "tool_result")

    assert isinstance(tool_use.tool_input, str)
    assert '"cmd"' in (tool_use.tool_input or "")
    assert isinstance(tool_result.tool_output, str)
    assert '"status"' in (tool_result.tool_output or "")


def test_cursor_stringifies_structured_tool_result(tmp_path: Path):
    from sesh.providers.cursor import CursorProvider

    db_path = tmp_path / "store.db"
    _create_store_db(
        db_path,
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool-call",
                        "toolName": "search",
                        "args": ["one", "two"],
                    },
                    {
                        "type": "tool-result",
                        "toolName": "search",
                        "result": {"hits": 3, "items": ["a", "b", "c"]},
                    },
                ],
            },
        ],
    )

    session = SessionMeta(
        id="cursor-structured",
        project_path="/test",
        provider=Provider.CURSOR,
        summary="test",
        timestamp=datetime.now(tz=timezone.utc),
        source_path=str(db_path),
    )

    messages = CursorProvider().get_messages(session)
    tool_use = next(m for m in messages if m.content_type == "tool_use")
    tool_result = next(m for m in messages if m.content_type == "tool_result")

    assert isinstance(tool_use.tool_input, str)
    assert "[" in (tool_use.tool_input or "")
    assert isinstance(tool_result.tool_output, str)
    assert '"hits"' in (tool_result.tool_output or "")
