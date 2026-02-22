from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sesh.models import Provider
from sesh.providers import claude
from tests.helpers import make_session, write_jsonl


def test_extract_text_string() -> None:
    """String content passes through as-is."""
    assert claude._extract_text("hello") == "hello"


def test_extract_text_list() -> None:
    """List-of-blocks content joins only type='text' blocks with newlines."""
    assert claude._extract_text(
        [
            {"type": "text", "text": "one"},
            {"type": "other", "text": "skip"},
            {"type": "text", "text": "two"},
        ]
    ) == "one\ntwo"


@pytest.mark.parametrize("prefix", claude.SYSTEM_PREFIXES)
def test_is_system_message_true(prefix: str) -> None:
    """Each known system prefix is detected as a system message."""
    assert claude._is_system_message(f"{prefix} details")


def test_is_system_message_false() -> None:
    """Normal user/assistant text is not classified as system."""
    assert not claude._is_system_message("Please fix the bug in app.py")


def test_is_system_message_empty() -> None:
    """Empty string is treated as system (noise, not user content)."""
    assert claude._is_system_message("")


def test_parse_timestamp_epoch_millis() -> None:
    """Numeric timestamps (epoch milliseconds) are converted to UTC datetime."""
    assert claude._parse_timestamp(1_735_689_600_000) == datetime(
        2025, 1, 1, tzinfo=timezone.utc
    )


def test_parse_timestamp_iso() -> None:
    """ISO 8601 timestamps with timezone offset parse correctly."""
    assert claude._parse_timestamp("2025-01-01T10:20:30+00:00") == datetime(
        2025, 1, 1, 10, 20, 30, tzinfo=timezone.utc
    )


def test_parse_timestamp_iso_z() -> None:
    """ISO 8601 timestamps with trailing 'Z' parse as UTC."""
    assert claude._parse_timestamp("2025-01-01T10:20:30Z") == datetime(
        2025, 1, 1, 10, 20, 30, tzinfo=timezone.utc
    )


def test_extract_project_path_single_cwd(tmp_path: Path) -> None:
    """When all JSONL entries share one cwd, it's returned as the project path."""
    project_dir = tmp_path / "proj"
    write_jsonl(
        project_dir / "a.jsonl",
        [
            {"cwd": "/Users/me/repo", "timestamp": "2025-01-01T00:00:00Z"},
            {"cwd": "/Users/me/repo", "timestamp": "2025-01-01T00:00:01Z"},
        ],
    )
    write_jsonl(
        project_dir / "agent-noise.jsonl",
        [{"cwd": "/ignored", "timestamp": "2025-01-01T00:00:02Z"}],
    )

    assert claude._extract_project_path("Users-me-repo", project_dir) == "/Users/me/repo"


def test_extract_project_path_prefers_recent_reasonable_usage(tmp_path: Path) -> None:
    """When the most recent cwd has reasonable usage (not an overwhelming minority), prefer it."""
    project_dir = tmp_path / "proj"
    entries = [
        {"cwd": "/a", "timestamp": "2025-01-01T00:00:00Z"},
        {"cwd": "/a", "timestamp": "2025-01-01T00:00:01Z"},
        {"cwd": "/a", "timestamp": "2025-01-01T00:00:02Z"},
        {"cwd": "/a", "timestamp": "2025-01-01T00:00:03Z"},
        {"cwd": "/b", "timestamp": "2025-01-01T00:00:04Z"},
    ]
    write_jsonl(project_dir / "s.jsonl", entries)

    assert claude._extract_project_path("fallback", project_dir) == "/b"


def test_extract_project_path_falls_back_to_most_frequent(tmp_path: Path) -> None:
    """When the most recent cwd is a tiny minority, fall back to the most frequent cwd."""
    project_dir = tmp_path / "proj"
    entries = [
        {"cwd": "/a", "timestamp": "2025-01-01T00:00:00Z"},
        {"cwd": "/a", "timestamp": "2025-01-01T00:00:01Z"},
        {"cwd": "/a", "timestamp": "2025-01-01T00:00:02Z"},
        {"cwd": "/a", "timestamp": "2025-01-01T00:00:03Z"},
        {"cwd": "/a", "timestamp": "2025-01-01T00:00:04Z"},
        {"cwd": "/b", "timestamp": "2025-01-01T00:00:05Z"},
    ]
    write_jsonl(project_dir / "s.jsonl", entries)

    assert claude._extract_project_path("fallback", project_dir) == "/a"


