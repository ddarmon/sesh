from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sesh.providers import cline
from tests.helpers import make_session, write_cline_session


def _load(tmp_cline_dir: Path, messages: list[dict], **kwargs):
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        messages=messages, **kwargs,
    )
    (session,) = cline.ClineProvider().get_sessions("/p")
    return cline.ClineProvider().get_messages(session)


def test_strip_user_input_wrapper() -> None:
    assert cline.strip_user_input_wrapper('<user_input mode="act">Hi!</user_input>') == "Hi!"
    assert cline.strip_user_input_wrapper("<user_input>Hi!</user_input>") == "Hi!"
    # Multi-line bodies survive intact.
    assert cline.strip_user_input_wrapper(
        '<user_input mode="plan">line 1\nline 2</user_input>'
    ) == "line 1\nline 2"
    # Unwrapped text passes through.
    assert cline.strip_user_input_wrapper("plain") == "plain"


def test_user_text_is_unwrapped(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [{
        "id": "m1", "role": "user", "ts": 1788435478090,
        "content": [{"type": "text", "text": '<user_input mode="act">Hi!</user_input>'}],
    }])
    assert [(m.role, m.content, m.is_system) for m in messages] == [("user", "Hi!", False)]
    assert messages[0].timestamp == datetime(
        2026, 9, 3, 11, 37, 58, 90000, tzinfo=timezone.utc
    )


def test_task_resumption_is_marked_system(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [{
        "id": "m1", "role": "user", "ts": 1,
        "content": [{
            "type": "text",
            "text": '<user_input mode="act">[TASK RESUMPTION] Please continue '
                    "where you left off.</user_input>",
        }],
    }])
    assert messages[0].is_system is True
    assert messages[0].content.startswith("[TASK RESUMPTION]")


def test_thinking_and_redacted_thinking(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [{
        "id": "m1", "role": "assistant", "ts": 1,
        "content": [
            {"type": "thinking", "thinking": "Let me check.", "signature": "sig"},
            {"type": "redacted_thinking", "data": "opaque"},
            {"type": "text", "text": "Done."},
        ],
    }])
    assert [(m.content_type, m.thinking) for m in messages] == [
        ("thinking", "Let me check."),
        ("thinking", "[redacted thinking]"),
        ("text", None),
    ]
    assert all(m.role == "assistant" for m in messages)


def test_empty_thinking_block_is_dropped(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [{
        "id": "m1", "role": "assistant", "ts": 1,
        "content": [
            {"type": "thinking", "thinking": "   "},
            {"type": "text", "text": "Done."},
        ],
    }])
    assert [m.content_type for m in messages] == ["text"]


def test_tool_use_and_tool_result(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [
        {
            "id": "m1", "role": "assistant", "ts": 1,
            "content": [{
                "type": "tool_use", "id": "call_1", "name": "run_commands",
                "input": {"commands": ["uname -a"]},
            }],
        },
        {
            "id": "m2", "role": "user", "ts": 2,
            "content": [{
                "type": "tool_result", "tool_use_id": "call_1",
                "name": "run_commands",
                # Arbitrary JSON, not a string.
                "content": [{"query": "uname -a", "result": "Darwin", "success": True}],
            }],
        },
    ])

    use, result = messages
    assert use.content_type == "tool_use"
    assert use.role == "assistant"
    assert use.tool_name == "run_commands"
    assert json.loads(use.tool_input) == {"commands": ["uname -a"]}

    assert result.content_type == "tool_result"
    assert result.role == "tool"
    assert result.tool_name == "run_commands"
    assert json.loads(result.tool_output) == [
        {"query": "uname -a", "result": "Darwin", "success": True}
    ]


def test_string_tool_result_is_passed_through(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [{
        "id": "m1", "role": "user", "ts": 1,
        "content": [{
            "type": "tool_result", "tool_use_id": "c1", "name": "read_file",
            "content": "file contents",
        }],
    }])
    assert messages[0].tool_output == "file contents"


def test_error_tool_result_is_prefixed(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [{
        "id": "m1", "role": "user", "ts": 1,
        "content": [{
            "type": "tool_result", "tool_use_id": "c1", "name": "read_file",
            "content": "no such file", "is_error": True,
        }],
    }])
    assert messages[0].tool_output == "[error] no such file"


def test_image_media_and_file_blocks_render_as_text(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [{
        "id": "m1", "role": "user", "ts": 1,
        "content": [
            {"type": "image", "data": "base64...", "mediaType": "image/png"},
            {"type": "media", "media": {"kind": "audio"}},
            {"type": "file", "path": "/p/notes.md", "content": "# Notes"},
        ],
    }])
    assert [(m.content_type, m.content) for m in messages] == [
        ("text", "[image: image/png]"),
        ("text", "[media]"),
        ("text", "[file: /p/notes.md]\n# Notes"),
    ]


def test_string_content_is_treated_as_one_text_block(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [
        {"id": "m1", "role": "assistant", "ts": 1, "content": "plain string"},
    ])
    assert [(m.role, m.content) for m in messages] == [("assistant", "plain string")]


def test_malformed_entries_are_skipped(tmp_cline_dir: Path) -> None:
    messages = _load(tmp_cline_dir, [
        "not a dict",
        {"id": "m1", "ts": 1, "content": [{"type": "text", "text": "no role"}]},
        {"id": "m2", "role": "user", "ts": 2, "content": "kept"},
        {"id": "m3", "role": "user", "ts": 3, "content": ["not a dict block", 7]},
        {"id": "m4", "role": "user", "ts": 4, "content": [{"type": "unknown"}]},
    ])
    assert [m.content for m in messages] == ["kept"]


def test_system_prompt_is_not_emitted(tmp_cline_dir: Path) -> None:
    messages = _load(
        tmp_cline_dir,
        [{"id": "m1", "role": "user", "ts": 1, "content": "hi"}],
        system_prompt="You are Cline. Working Directory: /p",
    )
    assert [m.content for m in messages] == ["hi"]


def test_unsupported_transcript_version_yields_nothing(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        messages=[{"id": "m1", "role": "user", "ts": 1, "content": "hi"}],
        messages_version=99,
    )
    session = make_session(
        source_path=str(tmp_cline_dir / "sessions" / "1_a" / "1_a.messages.json"),
    )
    assert cline.ClineProvider().get_messages(session) == []


def test_missing_or_corrupt_transcript_yields_nothing(tmp_cline_dir: Path) -> None:
    provider = cline.ClineProvider()
    assert provider.get_messages(make_session(source_path=None)) == []
    assert provider.get_messages(make_session(source_path="/nope.json")) == []

    corrupt = tmp_cline_dir / "sessions" / "1_a" / "1_a.messages.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not json")
    assert provider.get_messages(make_session(source_path=str(corrupt))) == []
