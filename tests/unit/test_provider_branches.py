from __future__ import annotations

from pathlib import Path

from sesh.models import Provider
from sesh.providers import claude, codex, pi
from sesh.providers.history import active_ancestor_ids
from tests.helpers import make_session, write_jsonl


def test_active_ancestor_ids_rejects_cycle() -> None:
    assert active_ancestor_ids({"a": "b", "b": "a"}, "a") is None
    assert active_ancestor_ids({"a": None, "b": "a"}, "b") == {"a", "b"}


def test_pi_uses_final_appended_branch_and_active_metadata(tmp_path: Path) -> None:
    path = tmp_path / "pi.jsonl"
    write_jsonl(path, [
        {"type": "session", "id": "session", "cwd": "/repo", "timestamp": "2026-01-01T00:00:00Z"},
        {"type": "model_change", "id": "root", "parentId": None},
        {"type": "message", "id": "old-u", "parentId": "root", "timestamp": "2026-01-01T00:00:01Z", "message": {"role": "user", "content": "old"}},
        {"type": "message", "id": "old-a", "parentId": "old-u", "timestamp": "2026-01-01T00:00:02Z", "message": {"role": "assistant", "content": "old answer", "usage": {"input": 10, "output": 2}}},
        {"type": "custom", "id": "connector", "parentId": "root"},
        {"type": "message", "id": "new-u", "parentId": "connector", "timestamp": "2026-01-01T00:00:03Z", "message": {"role": "user", "content": "new"}},
        {"type": "message", "id": "new-a", "parentId": "new-u", "timestamp": "2026-01-01T00:00:04Z", "message": {"role": "assistant", "content": "new answer", "usage": {"input": 20, "output": 3}}},
    ])
    provider = pi.PiProvider()
    session = make_session(id="session", provider=Provider.PI, source_path=str(path))
    assert [m.content for m in provider.get_messages(session)] == ["new", "new answer"]
    data = provider._parse_session_file(path, "/repo")
    assert data is not None
    assert data["message_count"] == 2
    assert data["summary"] == "new"
    assert data["input_tokens"] == 20
    assert data["output_tokens"] == 5  # incurred usage includes abandoned work


def test_claude_uses_last_prompt_leaf(tmp_path: Path) -> None:
    path = tmp_path / "claude.jsonl"
    sid = "session"
    def record(uuid: str, parent: str | None, role: str, text: str, second: int) -> dict:
        return {"type": role, "uuid": uuid, "parentUuid": parent, "sessionId": sid,
                "timestamp": f"2026-01-01T00:00:0{second}Z",
                "message": {"role": role, "content": text}}
    write_jsonl(path, [
        record("root", None, "user", "root", 1),
        record("old", "root", "user", "old", 2),
        record("old-a", "old", "assistant", "old answer", 3),
        record("new", "root", "user", "new", 4),
        record("new-a", "new", "assistant", "new answer", 5),
        {"type": "system", "uuid": "leaf", "parentUuid": "new-a", "sessionId": sid},
        {"type": "last-prompt", "sessionId": sid, "leafUuid": "leaf"},
    ])
    session = make_session(id=sid, provider=Provider.CLAUDE, source_path=str(path))
    assert [m.content for m in claude.ClaudeProvider().get_messages(session)] == [
        "root", "new", "new answer"
    ]


