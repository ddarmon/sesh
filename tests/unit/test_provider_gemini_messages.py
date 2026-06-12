from __future__ import annotations

from pathlib import Path

from sesh.models import Provider
from sesh.providers import gemini
from tests.helpers import make_session, write_gemini_session


def _make_session_for(file_path: Path):
    return make_session(
        id="s1",
        project_path="/repo/m",
        provider=Provider.GEMINI,
        source_path=str(file_path),
    )


def test_get_messages_fans_out_content_blocks(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-msg1.json"
    write_gemini_session(
        file_path,
        messages=[
            {
                "id": "u1",
                "timestamp": "2026-01-01T00:00:01.000Z",
                "type": "user",
                "content": [{"text": "Run a tool please"}],
            },
            {
                "id": "g1",
                "timestamp": "2026-01-01T00:00:02.000Z",
                "type": "gemini",
                "content": "I will run the tool.",
                "thoughts": [
                    {"subject": "Planning", "description": "Decide which tool to call."}
                ],
                "tokens": {"input": 10, "output": 5, "cached": 0, "thoughts": 2, "tool": 0, "total": 17},
                "model": "gemini-3-pro-preview",
                "toolCalls": [
                    {
                        "id": "read_file_1",
                        "name": "read_file",
                        "args": {"file_path": "pyproject.toml"},
                        "result": [
                            {
                                "functionResponse": {
                                    "id": "read_file_1",
                                    "name": "read_file",
                                    "response": {"output": "[project]\nname = \"sesh\""},
                                }
                            }
                        ],
                    }
                ],
            },
        ],
    )

    msgs = gemini.GeminiProvider().get_messages(_make_session_for(file_path))
    types = [(m.role, m.content_type) for m in msgs]
    assert types == [
        ("user", "text"),
        ("assistant", "thinking"),
        ("assistant", "text"),
        ("assistant", "tool_use"),
        ("tool", "tool_result"),
    ]

    thinking = msgs[1]
    assert "Planning" in thinking.thinking
    assert "Decide which tool" in thinking.thinking

    tool_use = msgs[3]
    assert tool_use.tool_name == "read_file"
    assert '"file_path"' in tool_use.tool_input

    tool_result = msgs[4]
    assert tool_result.tool_name == "read_file"
    assert "[project]" in tool_result.tool_output


def test_get_messages_flags_slash_commands_as_system(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-sys1.json"
    write_gemini_session(
        file_path,
        messages=[
            {"id": "u1", "timestamp": "2026-01-01T00:00:01.000Z", "type": "user", "content": [{"text": "/model"}]},
            {"id": "u2", "timestamp": "2026-01-01T00:00:02.000Z", "type": "user", "content": [{"text": "real prompt"}]},
            # A bare absolute path is NOT a command
            {"id": "u3", "timestamp": "2026-01-01T00:00:03.000Z", "type": "user", "content": [{"text": "/Users/me/file.py is broken"}]},
        ],
    )

    msgs = gemini.GeminiProvider().get_messages(_make_session_for(file_path))
    assert [m.is_system for m in msgs] == [True, False, False]


def test_get_messages_error_and_info_are_system(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-err1.json"
    write_gemini_session(
        file_path,
        messages=[
            {"id": "u1", "timestamp": "2026-01-01T00:00:01.000Z", "type": "user", "content": [{"text": "hi"}]},
            {"id": "e1", "timestamp": "2026-01-01T00:00:02.000Z", "type": "error", "content": "[API Error: Requested entity was not found.]"},
            {"id": "i1", "timestamp": "2026-01-01T00:00:03.000Z", "type": "info", "content": "Loop detection has been disabled for this session."},
        ],
    )

    msgs = gemini.GeminiProvider().get_messages(_make_session_for(file_path))
    assert [(m.role, m.is_system) for m in msgs] == [
        ("user", False),
        ("system", True),
        ("system", True),
    ]


def test_get_messages_handles_string_user_content(tmp_path: Path) -> None:
    """Older session files store user content as a plain string."""
    file_path = tmp_path / "session-2026-01-01T00-00-str1.json"
    write_gemini_session(
        file_path,
        messages=[
            {"id": "u1", "timestamp": "2026-01-01T00:00:01.000Z", "type": "user", "content": "plain string prompt"},
        ],
    )
    msgs = gemini.GeminiProvider().get_messages(_make_session_for(file_path))
    assert len(msgs) == 1
    assert msgs[0].content == "plain string prompt"


def test_get_messages_missing_file_returns_empty(tmp_path: Path) -> None:
    session = _make_session_for(tmp_path / "nope.json")
    assert gemini.GeminiProvider().get_messages(session) == []


def test_get_messages_corrupt_file_returns_empty(tmp_path: Path) -> None:
    file_path = tmp_path / "session-2026-01-01T00-00-bad1.json"
    file_path.write_text("{broken")
    assert gemini.GeminiProvider().get_messages(_make_session_for(file_path)) == []


def test_flatten_tool_result_handles_error_and_odd_shapes() -> None:
    assert gemini._flatten_tool_result(None) == ""
    assert gemini._flatten_tool_result("plain") == "plain"
    assert (
        gemini._flatten_tool_result(
            [{"functionResponse": {"response": {"error": "boom"}}}]
        )
        == "boom"
    )
    out = gemini._flatten_tool_result(
        [{"functionResponse": {"response": {"weird": 1}}}]
    )
    assert "weird" in out