def test_discover_projects_uses_cached_project_path(
    tmp_cache_dir, tmp_claude_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second discovery call uses the project-path cache, skipping JSONL scanning."""
    project_dir = tmp_claude_dir / "projects" / "-Users-me-repo"
    project_dir.mkdir(parents=True)

    calls: list[str] = []

    def first_extract(name: str, _entry: Path) -> str:
        calls.append(name)
        return "/Users/me/repo"

    monkeypatch.setattr(claude, "_extract_project_path", first_extract)
    discovered = list(claude.ClaudeProvider().discover_projects())
    assert discovered == [("/Users/me/repo", "repo")]
    assert calls == ["-Users-me-repo"]

    def fail_extract(*_args, **_kwargs):
        raise AssertionError("cache should avoid path extraction")

    monkeypatch.setattr(claude, "_extract_project_path", fail_extract)
    discovered_again = list(claude.ClaudeProvider().discover_projects())
    assert discovered_again == [("/Users/me/repo", "repo")]


def test_get_sessions_parses_and_groups_by_first_user_uuid(
    tmp_claude_dir,
) -> None:
    """Sessions grouped by sessionId; summaries, models, and truncation all handled correctly."""
    project_path = "/Users/me/repo"
    project_dir = tmp_claude_dir / "projects" / claude.encode_claude_path(project_path)
    project_dir.mkdir(parents=True)

    long_text = "x" * 90
    write_jsonl(
        project_dir / "sessions.jsonl",
        [
            {"type": "summary", "leafUuid": "leaf-1", "summary": "Pending Summary"},
            {
                "sessionId": "old",
                "timestamp": "2025-01-01T00:00:00Z",
                "uuid": "root-1",
                "parentUuid": None,
                "cwd": project_path,
                "message": {"role": "user", "content": "Old prompt"},
            },
            {
                "sessionId": "old",
                "timestamp": "2025-01-01T00:00:01Z",
                "parentUuid": "leaf-1",
                "message": {"role": "assistant", "content": "reply", "model": "claude-sonnet"},
            },
            {
                "sessionId": "new",
                "timestamp": "2025-01-02T00:00:00Z",
                "uuid": "root-1",
                "parentUuid": None,
                "cwd": project_path,
                "message": {"role": "user", "content": "New prompt"},
            },
            {
                "sessionId": "new",
                "timestamp": "2025-01-02T00:00:01Z",
                "parentUuid": "leaf-1",
                "message": {"role": "assistant", "content": "reply", "model": "claude-opus"},
            },
            {
                "sessionId": "fallback",
                "timestamp": "2025-01-03T00:00:00Z",
                "uuid": "root-2",
                "parentUuid": None,
                "cwd": project_path,
                "message": {"role": "user", "content": "<system-reminder> hidden"},
            },
            {
                "sessionId": "fallback",
                "timestamp": "2025-01-03T00:00:01Z",
                "message": {"role": "user", "content": long_text},
            },
            {
                "sessionId": "jsonskip",
                "timestamp": "2025-01-04T00:00:00Z",
                "type": "summary",
                "summary": '{ "weird": true }',
                "message": {"role": "assistant", "content": "ignored"},
            },
        ],
    )

    provider = claude.ClaudeProvider()
    sessions = provider.get_sessions(project_path)

    ids = {s.id for s in sessions}
    assert "new" in ids
    assert "old" not in ids
    assert "fallback" in ids
    assert "jsonskip" not in ids

    new_session = next(s for s in sessions if s.id == "new")
    assert new_session.summary == "Pending Summary"
    assert new_session.model == "claude-opus"
    assert new_session.provider is Provider.CLAUDE
    assert new_session.source_path == str(project_dir)

    fallback_session = next(s for s in sessions if s.id == "fallback")
    assert fallback_session.summary == (long_text[:80] + "...")


def test_get_sessions_uses_directory_cache(tmp_claude_dir) -> None:
    """A directory-level cache hit skips JSONL parsing entirely."""
    project_path = "/Users/me/repo"
    project_dir = tmp_claude_dir / "projects" / claude.encode_claude_path(project_path)
    project_dir.mkdir(parents=True)

    cached_session = make_session(
        id="cached",
        project_path=project_path,
        provider=Provider.CLAUDE,
        source_path=str(project_dir),
    )

    class FakeCache:
        def get_sessions_for_dir(self, dir_path: str):
            assert dir_path == str(project_dir)
            return [cached_session]

        def put_sessions_for_dir(self, dir_path: str, sessions):
            raise AssertionError("should not write cache on cache hit")

    provider = claude.ClaudeProvider()
    assert provider.get_sessions(project_path, cache=FakeCache()) == [cached_session]


def test_delete_session_removes_matching_lines_and_preserves_others(tmp_path: Path) -> None:
    """Only JSONL lines with the matching sessionId are removed; others (incl. non-JSON) kept."""
    project_dir = tmp_path / "claude-project"
    file_path = project_dir / "a.jsonl"
    file_path.parent.mkdir(parents=True)
    file_path.write_text(
        "\n".join(
            [
                '{"sessionId":"keep","message":{"role":"user","content":"a"}}',
                '{"sessionId":"drop","message":{"role":"user","content":"b"}}',
                "not json",
                "",
            ]
        )
        + "\n"
    )

    session = make_session(
        id="drop",
        provider=Provider.CLAUDE,
        source_path=str(project_dir),
    )
    claude.ClaudeProvider().delete_session(session)

    text = file_path.read_text()
    assert '"sessionId":"drop"' not in text
    assert '"sessionId":"keep"' in text
    assert "not json" in text


def test_delete_session_unlinks_empty_file(tmp_path: Path) -> None:
    """If deleting all lines from a JSONL file, the empty file is removed."""
    project_dir = tmp_path / "claude-project"
    write_jsonl(
        project_dir / "a.jsonl",
        [{"sessionId": "drop", "message": {"role": "user", "content": "x"}}],
    )
    session = make_session(id="drop", provider=Provider.CLAUDE, source_path=str(project_dir))
    claude.ClaudeProvider().delete_session(session)
    assert not (project_dir / "a.jsonl").exists()