def test_claude_follows_turns_appended_after_last_prompt_checkpoint(
    tmp_path: Path,
) -> None:
    """A stale last-prompt marker must not freeze live views at that turn."""
    path = tmp_path / "claude.jsonl"
    sid = "session"
    write_jsonl(path, [
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid,
         "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:02Z",
         "message": {"role": "assistant", "content": "first answer", "model": "old",
                     "usage": {"input_tokens": 10, "output_tokens": 2}}},
        {"type": "system", "uuid": "checkpoint", "parentUuid": "a1", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:03Z"},
        {"type": "last-prompt", "sessionId": sid, "leafUuid": "checkpoint"},
        # Current Claude can append complete turns without another last-prompt.
        {"type": "user", "uuid": "u2", "parentUuid": "checkpoint", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:04Z",
         "message": {"role": "user", "content": "second prompt"}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:05Z",
         "message": {"role": "assistant", "content": "second answer", "model": "new",
                     "usage": {"input_tokens": 20, "cache_read_input_tokens": 4,
                               "output_tokens": 3}}},
        {"type": "system", "uuid": "latest", "parentUuid": "a2", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:06Z"},
    ])

    session = make_session(id=sid, provider=Provider.CLAUDE, source_path=str(path))
    assert [m.content for m in claude.ClaudeProvider().get_messages(session)] == [
        "first prompt", "first answer", "second prompt", "second answer"
    ]

    discovered = claude.ClaudeProvider()._parse_sessions(tmp_path, "/repo")
    assert len(discovered) == 1
    meta = discovered[0]
    assert meta.message_count == 4
    assert meta.summary == "second prompt"
    assert meta.model == "new"
    assert meta.input_tokens == 24
    assert meta.output_tokens == 5
    assert meta.cumulative_input_tokens == 34


def test_claude_post_checkpoint_rewind_selects_appended_branch(tmp_path: Path) -> None:
    """A replacement branch started after a checkpoint becomes active at once."""
    path = tmp_path / "claude.jsonl"
    sid = "session"

    def record(uuid: str, parent: str | None, role: str, text: str, second: int) -> dict:
        return {"type": role, "uuid": uuid, "parentUuid": parent, "sessionId": sid,
                "timestamp": f"2026-01-01T00:00:0{second}Z",
                "message": {"role": role, "content": text}}

    write_jsonl(path, [
        record("root", None, "user", "root", 1),
        record("old", "root", "user", "old", 2),
        record("old-a", "old", "assistant", "old answer", 3),
        {"type": "last-prompt", "sessionId": sid, "leafUuid": "old-a"},
        record("new", "root", "user", "new", 4),
        record("new-a", "new", "assistant", "new answer", 5),
    ])

    session = make_session(id=sid, provider=Provider.CLAUDE, source_path=str(path))
    assert [m.content for m in claude.ClaudeProvider().get_messages(session)] == [
        "root", "new", "new answer"
    ]


def test_claude_bridges_compaction_boundary(tmp_path: Path) -> None:
    """A compact_boundary record (parentUuid null, logicalParentUuid set) must
    not sever pre-compaction history: it is live and must be retained."""
    path = tmp_path / "claude.jsonl"
    sid = "session"

    def record(uuid: str, parent: str | None, role: str, text: str, second: int) -> dict:
        return {"type": role, "uuid": uuid, "parentUuid": parent, "sessionId": sid,
                "timestamp": f"2026-01-01T00:00:0{second}Z",
                "message": {"role": role, "content": text}}

    write_jsonl(path, [
        record("u1", None, "user", "before one", 1),
        record("a1", "u1", "assistant", "answer one", 2),
        # Compaction boundary: parentUuid null, real chain via logicalParentUuid.
        {"type": "system", "subtype": "compact_boundary", "uuid": "boundary",
         "parentUuid": None, "logicalParentUuid": "a1", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:03Z"},
        record("u2", "boundary", "user", "after two", 4),
        record("a2", "u2", "assistant", "answer two", 5),
        {"type": "last-prompt", "sessionId": sid, "leafUuid": "a2"},
    ])
    session = make_session(id=sid, provider=Provider.CLAUDE, source_path=str(path))
    contents = [m.content for m in claude.ClaudeProvider().get_messages(session)]
    # Pre-compaction history is retained, not dropped as "abandoned".
    assert contents == ["before one", "answer one", "after two", "answer two"]

    data = claude.ClaudeProvider()._parse_sessions(tmp_path, "/repo")
    assert len(data) == 1
    assert data[0].message_count == 4


def test_claude_retains_parallel_tool_results(tmp_path: Path) -> None:
    """Parallel tool_use blocks live on separate assistant records; only one
    tool_result sits on the leaf chain. The off-chain sibling tool_result is
    live conversation and must be re-admitted."""
    path = tmp_path / "claude.jsonl"
    sid = "session"

    write_jsonl(path, [
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid,
         "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "user", "content": "do two things"}},
        # A1: tool_use X
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:02Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "X", "name": "Read", "input": {}}]}},
        # A2: tool_use Y, on-chain child of A1
        {"type": "assistant", "uuid": "a2", "parentUuid": "a1", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:03Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "Y", "name": "Grep", "input": {}}]}},
        # R1: tool_result for X, parented to A1 -> OFF the single leaf chain.
        {"type": "user", "uuid": "r1", "parentUuid": "a1", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:04Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "X", "content": "result X body"}]}},
        # R2: tool_result for Y, parented to A2 -> ON chain.
        {"type": "user", "uuid": "r2", "parentUuid": "a2", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:05Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "Y", "content": "result Y body"}]}},
        {"type": "assistant", "uuid": "a3", "parentUuid": "r2", "sessionId": sid,
         "timestamp": "2026-01-01T00:00:06Z",
         "message": {"role": "assistant", "content": "done"}},
        {"type": "last-prompt", "sessionId": sid, "leafUuid": "a3"},
    ])
    session = make_session(id=sid, provider=Provider.CLAUDE, source_path=str(path))
    messages = claude.ClaudeProvider().get_messages(session)
    outputs = [m.tool_output for m in messages if m.content_type == "tool_result"]
    # Both tool results present even though R1 is off the leaf chain.
    assert "result X body" in outputs
    assert "result Y body" in outputs


