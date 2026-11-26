"""Discovery and session metadata for the Antigravity CLI (agy) provider."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sesh.cache import SessionCache
from sesh.models import Provider
from sesh.providers.agy import (
    UNKNOWN_PROJECT_NAME,
    AgyProvider,
    is_valid_session_id,
    path_to_uri,
    pb_fields,
    pb_rewrite,
    rewrite_uri,
    uri_to_path,
)
from tests.helpers import (
    agy_reply,
    agy_user,
    pb_bytes,
    pb_int,
    write_agy_conversation,
)

CONV = "11111111-1111-4111-8111-111111111111"
OTHER = "44444444-4444-4444-8444-444444444444"
CHILD = "22222222-2222-4222-8222-222222222222"


def test_is_valid_session_id_rejects_traversal() -> None:
    assert is_valid_session_id(CONV)
    assert not is_valid_session_id("../" + CONV)
    assert not is_valid_session_id("AAAAAAAA-1111-4111-8111-111111111111")
    assert not is_valid_session_id(CONV + "\n")
    assert not is_valid_session_id("")
    assert not is_valid_session_id(None)


def test_workspace_uri_matches_agys_escaping() -> None:
    """Expected strings were captured from a real agy 1.5.12 store."""
    assert path_to_uri("/tmp/my dir é#1") == "file:///tmp/my%20dir%20%C3%A9%231"
    punct = "/tmp/a@b+c,(d)~e'f&g=h;i:j!k$l*m[n]o{p}q^r|s`t%u?v\"w<x>y"
    assert path_to_uri(punct) == (
        "file:///tmp/a@b+c,%28d%29~e%27f&g=h;i:j%21k$l%2Am%5Bn%5Do%7Bp%7Dq"
        "%5Er%7Cs%60t%25u%3Fv%22w%3Cx%3Ey"
    )
    assert uri_to_path(path_to_uri(punct)) == punct
    assert uri_to_path("https://example.com/x") == ""
    assert uri_to_path(None) == ""


def test_rewrite_uri_matches_on_a_path_boundary() -> None:
    assert rewrite_uri("file:///a/repo", "/a/repo", "/b/new") == "file:///b/new"
    assert rewrite_uri("file:///a/repo/sub%20dir", "/a/repo", "/b/new") == (
        "file:///b/new/sub%20dir"
    )
    assert rewrite_uri("file:///a/repo2", "/a/repo", "/b/new") is None
    assert rewrite_uri("file:///a/Repo", "/a/repo", "/b/new") is None
    assert rewrite_uri("not a uri", "/a/repo", "/b/new") is None


def test_pb_rewrite_replaces_nested_values_and_fixes_lengths() -> None:
    inner = pb_bytes(1, "old") + pb_int(2, 7)
    buf = pb_int(1, 300) + pb_bytes(2, pb_bytes(3, inner) + pb_bytes(4, "keep")) + pb_bytes(5, "old")

    new, count = pb_rewrite(buf, lambda v: b"a much longer value" if v == b"old" else None)

    assert count == 2
    fields = pb_fields(new)
    assert [(f, w) for f, w, _s, _e, _v in fields] == [(1, 0), (2, 2), (5, 2)]
    assert fields[0][4] == 300
    assert fields[2][4] == b"a much longer value"
    nested = pb_fields(pb_fields(fields[1][4])[0][4])
    assert nested[0][4] == b"a much longer value"
    assert nested[1][4] == 7
    assert pb_fields(fields[1][4])[1][4] == b"keep"


def test_pb_rewrite_leaves_untouched_and_unparseable_buffers_alone() -> None:
    buf = pb_bytes(1, "abc") + pb_int(2, 1)
    assert pb_rewrite(buf, lambda v: None) == (buf, 0)
    garbage = b"\xff\xff\xff not protobuf"
    assert pb_rewrite(garbage, lambda v: b"x") == (garbage, 0)


def test_discovers_projects_and_sessions(tmp_agy_dir) -> None:
    transcript = write_agy_conversation(
        tmp_agy_dir, workspace="/Users/me/my repo",
        records=[
            agy_user(0, "Fix the parser", created_at="2026-11-19T02:00:00Z"),
            agy_reply(1, "On it.", input_tokens=100, cache_read_tokens=20, output_tokens=5),
            agy_user(2, "Thanks"),
            agy_reply(
                3, "Done.", input_tokens=40, cache_read_tokens=200, output_tokens=7,
                model="gemini-4-argon", created_at="2026-11-19T02:05:00Z",
            ),
        ],
    )
    write_agy_conversation(tmp_agy_dir, conversation_id=OTHER, workspace="/Users/me/other")

    provider = AgyProvider()
    assert sorted(provider.discover_projects()) == [
        ("/Users/me/my repo", "my repo"),
        ("/Users/me/other", "other"),
    ]

    sessions = provider.get_sessions("/Users/me/my repo")
    assert len(sessions) == 1
    session = sessions[0]
    assert session.id == CONV
    assert session.provider is Provider.AGY
    assert session.summary == "Fix The Parser"
    assert session.source_path == str(transcript)
    assert session.message_count == 4
    assert session.model == "gemini-4-argon"
    assert session.input_tokens == 240          # last turn: input + cache read
    assert session.output_tokens == 12
    assert session.cumulative_input_tokens == 360
    assert session.start_timestamp.isoformat() == "2026-11-19T02:00:00+00:00"
    assert session.timestamp.isoformat() == "2026-11-19T02:05:00+00:00"
    assert session.subagent_count == 0
    assert session.is_empty is False
    assert session.host is None


def test_summary_falls_back_from_title_to_preview_to_first_prompt(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, title="", preview="The preview")
    write_agy_conversation(
        tmp_agy_dir, conversation_id=OTHER, with_index_row=False,
        records=[agy_user(0, "\nFirst line\nsecond line"), agy_reply(1, "ok")],
    )

    provider = AgyProvider()
    summaries = {s.id: s.summary for s in provider.get_sessions("/Users/me/repo")}

    assert summaries == {CONV: "The preview", OTHER: "First line"}


def test_unindexed_conversation_reads_its_workspace_from_its_own_db(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, workspace="/Users/me/blob repo", with_index_row=False)

    assert list(AgyProvider().discover_projects()) == [("/Users/me/blob repo", "blob repo")]


def test_conversation_without_any_workspace_goes_to_the_unknown_project(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, with_index_row=False, with_conversation_db=False)

    provider = AgyProvider()
    projects = list(provider.discover_projects())

    assert projects == [(str(tmp_agy_dir / "brain"), UNKNOWN_PROJECT_NAME)]
    assert [s.id for s in provider.get_sessions(projects[0][0])] == [CONV]


def test_subagents_are_counted_on_the_root_not_listed(tmp_agy_dir) -> None:
    grandchild = "33333333-3333-4333-8333-333333333333"
    write_agy_conversation(tmp_agy_dir)
    write_agy_conversation(tmp_agy_dir, conversation_id=CHILD, parent_id=CONV)
    write_agy_conversation(tmp_agy_dir, conversation_id=grandchild, parent_id=CHILD)

    provider = AgyProvider()
    sessions = provider.get_sessions("/Users/me/repo")

    assert [s.id for s in sessions] == [CONV]
    assert sessions[0].subagent_count == 2
    assert provider.locate(grandchild) == (CONV, "/Users/me/repo", grandchild)
    assert provider.locate(CONV) == (CONV, "/Users/me/repo", None)
    assert provider.locate(OTHER) is None


def test_sidecar_alone_links_a_child_when_the_index_is_missing(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, with_index_row=False)
    write_agy_conversation(
        tmp_agy_dir, conversation_id=CHILD, parent_id=CONV, with_index_row=False,
    )

    sessions = AgyProvider().get_sessions("/Users/me/repo")

    assert [(s.id, s.subagent_count) for s in sessions] == [(CONV, 1)]


def test_session_with_no_dialogue_is_empty(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, records=[])

    sessions = AgyProvider().get_sessions("/Users/me/repo")

    assert sessions[0].is_empty is True
    assert sessions[0].message_count == 0


def test_directories_that_are_not_conversation_ids_are_ignored(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir)
    (tmp_agy_dir / "brain" / "not-a-uuid" / ".system_generated" / "logs").mkdir(parents=True)
    (tmp_agy_dir / "brain" / "stray.txt").write_text("x")

    assert [s.id for s in AgyProvider().get_sessions("/Users/me/repo")] == [CONV]


def test_cache_serves_transcript_fields_but_not_index_fields(tmp_agy_dir, tmp_cache_dir) -> None:
    """A title agy backfills later must show up despite a warm cache."""
    transcript = write_agy_conversation(tmp_agy_dir, title="", preview="Old preview")
    cache = SessionCache()

    first = AgyProvider(cache=cache).get_sessions("/Users/me/repo")[0]
    assert first.summary == "Old preview"
    assert cache.get_sessions(str(transcript)) is not None

    conn = sqlite3.connect(tmp_agy_dir / "conversation_summaries.db")
    conn.execute(
        "UPDATE conversation_summaries SET title = 'Backfilled Title',"
        " workspace_uris = ? WHERE conversation_id = ?",
        ('["file:///Users/me/moved"]', CONV),
    )
    conn.commit()
    conn.close()

    provider = AgyProvider(cache=cache)
    assert list(provider.discover_projects()) == [("/Users/me/moved", "moved")]
    second = provider.get_sessions("/Users/me/moved")[0]
    assert second.summary == "Backfilled Title"
    assert second.project_path == "/Users/me/moved"
    assert second.message_count == first.message_count


def test_unreadable_index_is_reported_and_discovery_still_works(tmp_agy_dir) -> None:
    write_agy_conversation(tmp_agy_dir, with_index_row=False)
    (tmp_agy_dir / "conversation_summaries.db").write_text("this is not sqlite")

    provider = AgyProvider()

    assert [s.id for s in provider.get_sessions("/Users/me/repo")] == [CONV]
    issues = provider.diagnostic_issues()
    assert len(issues) == 1 and "conversation_summaries.db" in issues[0]


def test_aggregation_mode_reads_the_host_subtree(tmp_path: Path) -> None:
    host_dir = tmp_path / "agg" / "laptop"
    write_agy_conversation(host_dir / ".gemini" / "antigravity-cli")

    provider = AgyProvider(base_dir=host_dir, host="laptop")
    sessions = provider.get_sessions("/Users/me/repo")

    assert [(s.id, s.host) for s in sessions] == [(CONV, "laptop")]


def test_missing_store_yields_nothing(tmp_agy_dir) -> None:
    provider = AgyProvider()
    assert list(provider.discover_projects()) == []
    assert provider.diagnostic_issues() == []
    assert provider.diagnostic_paths() == [
        ("data", tmp_agy_dir), ("brain", tmp_agy_dir / "brain"),
    ]
