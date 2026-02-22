from __future__ import annotations

from datetime import datetime, timezone

from sesh.export import format_session_markdown
from sesh.models import Provider
from tests.helpers import make_message, make_session


def test_format_session_markdown_renders_message_types() -> None:
    """Markdown export formats text, thinking, tool call, and tool result blocks."""
    session = make_session(
        id="sess-1",
        provider=Provider.CODEX,
        project_path="/repo",
        model="gpt-4.1",
        timestamp=datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc),
    )
    messages = [
        make_message(role="user", content="hello", timestamp=None),
        make_message(role="assistant", content="hi", timestamp=None),
        make_message(
            role="assistant",
            content="",
            content_type="thinking",
            thinking="step one\nstep two",
            timestamp=None,
        ),
        make_message(
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="Read",
            tool_input='{"path":"x"}',
            timestamp=None,
        ),
        make_message(
            role="tool",
            content="",
            content_type="tool_result",
            tool_name="Read",
            tool_output="contents",
            timestamp=None,
        ),
    ]

    out = format_session_markdown(session, messages)

    assert "# Session: sess-1" in out
    assert "- **Provider:** codex" in out
    assert "## User" in out
    assert "## Assistant" in out
    assert "### Thinking" in out
    assert "> step one" in out
    assert "### Read (call)" in out
    assert "```json" in out
    assert "### Read (result)" in out
    assert "contents" in out


def test_format_session_markdown_empty_messages_still_returns_header() -> None:
    """Exporting an empty message list is valid and still includes session metadata."""
    session = make_session(id="empty", project_path="/repo")

    out = format_session_markdown(session, [])

    assert "# Session: empty" in out
    assert "- **Project:** /repo" in out
