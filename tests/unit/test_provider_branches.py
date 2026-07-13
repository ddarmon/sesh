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