def test_parse_sessions_bounded_opens_independent_of_session_count(
    tmp_path: Path, monkeypatch
) -> None:
    """_parse_sessions must open each file a bounded number of times regardless
    of how many sessions the directory holds — no per-session re-read."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    def record(sid: str, uuid: str, parent: str | None, role: str, text: str, second: int) -> dict:
        return {"type": role, "uuid": uuid, "parentUuid": parent, "sessionId": sid,
                "timestamp": f"2026-01-01T00:00:0{second}Z",
                "message": {"role": role, "content": text}}

    # Several sessions spread across several files.
    for file_index in range(3):
        entries = []
        for s_index in range(4):
            sid = f"sess-{file_index}-{s_index}"
            entries.extend([
                record(sid, f"{sid}-u", None, "user", "hi", 1),
                record(sid, f"{sid}-a", f"{sid}-u", "assistant", "yo", 2),
                {"type": "last-prompt", "sessionId": sid, "leafUuid": f"{sid}-a"},
            ])
        write_jsonl(project_dir / f"file-{file_index}.jsonl", entries)

    files = {str(p) for p in project_dir.glob("*.jsonl")}
    open_counts: dict[str, int] = {}
    real_open = open

    def counting_open(file, *args, **kwargs):
        key = str(file)
        if key in files:
            open_counts[key] = open_counts.get(key, 0) + 1
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(claude, "open", counting_open, raising=False)
    sessions = claude.ClaudeProvider()._parse_sessions(project_dir, "/repo")

    assert len(sessions) == 12  # 3 files x 4 sessions
    # Each file opened a bounded number of times, NOT once per session.
    assert open_counts, "expected the fixture files to be opened"
    assert max(open_counts.values()) <= 2
    assert sum(open_counts.values()) <= 2 * len(files)


def test_codex_replays_thread_rollback_by_whole_turn(tmp_path: Path) -> None:
    path = tmp_path / "codex.jsonl"
    entries = [{"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z", "payload": {"id": "session", "cwd": "/repo"}}]
    for number, (user, answer) in enumerate((("old one", "answer one"), ("old two", "answer two")), 1):
        entries.extend([
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": str(number)}},
            {"type": "event_msg", "timestamp": f"2026-01-01T00:00:0{number}Z", "payload": {"type": "user_message", "message": user}},
            {"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "thought"}},
            {"type": "response_item", "timestamp": f"2026-01-01T00:00:0{number + 2}Z", "payload": {"role": "assistant", "content": [{"text": answer}]}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": str(number)}},
        ])
    entries.extend([
        {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 2}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "3"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:06Z", "payload": {"type": "user_message", "message": "replacement"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:07Z", "payload": {"role": "assistant", "content": [{"text": "replacement answer"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "3"}},
    ])
    write_jsonl(path, entries)
    provider = codex.CodexProvider()
    session = make_session(id="session", provider=Provider.CODEX, source_path=str(path))
    messages = provider.get_messages(session)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "replacement"), ("assistant", "replacement answer")
    ]
    data = provider._parse_session_file(path)
    assert data is not None
    assert data["message_count"] == 2
    assert data["summary"] == "replacement"
