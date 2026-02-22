from __future__ import annotations

from sesh.models import (
    encode_claude_path,
    encode_cursor_path,
    encode_project_path,
    filter_messages,
    workspace_uri,
)
from tests.helpers import make_message


def test_encode_project_path() -> None:
    assert encode_project_path("/Users/me/project") == "Users-me-project"


def test_encode_project_path_no_leading_slash() -> None:
    assert encode_project_path("foo/bar") == "foo-bar"


def test_encode_claude_path() -> None:
    assert encode_claude_path("/Users/me/My Project") == "-Users-me-My-Project"


def test_encode_claude_path_spaces() -> None:
    assert encode_claude_path("/tmp/has spaces") == "-tmp-has-spaces"


def test_encode_cursor_path() -> None:
    assert encode_cursor_path("/Users/me/My Project") == "Users-me-My-Project"


def test_encode_cursor_path_spaces() -> None:
    assert encode_cursor_path("/tmp/has spaces") == "tmp-has-spaces"


def test_workspace_uri() -> None:
    assert workspace_uri("/Users/me/project") == "file:///Users/me/project"


def test_filter_defaults() -> None:
    messages = [
        make_message(role="user", content="hello"),
        make_message(content_type="thinking", thinking="hmm", content="", role="assistant"),
        make_message(content_type="tool_use", tool_name="Read", content="", role="assistant"),
        make_message(content_type="tool_result", tool_output="x", content="", role="tool"),
        make_message(is_system=True, content="sys"),
        make_message(role="assistant", content="visible"),
    ]

    filtered = filter_messages(messages)
    assert [m.content for m in filtered] == ["hello", "visible"]


def test_filter_tools_only() -> None:
    messages = [
        make_message(content="text"),
        make_message(content_type="thinking", thinking="hmm", content=""),
        make_message(content_type="tool_use", tool_name="Read", content=""),
        make_message(content_type="tool_result", tool_output="ok", content="", role="tool"),
    ]

    filtered = filter_messages(messages, include_tools=True)
    assert [m.content_type for m in filtered] == ["text", "tool_use", "tool_result"]


def test_filter_thinking_only() -> None:
    messages = [
        make_message(content="text"),
        make_message(content_type="thinking", thinking="hmm", content="", role="assistant"),
        make_message(content_type="tool_use", tool_name="Read", content=""),
    ]

    filtered = filter_messages(messages, include_thinking=True)
    assert [m.content_type for m in filtered] == ["text", "thinking"]


def test_filter_system_only() -> None:
    messages = [
        make_message(content="text"),
        make_message(is_system=True, content="sys"),
    ]

    filtered = filter_messages(messages, include_system=True)
    assert [m.content for m in filtered] == ["text", "sys"]


def test_filter_empty_list() -> None:
    assert filter_messages([]) == []


def test_filter_all_hidden() -> None:
    messages = [
        make_message(is_system=True, content="sys"),
        make_message(content_type="tool_use", tool_name="Read", content=""),
        make_message(content_type="thinking", thinking="...", content="", role="assistant"),
    ]

    assert filter_messages(messages) == []
