from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider
from sesh.providers import claude
from tests.helpers import make_session, write_jsonl


def _agent_records(
    session_id: str,
    agent_id: str,
    *,
    cwd: str = "/Users/me/repo",
    user_text: str = "Investigate the failing test",
) -> list[dict]:
    """Realistic agent-*.jsonl records: parent sessionId, agentId, isSidechain."""
    return [
        {
            "sessionId": session_id,
            "agentId": agent_id,
            "isSidechain": True,
            "timestamp": "2025-01-01T00:00:00Z",
            "cwd": cwd,
            "uuid": "u1",
            "parentUuid": None,
            "message": {"role": "user", "content": user_text},
        },
        {
            "sessionId": session_id,
            "agentId": agent_id,
            "isSidechain": True,
            "timestamp": "2025-01-01T00:00:05Z",
            "cwd": cwd,
            "uuid": "u2",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Looking into it"}],
                "model": "claude-sonnet",
                "usage": {"input_tokens": 100, "output_tokens": 42},
            },
        },
    ]


def _write_sidecar(agent_file: Path, data: dict) -> None:
    sidecar = agent_file.parent / (agent_file.stem + ".meta.json")
    sidecar.write_text(json.dumps(data))


def test_discover_current_layout_with_sidecar(tmp_path: Path) -> None:
    """Layout (a): per-session subagents dir; sidecar maps all four fields."""
    project_dir = tmp_path / "proj"
    session_id = "sess-1"
    agent_file = project_dir / session_id / "subagents" / "agent-abc123.jsonl"
    write_jsonl(agent_file, _agent_records(session_id, "abc123"))
    _write_sidecar(
        agent_file,
        {
            "agentType": "Explore",
            "isFork": True,
            "description": "Sidecar description",
            "toolUseId": "toolu_999",
        },
    )

    session = make_session(id=session_id, source_path=str(project_dir))
    metas = claude.ClaudeProvider().discover_subagents(session)

    assert len(metas) == 1
    m = metas[0]
    assert m.agent_id == "abc123"
    assert m.file_path == str(agent_file)
    assert m.description == "Sidecar description"
    assert m.agent_type == "Explore"
    assert m.is_fork is True
    assert m.tool_use_id == "toolu_999"
    assert m.message_count == 2
    assert m.output_tokens == 42
    assert m.first_timestamp == datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_discover_current_layout_description_fallback(tmp_path: Path) -> None:
    """No sidecar: description derives from the first user message (80-char cap)."""
    project_dir = tmp_path / "proj"
    session_id = "sess-1"
    long_text = "y" * 90
    agent_file = project_dir / session_id / "subagents" / "agent-def.jsonl"
    write_jsonl(agent_file, _agent_records(session_id, "def", user_text=long_text))

    session = make_session(id=session_id, source_path=str(project_dir))
    metas = claude.ClaudeProvider().discover_subagents(session)

    assert len(metas) == 1
    assert metas[0].description == long_text[:80] + "..."
    assert metas[0].agent_type is None
    assert metas[0].is_fork is False
    assert metas[0].tool_use_id is None


def test_discover_legacy_project_level_subagents(tmp_path: Path) -> None:
    """Layout (b): {project}/subagents/agent-*.jsonl, no sidecar, sessionId probe."""
    project_dir = tmp_path / "proj"
    session_id = "sess-2"
    agent_file = project_dir / "subagents" / "agent-leg.jsonl"
    write_jsonl(agent_file, _agent_records(session_id, "leg"))

    session = make_session(id=session_id, source_path=str(project_dir))
    metas = claude.ClaudeProvider().discover_subagents(session)

    assert [m.agent_id for m in metas] == ["leg"]
    assert metas[0].description == "Investigate the failing test"


def test_discover_oldest_project_dir_agent_files(tmp_path: Path) -> None:
    """Layout (c): {project}/agent-*.jsonl, sessionId probe."""
    project_dir = tmp_path / "proj"
    session_id = "sess-3"
    agent_file = project_dir / "agent-old.jsonl"
    write_jsonl(agent_file, _agent_records(session_id, "old"))

    session = make_session(id=session_id, source_path=str(project_dir))
    metas = claude.ClaudeProvider().discover_subagents(session)

    assert [m.agent_id for m in metas] == ["old"]


