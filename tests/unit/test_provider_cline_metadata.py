from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sesh.models import Provider
from sesh.providers import cline
from tests.helpers import write_cline_session


def _assistant(
    *,
    ts: int = 1788435548065,
    text: str = "Hi there!",
    metrics: dict | None = None,
) -> dict:
    msg: dict = {
        "id": f"msg_{ts}",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "ts": ts,
    }
    if metrics is not None:
        msg["metrics"] = metrics
    return msg


def _user(*, ts: int = 1788435478090, text: str = "Hi!") -> dict:
    return {
        "id": f"msg_{ts}",
        "role": "user",
        "content": [{"type": "text", "text": f'<user_input mode="act">{text}</user_input>'}],
        "ts": ts,
    }


def test_resolve_data_dir_prefers_cline_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLINE_DATA_DIR", "/explicit/data")
    monkeypatch.setenv("CLINE_DIR", "/other")
    assert cline.resolve_data_dir() == Path("/explicit/data")


def test_resolve_data_dir_falls_back_to_cline_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLINE_DATA_DIR", "   ")
    monkeypatch.setenv("CLINE_DIR", "/opt/cline")
    assert cline.resolve_data_dir() == Path("/opt/cline/data")


def test_resolve_data_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLINE_DATA_DIR", raising=False)
    monkeypatch.delenv("CLINE_DIR", raising=False)
    assert cline.resolve_data_dir() == cline.CLINE_DATA_DIR


def test_is_valid_session_id() -> None:
    assert cline.is_valid_session_id("1788435477952_eibi9")
    assert not cline.is_valid_session_id("../escape")
    assert not cline.is_valid_session_id("1788435477952")
    assert not cline.is_valid_session_id("abc_def")


