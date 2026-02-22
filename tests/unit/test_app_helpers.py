from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

if sys.version_info < (3, 10):
    pytest.skip("app.py requires Python 3.10+ syntax at import time", allow_module_level=True)

from sesh.app import SeshApp, _MODEL_SHORT, _short_model_name
from sesh.models import MoveReport, Provider, SearchResult
from tests.helpers import make_session


def setup_function() -> None:
    _MODEL_SHORT.clear()


def test_claude_opus() -> None:
    assert _short_model_name("claude-opus-4-6-20250101") == "opus-4.6"


def test_claude_sonnet() -> None:
    assert _short_model_name("claude-sonnet-4-6-20250101") == "sonnet-4.6"


def test_claude_haiku() -> None:
    assert _short_model_name("claude-haiku-4-5-20250101") == "haiku-4.5"


def test_gpt_model_last_segment() -> None:
    assert _short_model_name("gpt-4o-mini") == "mini"


def test_date_suffix_falls_back_to_prefix() -> None:
    assert _short_model_name("gpt-4o-20250101") == "gpt"


def test_none_like_empty_string() -> None:
    assert _short_model_name("") == ""


def test_session_from_search_result_claude_source_path() -> None:
    result = SearchResult(
        session_id="s1",
        project_path="/repo",
        provider=Provider.CLAUDE,
        matched_line="needle",
        file_path="/tmp/.claude/projects/-Users-me-repo/abc.jsonl",
    )
    session = SeshApp._session_from_search_result(result)
    assert session is not None
    assert session.source_path == "/tmp/.claude/projects/-Users-me-repo"


def test_session_from_search_result_codex_source_path() -> None:
    result = SearchResult(
        session_id="s1",
        project_path="/repo",
        provider=Provider.CODEX,
        matched_line="needle",
        file_path="/tmp/.codex/sessions/x.jsonl",
    )
    session = SeshApp._session_from_search_result(result)
    assert session is not None
    assert session.source_path == "/tmp/.codex/sessions/x.jsonl"


def test_session_from_search_result_cursor_source_path() -> None:
    result = SearchResult(
        session_id="s1",
        project_path="/repo",
        provider=Provider.CURSOR,
        matched_line="needle",
        file_path="/tmp/.cursor/projects/x/agent-transcripts/s1.txt",
    )
    session = SeshApp._session_from_search_result(result)
    assert session is not None
    assert session.source_path.endswith("s1.txt")


def test_highlight_text_case_insensitive() -> None:
    out = SeshApp._highlight_text("Needle and needle", "needle")
    assert out.count("[reverse]") == 2
    assert "Needle" in out


def test_highlight_text_regex_special_chars() -> None:
    out = SeshApp._highlight_text("a.b (x)", ".b (")
    assert "[reverse].b ([/reverse]" in out


def test_highlight_text_no_match() -> None:
    assert SeshApp._highlight_text("hello", "zzz") == "hello"


def test_resume_command_claude(monkeypatch) -> None:
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: "/bin/claude")
    session = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    assert SeshApp._resume_command(session) == (["claude", "--resume", "s1"], "/repo")


def test_resume_command_codex(monkeypatch) -> None:
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: "/bin/codex")
    session = make_session(id="s1", provider=Provider.CODEX, project_path="/repo")
    assert SeshApp._resume_command(session) == (["codex", "resume", "s1"], "/repo")


def test_resume_command_cursor(monkeypatch) -> None:
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: "/bin/agent")
    session = make_session(id="s1", provider=Provider.CURSOR, project_path="/repo")
    assert SeshApp._resume_command(session) == (["agent", "--resume=s1"], "/repo")


def test_resume_command_cursor_txt_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: "/bin/agent")
    session = make_session(
        id="s1",
        provider=Provider.CURSOR,
        project_path="/repo",
        source_path="/tmp/transcript.txt",
    )
    assert SeshApp._resume_command(session) is None


def test_resume_command_missing_binary_returns_none(monkeypatch) -> None:
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: None)
    session = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    assert SeshApp._resume_command(session) is None


def test_format_move_status_success_all_providers() -> None:
    reports = [
        MoveReport(provider=Provider.CLAUDE, success=True, files_modified=2, dirs_renamed=1),
        MoveReport(provider=Provider.CODEX, success=True, files_modified=3),
        MoveReport(provider=Provider.CURSOR, success=True, dirs_renamed=2),
    ]
    text = SeshApp._format_move_status("/new/path", reports)
    assert text.startswith("Move complete -> /new/path")
    assert "claude: 1 dirs, 2 files" in text
    assert "codex: 3 files" in text
    assert "cursor: 2 dirs" in text


def test_format_move_status_partial_failure() -> None:
    reports = [
        MoveReport(provider=Provider.CLAUDE, success=False, error="rename failed"),
        MoveReport(provider=Provider.CODEX, success=True),
    ]
    text = SeshApp._format_move_status("/new/path", reports)
    assert text == "Move completed with errors: claude: rename failed"


def test_format_move_status_zero_changes() -> None:
    reports = [MoveReport(provider=Provider.CODEX, success=True)]
    text = SeshApp._format_move_status("/new/path", reports)
    assert "codex: no changes" in text