def test_discover_excludes_legacy_files_for_other_session(tmp_path: Path) -> None:
    """Legacy agent files whose internal sessionId differs are excluded."""
    project_dir = tmp_path / "proj"
    write_jsonl(
        project_dir / "subagents" / "agent-mine.jsonl",
        _agent_records("sess-4", "mine"),
    )
    write_jsonl(
        project_dir / "subagents" / "agent-other.jsonl",
        _agent_records("sess-OTHER", "other"),
    )
    write_jsonl(
        project_dir / "agent-otherc.jsonl",
        _agent_records("sess-OTHER", "otherc"),
    )

    session = make_session(id="sess-4", source_path=str(project_dir))
    metas = claude.ClaudeProvider().discover_subagents(session)

    assert [m.agent_id for m in metas] == ["mine"]


def test_discover_sorts_by_first_timestamp(tmp_path: Path) -> None:
    """Results are ordered by earliest timestamp across layouts."""
    project_dir = tmp_path / "proj"
    session_id = "sess-5"

    early = project_dir / session_id / "subagents" / "agent-early.jsonl"
    late = project_dir / session_id / "subagents" / "agent-late.jsonl"
    write_jsonl(early, [
        {
            "sessionId": session_id,
            "agentId": "early",
            "timestamp": "2025-01-01T00:00:00Z",
            "message": {"role": "user", "content": "a"},
        }
    ])
    write_jsonl(late, [
        {
            "sessionId": session_id,
            "agentId": "late",
            "timestamp": "2025-06-01T00:00:00Z",
            "message": {"role": "user", "content": "b"},
        }
    ])

    session = make_session(id=session_id, source_path=str(project_dir))
    metas = claude.ClaudeProvider().discover_subagents(session)
    assert [m.agent_id for m in metas] == ["early", "late"]


def test_get_subagent_messages_parses_blocks(tmp_path: Path) -> None:
    """Message loading returns parsed text / tool_use / thinking Messages."""
    project_dir = tmp_path / "proj"
    session_id = "sess-6"
    agent_file = project_dir / session_id / "subagents" / "agent-msg.jsonl"
    write_jsonl(agent_file, [
        {
            "sessionId": session_id,
            "agentId": "msg",
            "timestamp": "2025-01-01T00:00:00Z",
            "message": {"role": "user", "content": "do the thing"},
        },
        {
            "sessionId": session_id,
            "agentId": "msg",
            "timestamp": "2025-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "on it"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "x"}},
                ],
            },
        },
    ])

    session = make_session(id=session_id, source_path=str(project_dir))
    provider = claude.ClaudeProvider()
    meta = provider.discover_subagents(session)[0]
    messages = provider.get_subagent_messages(session, meta)

    types = [m.content_type for m in messages]
    assert "text" in types
    assert "thinking" in types
    assert "tool_use" in types
    tool_msg = next(m for m in messages if m.content_type == "tool_use")
    assert tool_msg.tool_name == "Read"


def test_get_subagent_messages_skips_fork_context_ref(tmp_path: Path) -> None:
    """fork-context-ref records (no message field) are skipped."""
    project_dir = tmp_path / "proj"
    session_id = "sess-7"
    agent_file = project_dir / session_id / "subagents" / "agent-fork.jsonl"
    write_jsonl(agent_file, [
        {
            "type": "fork-context-ref",
            "parentSessionId": "parent",
            "parentLastUuid": "last",
            "contextLength": 1234,
        },
        {
            "sessionId": session_id,
            "agentId": "fork",
            "timestamp": "2025-01-01T00:00:00Z",
            "message": {"role": "user", "content": "forked work"},
        },
    ])

    session = make_session(id=session_id, source_path=str(project_dir))
    provider = claude.ClaudeProvider()
    meta = provider.discover_subagents(session)[0]
    assert meta.message_count == 1
    messages = provider.get_subagent_messages(session, meta)
    assert len(messages) == 1
    assert messages[0].content == "forked work"


def test_count_subagents_current_layout_only(tmp_path: Path) -> None:
    """count_subagents counts current-layout files only, with no file reads."""
    project_dir = tmp_path / "proj"
    session_id = "sess-8"
    subdir = project_dir / session_id / "subagents"
    write_jsonl(subdir / "agent-1.jsonl", _agent_records(session_id, "1"))
    write_jsonl(subdir / "agent-2.jsonl", _agent_records(session_id, "2"))
    # Legacy files are intentionally not counted.
    write_jsonl(project_dir / "agent-legacy.jsonl", _agent_records(session_id, "legacy"))

    provider = claude.ClaudeProvider()
    assert provider.count_subagents(session_id, project_dir) == 2
    assert provider.count_subagents("missing", project_dir) == 0


