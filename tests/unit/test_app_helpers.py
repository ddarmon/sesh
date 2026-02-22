from __future__ import annotations

from datetime import datetime, timezone

from sesh.app import SeshApp, _MODEL_SHORT, _format_duration, _relative_time, _short_model_name
from sesh.models import MoveReport, Provider, SearchResult
from tests.helpers import make_session


def setup_function() -> None:
    _MODEL_SHORT.clear()


def test_claude_opus() -> None:
    """Claude model IDs like 'claude-opus-4-6-YYYYMMDD' shorten to 'opus-4.6'."""
    assert _short_model_name("claude-opus-4-6-20250101") == "opus-4.6"


def test_claude_sonnet() -> None:
    """Claude Sonnet model IDs shorten to 'sonnet-4.6'."""
    assert _short_model_name("claude-sonnet-4-6-20250101") == "sonnet-4.6"


def test_claude_haiku() -> None:
    """Claude Haiku model IDs shorten to 'haiku-4.5'."""
    assert _short_model_name("claude-haiku-4-5-20250101") == "haiku-4.5"


def test_gpt_model_last_segment() -> None:
    """Non-Claude models use the last hyphen-segment (e.g. 'gpt-4o-mini' -> 'mini')."""
    assert _short_model_name("gpt-4o-mini") == "mini"


def test_date_suffix_falls_back_to_prefix() -> None:
    """When the last segment looks like a date (YYYYMMDD), use the first segment instead."""
    assert _short_model_name("gpt-4o-20250101") == "gpt"


def test_none_like_empty_string() -> None:
    """Empty string input returns empty string."""
    assert _short_model_name("") == ""


def test_relative_time_thresholds() -> None:
    """Relative time labels use expected buckets at boundary values."""
    now = datetime(2025, 2, 22, 15, 0, 0, tzinfo=timezone.utc)

    assert _relative_time(now, now=now) == "now"
    assert _relative_time(datetime(2025, 2, 22, 14, 59, 1, tzinfo=timezone.utc), now=now) == "now"
    assert _relative_time(datetime(2025, 2, 22, 14, 59, 0, tzinfo=timezone.utc), now=now) == "1m ago"
    assert _relative_time(datetime(2025, 2, 22, 14, 0, 1, tzinfo=timezone.utc), now=now) == "59m ago"
    assert _relative_time(datetime(2025, 2, 22, 14, 0, 0, tzinfo=timezone.utc), now=now) == "1h ago"
    assert _relative_time(datetime(2025, 2, 21, 15, 0, 1, tzinfo=timezone.utc), now=now) == "23h ago"
    assert _relative_time(datetime(2025, 2, 21, 15, 0, 0, tzinfo=timezone.utc), now=now) == "yesterday"
    assert _relative_time(datetime(2025, 2, 20, 15, 0, 0, tzinfo=timezone.utc), now=now) == "2d ago"
    assert _relative_time(datetime(2025, 2, 16, 15, 0, 1, tzinfo=timezone.utc), now=now) == "5d ago"
    assert _relative_time(datetime(2025, 2, 15, 15, 0, 0, tzinfo=timezone.utc), now=now) == "02-15 15:00"


def test_relative_time_naive_datetime_treated_as_utc() -> None:
    """Naive datetimes are interpreted as UTC for deterministic labels."""
    now = datetime(2025, 2, 22, 15, 0, 0, tzinfo=timezone.utc)
    naive_dt = datetime(2025, 2, 22, 14, 0, 0)

    assert _relative_time(naive_dt, now=now) == "1h ago"


def test_relative_time_future_timestamp_clamped_to_now() -> None:
    """Future timestamps are clamped to 'now' to tolerate clock skew."""
    now = datetime(2025, 2, 22, 15, 0, 0, tzinfo=timezone.utc)
    future = datetime(2025, 2, 22, 15, 0, 30, tzinfo=timezone.utc)

    assert _relative_time(future, now=now) == "now"


def test_format_duration_boundaries() -> None:
    """Duration formatter uses minute/hour/day buckets and hides sub-minute spans."""
    start = datetime(2025, 2, 22, 12, 0, 0, tzinfo=timezone.utc)

    assert _format_duration(start, start) == ""
    assert _format_duration(start, datetime(2025, 2, 22, 12, 0, 59, tzinfo=timezone.utc)) == ""
    assert _format_duration(start, datetime(2025, 2, 22, 12, 1, 0, tzinfo=timezone.utc)) == "1m"
    assert _format_duration(start, datetime(2025, 2, 22, 12, 59, 59, tzinfo=timezone.utc)) == "59m"
    assert _format_duration(start, datetime(2025, 2, 22, 13, 0, 0, tzinfo=timezone.utc)) == "1h"
    assert _format_duration(start, datetime(2025, 2, 23, 11, 59, 59, tzinfo=timezone.utc)) == "23h"
    assert _format_duration(start, datetime(2025, 2, 23, 12, 0, 0, tzinfo=timezone.utc)) == "1d"