def test_aggregation_root_ignores_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Env overrides describe this machine, not the mirrored host."""
    monkeypatch.setenv("CLINE_DATA_DIR", "/explicit/data")
    provider = cline.ClineProvider(base_dir=tmp_path / "laptop", host="laptop")
    assert provider._data_dir == tmp_path / "laptop" / ".cline" / "data"


def test_discover_projects_groups_by_workspace_root(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/Users/me/repo")
    write_cline_session(tmp_cline_dir, session_id="2_b", workspace_root="/Users/me/repo")
    write_cline_session(tmp_cline_dir, session_id="3_c", workspace_root="/Users/me/other")

    projects = sorted(cline.ClineProvider().discover_projects())
    assert projects == [
        ("/Users/me/other", "other"),
        ("/Users/me/repo", "repo"),
    ]


def test_discover_projects_names_chat_scratch_workspace(tmp_cline_dir: Path) -> None:
    chat = str(tmp_cline_dir / "workspaces" / "chat")
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root=chat)

    assert list(cline.ClineProvider().discover_projects()) == [(chat, "cline:chat")]


def test_discover_projects_falls_back_to_cwd(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir,
        session_id="1_a",
        workspace_root="",
        cwd="/Users/me/fallback",
    )
    assert list(cline.ClineProvider().discover_projects()) == [
        ("/Users/me/fallback", "fallback")
    ]


def test_discover_projects_empty_when_root_missing(tmp_cline_dir: Path) -> None:
    assert list(cline.ClineProvider().discover_projects()) == []


def test_unsafe_and_unversioned_dirs_are_skipped(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/p")
    # A future format version, and a directory name that is not a session id.
    write_cline_session(
        tmp_cline_dir, session_id="2_b", workspace_root="/p", version=99,
    )
    stray = tmp_cline_dir / "sessions" / "not-a-session-id"
    stray.mkdir(parents=True)
    (stray / "not-a-session-id.json").write_text("{}")

    sessions = cline.ClineProvider().get_sessions("/p")
    assert [s.id for s in sessions] == ["1_a"]


def test_missing_or_unparseable_record_is_skipped(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/p")
    broken = tmp_cline_dir / "sessions" / "2_b"
    broken.mkdir(parents=True)
    (broken / "2_b.json").write_text("{not json")
    (tmp_cline_dir / "sessions" / "3_c").mkdir(parents=True)

    assert [s.id for s in cline.ClineProvider().get_sessions("/p")] == ["1_a"]


def test_get_sessions_builds_metadata(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir,
        session_id="1788435477952_eibi9",
        workspace_root="/Users/me/repo",
        model="qwen3.8:27b-mlx",
        title="Investigate the flake",
        usage={
            "inputTokens": 11360,
            "outputTokens": 1373,
            "cacheReadTokens": 100,
            "cacheWriteTokens": 20,
        },
        messages=[
            _user(),
            _assistant(metrics={
                "inputTokens": 4936, "outputTokens": 478,
                "cacheReadTokens": 64, "cacheWriteTokens": 0,
            }),
        ],
    )

    (session,) = cline.ClineProvider().get_sessions("/Users/me/repo")
    assert session.id == "1788435477952_eibi9"
    assert session.provider is Provider.CLINE
    assert session.project_path == "/Users/me/repo"
    assert session.summary == "Investigate the flake"
    assert session.model == "qwen3.8:27b-mlx"
    assert session.message_count == 2
    assert session.start_timestamp == datetime(
        2026, 9, 3, 11, 37, 58, 8000, tzinfo=timezone.utc
    )
    # No updated_at on the record, so ended_at supplies the activity time.
    assert session.timestamp == datetime(
        2026, 9, 3, 11, 41, 26, 59000, tzinfo=timezone.utc
    )
    assert session.source_path.endswith("1788435477952_eibi9.messages.json")


def test_token_totals_use_usage_and_last_turn_metrics(tmp_cline_dir: Path) -> None:
    """metadata.tokensIn/tokensOut skip tool-call turns; usage.* does not."""
    write_cline_session(
        tmp_cline_dir,
        session_id="1_a",
        workspace_root="/p",
        usage={
            "inputTokens": 11360,
            "outputTokens": 1373,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
        },
        extra_record={
            "metadata": {
                "title": "Tool run",
                "size": 7850,
                "usage": {
                    "inputTokens": 11360,
                    "outputTokens": 1373,
                    "cacheReadTokens": 0,
                    "cacheWriteTokens": 0,
                },
                # Cline's own tokensIn/tokensOut skip the tool-call turn.
                "tokensIn": 8024,
                "tokensOut": 706,
            },
        },
        messages=[
            _assistant(ts=1, metrics={
                "inputTokens": 3088, "outputTokens": 228,
                "cacheReadTokens": 0, "cacheWriteTokens": 0,
            }),
            _assistant(ts=2, metrics={
                "inputTokens": 3336, "outputTokens": 667,
                "cacheReadTokens": 0, "cacheWriteTokens": 0,
            }),
            _assistant(ts=3, metrics={
                "inputTokens": 4936, "outputTokens": 478,
                "cacheReadTokens": 10, "cacheWriteTokens": 5,
            }),
        ],
    )

    (session,) = cline.ClineProvider().get_sessions("/p")
    assert session.cumulative_input_tokens == 11360
    assert session.output_tokens == 1373
    # Last assistant turn's context: 4936 + 10 + 5
    assert session.input_tokens == 4951


def test_tokens_absent_when_no_usage_or_metrics(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        messages=[_user()],
    )
    (session,) = cline.ClineProvider().get_sessions("/p")
    assert session.input_tokens is None
    assert session.output_tokens is None
    assert session.cumulative_input_tokens is None


def test_summary_falls_back_to_prompt_then_default(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        title=None, prompt="Refactor the parser",
    )
    write_cline_session(
        tmp_cline_dir, session_id="2_b", workspace_root="/p",
        title=None, prompt="",
    )
    by_id = {s.id: s for s in cline.ClineProvider().get_sessions("/p")}
    assert by_id["1_a"].summary == "Refactor the parser"
    assert by_id["2_b"].summary == "Cline Session"


def test_sessions_sorted_newest_first(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        ended_at="2026-09-03T10:00:00.000Z",
    )
    write_cline_session(
        tmp_cline_dir, session_id="2_b", workspace_root="/p",
        ended_at="2026-09-03T12:00:00.000Z",
    )
    assert [s.id for s in cline.ClineProvider().get_sessions("/p")] == ["2_b", "1_a"]


def test_timestamp_falls_back_to_transcript_updated_at(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        ended_at=None, messages_updated_at="2026-09-03T13:14:15.000Z",
    )
    (session,) = cline.ClineProvider().get_sessions("/p")
    assert session.timestamp == datetime(
        2026, 9, 3, 13, 14, 15, tzinfo=timezone.utc
    )


def test_updated_at_wins_over_ended_at(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        ended_at="2026-09-03T10:00:00.000Z",
        updated_at="2026-09-03T15:00:00.000Z",
    )
    (session,) = cline.ClineProvider().get_sessions("/p")
    assert session.timestamp.hour == 15


def test_subagents_excluded_and_counted_on_parent(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/p")
    write_cline_session(
        tmp_cline_dir, session_id="2_b", workspace_root="/p",
        is_subagent=True, parent_session_id="1_a",
    )
    write_cline_session(
        tmp_cline_dir, session_id="3_c", workspace_root="/p",
        is_subagent=True, parent_session_id="1_a",
    )

    sessions = cline.ClineProvider().get_sessions("/p")
    assert [s.id for s in sessions] == ["1_a"]
    assert sessions[0].subagent_count == 2


def test_subagent_only_workspace_yields_no_project(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="2_b", workspace_root="/child-only",
        is_subagent=True, parent_session_id="1_a",
    )
    assert list(cline.ClineProvider().discover_projects()) == []


def test_host_is_stamped_in_aggregation_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "laptop" / ".cline" / "data"
    write_cline_session(data_dir, session_id="1_a", workspace_root="/p")

    provider = cline.ClineProvider(base_dir=tmp_path / "laptop", host="laptop")
    (session,) = provider.get_sessions("/p")
    assert session.host == "laptop"
    # messages_path recorded on disk points at the source host; the provider
    # recomputes it from the directory it actually scanned.
    assert session.source_path == str(data_dir / "sessions" / "1_a" / "1_a.messages.json")


def test_cache_serves_transcript_fields_without_reparsing(
    tmp_cline_dir: Path, tmp_cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm cache answers the transcript-derived fields without a re-parse."""
    from sesh.cache import SessionCache

    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        messages=[_user(), _assistant(metrics={
            "inputTokens": 100, "outputTokens": 5,
            "cacheReadTokens": 0, "cacheWriteTokens": 0,
        })],
    )
    cache = SessionCache()
    (first,) = cline.ClineProvider(cache=cache).get_sessions("/p")
    assert (first.message_count, first.input_tokens) == (2, 100)

    def fail(*args, **kwargs):
        raise AssertionError("transcript should not be re-parsed on a cache hit")

    monkeypatch.setattr(cline.ClineProvider, "_messages_summary", staticmethod(fail))
    (second,) = cline.ClineProvider(cache=cache).get_sessions("/p")
    assert (second.message_count, second.input_tokens) == (2, 100)


