from __future__ import annotations

import json

from sesh import search


def test_stringify_value() -> None:
    assert search._stringify_value(None) == ""
    assert search._stringify_value("x") == "x"
    assert search._stringify_value({"a": 1}) == json.dumps({"a": 1})


def test_extract_content_text_from_codex_payload_prefers_query_match() -> None:
    entry = {
        "payload": {
            "type": "function_call_output",
            "output": {"status": "ok", "detail": "needle in result"},
            "content": [{"text": "long filler without token"}],
            "message": "message without token",
        }
    }
    extracted = search._extract_content_text(entry, "needle")
    assert "needle" in extracted.lower()


def test_extract_content_text_from_claude_blocks() -> None:
    entry = {
        "message": {
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "tool_use", "input": {"cmd": "echo hi"}},
                {"type": "tool_result", "content": [{"type": "text", "text": "done"}]},
            ]
        }
    }
    extracted = search._extract_content_text(entry)
    assert "echo hi" in extracted or "done" in extracted or "hmm" in extracted


def test_extract_display_text_centers_match_and_ellipsizes() -> None:
    content = "a" * 120 + "NEEDLE" + "b" * 120
    snippet = search._extract_display_text(content, "needle", max_len=50)
    assert len(snippet) == 50
    assert "NEEDLE" in snippet
    assert snippet.startswith("...")
    assert snippet.endswith("...")


def test_extract_display_text_no_match_returns_prefix() -> None:
    content = "abcdef" * 20
    assert search._extract_display_text(content, "zzz", max_len=10) == content[:10]


def test_extract_codex_session_id_prefers_uuid() -> None:
    file_path = "/tmp/prefix-123e4567-e89b-12d3-a456-426614174000.jsonl"
    assert (
        search._extract_codex_session_id(file_path)
        == "123e4567-e89b-12d3-a456-426614174000"
    )


def test_extract_codex_session_id_falls_back_to_stem() -> None:
    assert search._extract_codex_session_id("/tmp/session-name.jsonl") == "session-name"


def test_decode_cursor_projects_path_existing(monkeypatch) -> None:
    monkeypatch.setattr(
        search.Path,
        "is_dir",
        lambda p: str(p) == "/Users/me/repo",
    )
    assert search._decode_cursor_projects_path("Users-me-repo") == "/Users/me/repo"


def test_decode_cursor_projects_path_fallback() -> None:
    assert search._decode_cursor_projects_path("Users-me-repo") == "Users-me-repo"

