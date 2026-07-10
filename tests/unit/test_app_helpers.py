from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sesh.app import (
    SeshApp,
    _MODEL_SHORT,
    _compact_tokens,
    _format_duration,
    _relative_time,
    _short_model_name,
    splice_subagent_threads,
)
from sesh.models import Message, MoveReport, Provider, SearchResult, SubagentMeta
from tests.helpers import make_message, make_session


def setup_function() -> None:
    _MODEL_SHORT.clear()


def test_validate_live_source_rejects_partial_gemini_json(tmp_path) -> None:
    source = tmp_path / "session.json"
    source.write_text('{"messages": [')
    session = make_session(provider=Provider.GEMINI, source_path=str(source))

    with pytest.raises(ValueError):
        SeshApp._validate_live_source(session)


def test_validate_live_source_accepts_complete_gemini_json(tmp_path) -> None:
    source = tmp_path / "session.json"
    source.write_text('{"messages": []}')
    session = make_session(provider=Provider.GEMINI, source_path=str(source))

    SeshApp._validate_live_source(session)


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


def test_format_duration_mixed_naive_and_aware() -> None:
    """Mixed naive/aware inputs are both normalized to UTC before computing duration."""
    naive = datetime(2025, 2, 22, 12, 0, 0)
    aware = datetime(2025, 2, 22, 12, 45, 0, tzinfo=timezone.utc)

    assert _format_duration(naive, aware) == "45m"
    assert _format_duration(aware, naive) == ""


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


def test_session_from_search_result_copilot_source_path() -> None:
    """Copilot search results use the parent directory (session dir) as source_path."""
    result = SearchResult(
        session_id="abc-123",
        project_path="/repo",
        provider=Provider.COPILOT,
        matched_line="needle",
        file_path="/tmp/.copilot/session-state/abc-123/events.jsonl",
    )
    session = SeshApp._session_from_search_result(result)
    assert session is not None
    assert session.source_path == "/tmp/.copilot/session-state/abc-123"


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


def test_compact_tokens_none() -> None:
    """Both None inputs produce an empty string."""
    assert _compact_tokens(None, None) == ""


def test_compact_tokens_small() -> None:
    """Sub-1K totals show exact number."""
    assert _compact_tokens(500, 200) == "700 tok"


def test_compact_tokens_thousands() -> None:
    """Totals in the thousands show K suffix."""
    assert _compact_tokens(15000, 3000) == "18K tok"


def test_compact_tokens_millions() -> None:
    """Totals in the millions show M suffix with one decimal."""
    assert _compact_tokens(1500000, 200000) == "1.7M tok"


def test_compact_tokens_partial_none() -> None:
    """One None input treated as zero."""
    assert _compact_tokens(5000, None) == "5K tok"
    assert _compact_tokens(None, 800) == "800 tok"


def _set_sort_mode(app: SeshApp, mode: str) -> None:
    app.sort_index = app.sort_options.index(mode)


def test_sort_sessions_tokens_descending() -> None:
    """Tokens sort orders sessions by input_tokens, largest first."""
    app = SeshApp()
    _set_sort_mode(app, "tokens")
    small = make_session(id="small", input_tokens=1000)
    big = make_session(id="big", input_tokens=50000)
    medium = make_session(id="medium", input_tokens=18000)

    result = app._sort_sessions([small, big, medium])

    assert [s.id for s in result] == ["big", "medium", "small"]


def test_sort_sessions_tokens_none_sorts_last() -> None:
    """Sessions without token data are treated as 0 and sort last."""
    app = SeshApp()
    _set_sort_mode(app, "tokens")
    no_tokens = make_session(id="no-tokens", input_tokens=None)
    with_tokens = make_session(id="with-tokens", input_tokens=42)

    result = app._sort_sessions([no_tokens, with_tokens])

    assert [s.id for s in result] == ["with-tokens", "no-tokens"]


def test_sort_options_include_tokens_mode() -> None:
    """The s-cycle includes the tokens mode between messages and timeline."""
    app = SeshApp()

    assert app.sort_options == ["date", "name", "messages", "tokens", "timeline"]


def _at(hour: int, minute: int) -> datetime:
    return datetime(2025, 1, 1, hour, minute, tzinfo=timezone.utc)


def _sub(agent_id: str, ts: datetime | None) -> tuple[SubagentMeta, list[Message]]:
    meta = SubagentMeta(
        agent_id=agent_id,
        file_path=f"/tmp/agent-{agent_id}.jsonl",
        description=f"desc-{agent_id}",
        agent_type="fork",
        first_timestamp=ts,
        message_count=3,
    )
    return meta, [make_message(content=f"interior-{agent_id}")]


def test_splice_no_subagents_returns_messages_only() -> None:
    """With no sub-agents, every item is a ('message', ...) in order."""
    msgs = [make_message(content="a", timestamp=_at(10, 0))]
    out = splice_subagent_threads(msgs, [])
    assert out == [("message", msgs[0])]


def test_splice_anchors_before_first_later_message() -> None:
    """A sub-agent is inserted before the first message with a later timestamp."""
    m0 = make_message(content="m0", timestamp=_at(10, 0))
    m1 = make_message(content="m1", timestamp=_at(10, 30))
    sub = _sub("x", _at(10, 15))

    out = splice_subagent_threads([m0, m1], [sub])

    kinds = [k for k, _ in out]
    assert kinds == ["message", "agent", "message"]
    assert out[1][1][0] is sub[0]


def test_splice_appends_when_later_than_all_messages() -> None:
    """A sub-agent later than every message is appended at the end."""
    m0 = make_message(content="m0", timestamp=_at(10, 0))
    sub = _sub("x", _at(11, 0))

    out = splice_subagent_threads([m0], [sub])

    assert [k for k, _ in out] == ["message", "agent"]