def test_parse_sessions_populates_subagent_count(tmp_path: Path) -> None:
    """_parse_sessions counts current-layout sub-agents via a directory glob."""
    project_dir = tmp_path / "proj"
    session_id = "sess-count"
    write_jsonl(
        project_dir / f"{session_id}.jsonl",
        [
            {
                "sessionId": session_id,
                "cwd": "/Users/me/repo",
                "timestamp": "2025-01-01T00:00:00Z",
                "uuid": "u1",
                "parentUuid": None,
                "message": {"role": "user", "content": "start"},
            }
        ],
    )
    subdir = project_dir / session_id / "subagents"
    write_jsonl(subdir / "agent-1.jsonl", _agent_records(session_id, "1"))
    write_jsonl(subdir / "agent-2.jsonl", _agent_records(session_id, "2"))

    provider = claude.ClaudeProvider()
    sessions = provider._parse_sessions(project_dir, "/Users/me/repo")

    by_id = {s.id: s for s in sessions}
    assert by_id[session_id].subagent_count == 2


def test_get_sessions_subagent_count_survives_dir_cache(tmp_path: Path, tmp_cache_dir) -> None:
    """subagent_count round-trips through the per-directory sessions cache."""
    from sesh.cache import SessionCache

    project_dir = tmp_path / "proj"
    session_id = "sess-cache"
    write_jsonl(
        project_dir / f"{session_id}.jsonl",
        [
            {
                "sessionId": session_id,
                "cwd": "/Users/me/repo",
                "timestamp": "2025-01-01T00:00:00Z",
                "uuid": "u1",
                "parentUuid": None,
                "message": {"role": "user", "content": "start"},
            }
        ],
    )
    write_jsonl(
        project_dir / session_id / "subagents" / "agent-1.jsonl",
        _agent_records(session_id, "1"),
    )

    provider = claude.ClaudeProvider()
    provider._path_to_dir["/Users/me/repo"] = project_dir
    cache = SessionCache()

    first = provider.get_sessions("/Users/me/repo", cache=cache)
    assert {s.id: s.subagent_count for s in first}[session_id] == 1

    # Second read comes from the cache; the field must survive serialization.
    cached = provider.get_sessions("/Users/me/repo", cache=cache)
    assert {s.id: s.subagent_count for s in cached}[session_id] == 1


def test_delete_session_removes_sidecar_dir_and_legacy_files(tmp_path: Path) -> None:
    """delete_session drops the sidecar dir and legacy agent files for the session."""
    project_dir = tmp_path / "proj"
    session_id = "drop"

    # Main session file.
    write_jsonl(
        project_dir / "main.jsonl",
        [{"sessionId": session_id, "message": {"role": "user", "content": "hi"}}],
    )
    # Current-layout sidecar dir.
    sidecar_dir = project_dir / session_id
    write_jsonl(sidecar_dir / "subagents" / "agent-a.jsonl", _agent_records(session_id, "a"))
    (sidecar_dir / "tool-results").mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "tool-results" / "r.json").write_text("{}")
    # Legacy files: one ours, one for a different session (must survive).
    write_jsonl(project_dir / "subagents" / "agent-b.jsonl", _agent_records(session_id, "b"))
    write_jsonl(project_dir / "agent-c.jsonl", _agent_records(session_id, "c"))
    keep = project_dir / "agent-keep.jsonl"
    write_jsonl(keep, _agent_records("other-session", "keep"))

    session = make_session(id=session_id, source_path=str(project_dir))
    claude.ClaudeProvider().delete_session(session)

    assert not sidecar_dir.exists()
    assert not (project_dir / "subagents" / "agent-b.jsonl").exists()
    assert not (project_dir / "agent-c.jsonl").exists()
    assert keep.exists()
    assert not (project_dir / "main.jsonl").exists()


def test_move_project_rewrites_cwd_in_agent_files(tmp_claude_dir) -> None:
    """move_project rewrites cwd inside agent files across all three layouts."""
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"
    old_dir = tmp_claude_dir / "projects" / claude.encode_claude_path(old_path)

    write_jsonl(old_dir / "session.jsonl", [{"cwd": old_path, "sessionId": "s1"}])
    # (c) oldest, (b) legacy, (a) current
    write_jsonl(old_dir / "agent-c.jsonl", [{"cwd": old_path, "sessionId": "s1"}])
    write_jsonl(old_dir / "subagents" / "agent-b.jsonl", [{"cwd": old_path, "sessionId": "s1"}])
    write_jsonl(
        old_dir / "s1" / "subagents" / "agent-a.jsonl",
        [{"cwd": old_path, "sessionId": "s1"}],
    )

    provider = claude.ClaudeProvider()
    provider._path_to_dir[old_path] = old_dir
    report = provider.move_project(old_path, new_path)

    new_dir = tmp_claude_dir / "projects" / claude.encode_claude_path(new_path)
    assert report.success is True
    # main + 3 agent files rewritten
    assert report.files_modified == 4

    def _cwd(p: Path) -> str:
        return json.loads(p.read_text().splitlines()[0])["cwd"]

    assert _cwd(new_dir / "session.jsonl") == new_path
    assert _cwd(new_dir / "agent-c.jsonl") == new_path
    assert _cwd(new_dir / "subagents" / "agent-b.jsonl") == new_path
    assert _cwd(new_dir / "s1" / "subagents" / "agent-a.jsonl") == new_path