def test_cache_does_not_stale_record_only_metadata(
    tmp_cline_dir: Path, tmp_cache_dir: Path
) -> None:
    """The cache is keyed on the transcript, so record fields must stay live.

    Cline backfills a generated `metadata.title` into the record alone; a
    cached summary keyed on the untouched transcript would never catch up.
    """
    from sesh.cache import SessionCache

    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        title="Old title", model="old-model", messages=[_user()],
    )
    cache = SessionCache()
    (first,) = cline.ClineProvider(cache=cache).get_sessions("/p")
    assert first.summary == "Old title"

    record_file = tmp_cline_dir / "sessions" / "1_a" / "1_a.json"
    record = json.loads(record_file.read_text())
    record["metadata"]["title"] = "Generated title"
    record["model"] = "new-model"
    record_file.write_text(json.dumps(record))

    (second,) = cline.ClineProvider(cache=cache).get_sessions("/p")
    assert second.summary == "Generated title"
    assert second.model == "new-model"
    # ...while the transcript-derived count still comes from the cache.
    assert second.message_count == 1


def test_chat_workspace_is_named_in_aggregation_mode(tmp_path: Path) -> None:
    """A mirrored record carries the source host's absolute chat path."""
    data_dir = tmp_path / "laptop" / ".cline" / "data"
    write_cline_session(
        data_dir, session_id="1_a",
        workspace_root="/Users/someone-else/.cline/data/workspaces/chat",
    )

    provider = cline.ClineProvider(base_dir=tmp_path / "laptop", host="laptop")
    assert list(provider.discover_projects()) == [
        ("/Users/someone-else/.cline/data/workspaces/chat", "cline:chat"),
    ]


def test_is_chat_workspace() -> None:
    assert cline.is_chat_workspace("/Users/me/.cline/data/workspaces/chat")
    assert cline.is_chat_workspace("/anywhere/data/workspaces/chat")
    assert not cline.is_chat_workspace("/Users/me/data/workspaces/other")
    assert not cline.is_chat_workspace("/Users/me/repo")


def test_empty_metrics_reads_as_no_token_data(tmp_cline_dir: Path) -> None:
    """An empty metrics dict is absent data, not a context size of zero."""
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p",
        messages=[_assistant(metrics={})],
    )
    (session,) = cline.ClineProvider().get_sessions("/p")
    assert session.input_tokens is None


def test_record_id_is_the_directory_name(tmp_cline_dir: Path) -> None:
    """Identity comes from the folder, which is what search and delete use."""
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/p")
    record_file = tmp_cline_dir / "sessions" / "1_a" / "1_a.json"
    record = json.loads(record_file.read_text())
    record["session_id"] = "9_z"
    record_file.write_text(json.dumps(record))

    (session,) = cline.ClineProvider().get_sessions("/p")
    assert session.id == "1_a"
    assert session.source_path.endswith("1_a.messages.json")


def test_diagnostic_paths_report_data_and_sessions_roots(tmp_cline_dir: Path) -> None:
    """`sesh doctor` needs sessions/ separately: it is the `next`-bundle signal."""
    paths = cline.ClineProvider().diagnostic_paths()
    assert ("sessions", tmp_cline_dir / "sessions") in paths
    assert ("data", tmp_cline_dir) in paths