def test_splice_no_timestamp_goes_trailing() -> None:
    """A sub-agent with no first_timestamp lands in the trailing section."""
    m0 = make_message(content="m0", timestamp=_at(10, 0))
    sub = _sub("x", None)

    out = splice_subagent_threads([m0], [sub])

    assert [k for k, _ in out] == ["message", "agent"]
    assert out[1][1][0] is sub[0]


def test_splice_multiple_ordered_by_anchor() -> None:
    """Multiple sub-agents interleave at their respective anchor points."""
    m0 = make_message(content="m0", timestamp=_at(10, 0))
    m1 = make_message(content="m1", timestamp=_at(12, 0))
    early = _sub("early", _at(9, 0))     # before m0
    mid = _sub("mid", _at(11, 0))        # between m0 and m1

    out = splice_subagent_threads([m0, m1], [mid, early])

    assert [k for k, _ in out] == ["agent", "message", "agent", "message"]
    assert out[0][1][0] is early[0]
    assert out[2][1][0] is mid[0]


def test_format_status_suffix_agents_flag() -> None:
    """The show_agents flag adds the Agents:ON suffix."""
    app = SeshApp()
    app._show_agents = True
    assert "Agents:ON" in app._format_status_suffix()


def test_session_label_shows_subagent_badge() -> None:
    """A session with sub-agents gets a ⑂N suffix in its tree label."""
    app = SeshApp()
    session = make_session(id="s1", subagent_count=3, summary="do a thing")
    label = app._session_label(session)
    assert "⑂3" in label


def test_session_label_no_badge_when_zero() -> None:
    """Sessions without sub-agents carry no ⑂ marker."""
    app = SeshApp()
    session = make_session(id="s1", subagent_count=0, summary="do a thing")
    assert "⑂" not in app._session_label(session)


def test_splice_mixed_naive_and_aware_timestamps() -> None:
    """[finding 2] Splicing must not crash when a message timestamp came from a
    no-offset (naive-source) stamp and the sub-agent's from a Z stamp."""
    from sesh.providers.claude import _parse_timestamp

    m0 = make_message(content="early", timestamp=_parse_timestamp("2025-01-01T10:00:00"))
    m1 = make_message(content="late", timestamp=_parse_timestamp("2025-01-01T12:00:00Z"))
    sub = _sub("x", _parse_timestamp("2025-01-01T11:00:00Z"))

    out = splice_subagent_threads([m0, m1], [sub])  # must not raise
    assert [k for k, _ in out] == ["message", "agent", "message"]


def test_agents_override_for_selection() -> None:
    """[finding 8] Only a ⑂ search hit (agent_id set) triggers the auto-show."""
    hit = SearchResult(
        session_id="s2", project_path="/repo", provider=Provider.CLAUDE,
        matched_line="agent hit", file_path="/x/subagents/agent-a.jsonl", agent_id="a",
    )
    plain = SearchResult(
        session_id="s1", project_path="/repo", provider=Provider.CLAUDE,
        matched_line="plain", file_path="/x/s1.jsonl",
    )
    assert SeshApp._agents_override_for_selection(hit) is True
    assert SeshApp._agents_override_for_selection(plain) is False
    assert SeshApp._agents_override_for_selection(make_session(id="s1")) is False


def test_agents_visible_reflects_override_without_persisting() -> None:
    """[finding 8] The override reveals agents without flipping show_agents."""
    app = SeshApp()
    app._show_agents = False
    app._agents_override = True
    assert app._agents_visible is True
    # AUTO hint shows in the status suffix while the pref stays off.
    assert "Agents:AUTO" in app._format_status_suffix()
    assert app._show_agents is False

    app._agents_override = False
    assert app._agents_visible is False
    assert "Agents:AUTO" not in app._format_status_suffix()




def test_format_session_header_full_fields() -> None:
    """The TUI details header carries provider, model, host, id, time range,
    counts, token totals, and the resume command when provided."""
    from sesh.app import format_session_header

    session = make_session(
        id="sess-9",
        provider=Provider.CLAUDE,
        project_path="/repo",
        model="claude-opus-4-8",
        start_timestamp=datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc),
        timestamp=datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc),
        input_tokens=1000,
        output_tokens=200,
        cumulative_input_tokens=5000,
        host="laptop",
    )
    header = format_session_header(
        session,
        message_count=7,
        subagent_count=2,
        resume_cmd="claude --resume sess-9",
    )
    assert "claude" in header
    assert "model claude-opus-4-8" in header
    assert "[laptop]" in header
    assert "sess-9" in header
    assert "2026-07-10 14:00 → 15:00 (1h)" in header
    assert "7 msgs" in header
    assert "⑂2" in header
    assert "1,200 ctx tokens" in header
    assert "5,200 cumulative" in header
    assert "resume: claude --resume sess-9" in header


def test_format_session_header_omits_empty_fields() -> None:
    """Model, host, sub-agents, tokens, and resume are dropped when unavailable."""
    from sesh.app import format_session_header

    session = make_session(
        id="s-min",
        provider=Provider.CURSOR,
        model=None,
        host=None,
        input_tokens=None,
        output_tokens=None,
        cumulative_input_tokens=None,
    )
    header = format_session_header(session, message_count=1)
    assert "model " not in header
    assert "[" not in header  # no host bracket
    assert "⑂" not in header
    assert "ctx tokens" not in header
    assert "cumulative" not in header
    assert "resume:" not in header
    assert "cursor" in header
    assert "1 msgs" in header