def test_discover_no_subagents_returns_empty(tmp_path: Path) -> None:
    """A session with no agent files yields an empty list."""
    project_dir = tmp_path / "proj"
    write_jsonl(
        project_dir / "main.jsonl",
        [{"sessionId": "lonely", "message": {"role": "user", "content": "hi"}}],
    )
    session = make_session(id="lonely", source_path=str(project_dir))
    assert claude.ClaudeProvider().discover_subagents(session) == []


# --- regression: review findings -----------------------------------------


def test_is_safe_session_id() -> None:
    """Traversal-safe id guard: plain ids pass; separators / pure dots fail."""
    assert claude._is_safe_session_id("abc123-DEF_4.5")
    assert not claude._is_safe_session_id("")
    assert not claude._is_safe_session_id("..")
    assert not claude._is_safe_session_id(".")
    assert not claude._is_safe_session_id("../../etc")
    assert not claude._is_safe_session_id("a/b")
    assert not claude._is_safe_session_id("/abs")


def test_delete_session_hostile_id_stays_inside_project(tmp_path: Path) -> None:
    """[finding 1] A ../-laden sessionId must never rmtree outside the project."""
    outside = tmp_path / "outside"
    (outside).mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("precious")

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # A record whose sessionId resolves (naively) to ../outside.
    hostile_id = "../outside"
    write_jsonl(
        project_dir / "main.jsonl",
        [{"sessionId": hostile_id, "message": {"role": "user", "content": "hi"}}],
    )

    session = make_session(id=hostile_id, source_path=str(project_dir))
    claude.ClaudeProvider().delete_session(session)

    # The sibling directory outside the project is untouched.
    assert outside.is_dir()
    assert sentinel.read_text() == "precious"


def test_parse_timestamp_naive_assumed_utc() -> None:
    """[finding 2] A no-offset ISO timestamp parses to an aware UTC datetime."""
    parsed = claude._parse_timestamp("2026-07-05T11:00:00")
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 7, 5, 11, 0, 0, tzinfo=timezone.utc)


def test_discover_mixed_timestamp_formats_does_not_raise(tmp_path: Path) -> None:
    """[finding 2] An agent file mixing Z and no-offset stamps scans cleanly."""
    project_dir = tmp_path / "proj"
    session_id = "sess-mix"
    agent_file = project_dir / session_id / "subagents" / "agent-mix.jsonl"
    write_jsonl(agent_file, [
        {
            "sessionId": session_id,
            "timestamp": "2026-07-05T12:00:00Z",
            "message": {"role": "user", "content": "aware"},
        },
        {
            "sessionId": session_id,
            "timestamp": "2026-07-05T11:00:00",  # no offset -> naive source
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "naive"}]},
        },
    ])

    session = make_session(id=session_id, source_path=str(project_dir))
    metas = claude.ClaudeProvider().discover_subagents(session)
    assert len(metas) == 1
    # Earliest timestamp wins and is aware (the 11:00 record).
    assert metas[0].first_timestamp == datetime(2026, 7, 5, 11, 0, 0, tzinfo=timezone.utc)


def test_parse_agent_file_survives_malformed_records(tmp_path: Path) -> None:
    """[finding 3] Non-dict lines / string message / non-dict usage are skipped."""
    project_dir = tmp_path / "proj"
    session_id = "sess-bad"
    agent_file = project_dir / session_id / "subagents" / "agent-bad.jsonl"
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    agent_file.write_text(
        "[]\n"  # valid JSON, not an object
        + json.dumps({"message": "just a string", "sessionId": session_id}) + "\n"
        + json.dumps({
            "sessionId": session_id,
            "message": {"role": "assistant", "usage": "not-a-dict",
                        "content": [{"type": "text", "text": "ok"}]},
        }) + "\n"
        + json.dumps({
            "sessionId": session_id,
            "message": {"role": "user", "content": "real user"},
        }) + "\n"
    )

    session = make_session(id=session_id, source_path=str(project_dir))
    provider = claude.ClaudeProvider()
    metas = provider.discover_subagents(session)  # must not raise
    assert len(metas) == 1
    # Only the two well-formed message records count.
    assert metas[0].message_count == 2
    messages = provider.get_subagent_messages(session, metas[0])
    contents = {m.content for m in messages}
    assert "ok" in contents
    assert "real user" in contents