def test_format_duration_naive_and_negative_inputs() -> None:
    """Naive datetimes are treated as UTC and negative spans are clamped empty."""
    start = datetime(2025, 2, 22, 12, 0, 0)
    end = datetime(2025, 2, 22, 12, 45, 0)

    assert _format_duration(start, end) == "45m"
    assert _format_duration(end, start) == ""
    assert _format_duration(None, end) == ""
    assert _format_duration(start, None) == ""


def test_session_from_search_result_claude_source_path() -> None:
    """Claude search results use the parent directory of the matched file as source_path."""
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
    """Codex search results use the file_path directly as source_path."""
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
    """Cursor search results use the file_path directly as source_path."""
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
    """All case variants of the search term are highlighted."""
    out = SeshApp._highlight_text("Needle and needle", "needle")
    assert out.count("[reverse]") == 2
    assert "Needle" in out


def test_highlight_text_regex_special_chars() -> None:
    """Regex metacharacters in the search term are escaped, not interpreted."""
    out = SeshApp._highlight_text("a.b (x)", ".b (")
    assert "[reverse].b ([/reverse]" in out


def test_highlight_text_no_match() -> None:
    """Text with no match is returned unchanged."""
    assert SeshApp._highlight_text("hello", "zzz") == "hello"


def test_resume_command_claude(monkeypatch) -> None:
    """Claude resume builds 'claude --resume <id>' with the project path."""
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: "/bin/claude")
    session = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    assert SeshApp._resume_command(session) == (["claude", "--resume", "s1"], "/repo")


def test_resume_command_codex(monkeypatch) -> None:
    """Codex resume builds 'codex resume <id>' with the project path."""
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: "/bin/codex")
    session = make_session(id="s1", provider=Provider.CODEX, project_path="/repo")
    assert SeshApp._resume_command(session) == (["codex", "resume", "s1"], "/repo")


def test_resume_command_cursor(monkeypatch) -> None:
    """Cursor resume builds 'agent --resume=<id>' with the project path."""
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: "/bin/agent")
    session = make_session(id="s1", provider=Provider.CURSOR, project_path="/repo")
    assert SeshApp._resume_command(session) == (["agent", "--resume=s1"], "/repo")


def test_resume_command_cursor_txt_returns_none(monkeypatch) -> None:
    """Cursor .txt transcript sessions can't be resumed (no session ID in Cursor's format)."""
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: "/bin/agent")
    session = make_session(
        id="s1",
        provider=Provider.CURSOR,
        project_path="/repo",
        source_path="/tmp/transcript.txt",
    )
    assert SeshApp._resume_command(session) is None


def test_resume_command_missing_binary_returns_none(monkeypatch) -> None:
    """When the provider CLI binary isn't on PATH, _resume_command returns None."""
    monkeypatch.setattr("sesh.app.shutil.which", lambda name: None)
    session = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    assert SeshApp._resume_command(session) is None


def test_format_move_status_success_all_providers() -> None:
    """Successful move across all providers shows per-provider counts."""
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
    """Partial failure includes the provider's error message in the status."""
    reports = [
        MoveReport(provider=Provider.CLAUDE, success=False, error="rename failed"),
        MoveReport(provider=Provider.CODEX, success=True),
    ]
    text = SeshApp._format_move_status("/new/path", reports)
    assert text == "Move completed with errors: claude: rename failed"


def test_format_move_status_zero_changes() -> None:
    """Provider with no dirs or files to change shows 'no changes'."""
    reports = [MoveReport(provider=Provider.CODEX, success=True)]
    text = SeshApp._format_move_status("/new/path", reports)
    assert "codex: no changes" in text


def test_format_status_suffix_fullscreen_only() -> None:
    """Fullscreen flag adds the Full:ON suffix."""
    app = SeshApp()
    app._fullscreen = True

    assert "Full:ON" in app._format_status_suffix()


def test_format_status_suffix_all_flags() -> None:
    """All visibility flags appear in the status suffix."""
    app = SeshApp()
    app._fullscreen = True
    app._show_tools = True
    app._show_thinking = True

    suffix = app._format_status_suffix()
    assert "Full:ON" in suffix
    assert "Tools:ON" in suffix
    assert "Think:ON" in suffix


def test_format_status_suffix_no_flags() -> None:
    """No flags enabled yields an empty suffix."""
    app = SeshApp()

    assert app._format_status_suffix() == ""