def test_agent_file_without_sessionid_loads_messages(tmp_path: Path) -> None:
    """[finding 4] Records with agentId but no sessionId still load (no filter)."""
    project_dir = tmp_path / "proj"
    session_id = "sess-nosid"
    agent_file = project_dir / session_id / "subagents" / "agent-fork.jsonl"
    write_jsonl(agent_file, [
        {
            "agentId": "fork",
            "timestamp": "2025-01-01T00:00:00Z",
            "message": {"role": "user", "content": "kickoff"},
        },
        {
            "agentId": "fork",
            "timestamp": "2025-01-01T00:00:05Z",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "done"}]},
        },
    ])

    session = make_session(id=session_id, source_path=str(project_dir))
    provider = claude.ClaudeProvider()
    metas = provider.discover_subagents(session)
    assert len(metas) == 1
    assert metas[0].message_count == 2
    messages = provider.get_subagent_messages(session, metas[0])
    # Advertised count and loaded messages agree, and both are non-empty.
    assert len(messages) == metas[0].message_count > 0


def test_probe_agent_session_id_reads_head(tmp_path: Path) -> None:
    """[finding 9] The probe returns the first internal sessionId it sees."""
    agent_file = tmp_path / "agent-p.jsonl"
    write_jsonl(agent_file, [
        {"type": "fork-context-ref", "parentSessionId": "x"},  # no sessionId
        {"sessionId": "sess-head", "message": {"role": "user", "content": "hi"}},
    ])
    assert claude._probe_agent_session_id(agent_file) == "sess-head"


def test_legacy_nonmatching_file_not_fully_parsed(
    tmp_path: Path, monkeypatch
) -> None:
    """[finding 9/10] Legacy files for another session are probed, not full-read.

    Also pins the single-pass property: the one matching file is parsed
    exactly once (old code scanned then re-read it).
    """
    project_dir = tmp_path / "proj"
    session_id = "sess-mine"
    write_jsonl(
        project_dir / "subagents" / "agent-mine.jsonl",
        _agent_records(session_id, "mine"),
    )
    write_jsonl(
        project_dir / "subagents" / "agent-other.jsonl",
        _agent_records("sess-OTHER", "other"),
    )

    parsed: list[str] = []
    orig = claude._parse_agent_file

    def _counting(agent_file):
        parsed.append(Path(agent_file).name)
        return orig(agent_file)

    monkeypatch.setattr(claude, "_parse_agent_file", _counting)

    provider = claude.ClaudeProvider()
    session = make_session(id=session_id, source_path=str(project_dir))
    loaded = provider.load_subagents(session)

    assert [meta.agent_id for meta, _ in loaded] == ["mine"]
    # Non-matching file skipped via cheap probe; matching file parsed once.
    assert parsed == ["agent-mine.jsonl"]


def test_load_subagents_returns_meta_and_messages_single_pass(tmp_path: Path) -> None:
    """[finding 10] load_subagents yields (meta, messages) per file in one read."""
    project_dir = tmp_path / "proj"
    session_id = "sess-load"
    agent_file = project_dir / session_id / "subagents" / "agent-lp.jsonl"
    write_jsonl(agent_file, _agent_records(session_id, "lp"))

    session = make_session(id=session_id, source_path=str(project_dir))
    loaded = claude.ClaudeProvider().load_subagents(session)
    assert len(loaded) == 1
    meta, messages = loaded[0]
    assert meta.agent_id == "lp"
    assert meta.message_count == 2
    assert [m.content for m in messages] == ["Investigate the failing test", "Looking into it"]


def test_subagent_meta_is_claude_provider_dataclass() -> None:
    """SubagentMeta constructs with required fields and sane defaults."""
    m = claude.SubagentMeta(agent_id="x", file_path="/p")
    assert m.agent_id == "x"
    assert m.file_path == "/p"
    assert m.description is None
    assert m.is_fork is False
    assert m.message_count == 0
    assert Provider.CLAUDE  # sanity: models import path intact
