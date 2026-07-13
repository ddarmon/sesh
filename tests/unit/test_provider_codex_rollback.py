"""Codex rollback-replay and turn-bookkeeping tests.

Turn model under test (implemented identically in
``codex._active_dialogue_metadata`` and ``codex.CodexProvider._parse_rollout_file``):

* ``task_started`` closes any in-progress turn (preserving its dialogue) and
  opens a fresh one -- so aborted turns and nested ``task_started`` sequences
  keep their messages.
* dialogue outside an explicit turn opens its own implicit turn.
* ``thread_rolled_back`` flushes any in-progress (aborted) turn, then removes
  the last N completed turns.
"""

from __future__ import annotations

from pathlib import Path

from sesh.models import Provider
from sesh.providers import codex
from tests.helpers import make_session, write_jsonl

HEADER = {
    "type": "session_meta",
    "timestamp": "2026-01-01T00:00:00Z",
    "payload": {"id": "session", "cwd": "/repo"},
}


def _dialogue(messages: list) -> list[tuple[str, str]]:
    return [(m.role, m.content) for m in messages if m.role in ("user", "assistant")]


def test_aborted_turn_dialogue_preserved(tmp_path: Path) -> None:
    """An interrupted turn's messages remain in the transcript and the count."""
    path = tmp_path / "codex.jsonl"
    entries = [
        HEADER,
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "1"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
         "payload": {"type": "user_message", "message": "first ask"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
         "payload": {"role": "assistant", "content": [{"text": "partial answer"}]}},
        # User interrupt: turn_aborted event, no task_complete for this turn.
        {"type": "event_msg", "payload": {"type": "turn_aborted"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "2"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:03Z",
         "payload": {"type": "user_message", "message": "second ask"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:04Z",
         "payload": {"role": "assistant", "content": [{"text": "final answer"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "2"}},
    ]
    write_jsonl(path, entries)
    provider = codex.CodexProvider()
    session = make_session(id="session", provider=Provider.CODEX, source_path=str(path))
    messages = provider.get_messages(session)
    assert _dialogue(messages) == [
        ("user", "first ask"),
        ("assistant", "partial answer"),
        ("user", "second ask"),
        ("assistant", "final answer"),
    ]
    data = provider._parse_session_file(path)
    assert data is not None
    assert data["message_count"] == 4
    assert data["summary"] == "first ask"


def test_nested_task_started(tmp_path: Path) -> None:
    """A task_started inside an open turn flushes it; both dialogue turns count."""
    path = tmp_path / "codex.jsonl"
    entries = [
        HEADER,
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "1"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
         "payload": {"type": "user_message", "message": "outer ask"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "1b"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
         "payload": {"role": "assistant", "content": [{"text": "nested answer"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "1b"}},
    ]
    write_jsonl(path, entries)
    provider = codex.CodexProvider()
    session = make_session(id="session", provider=Provider.CODEX, source_path=str(path))
    messages = provider.get_messages(session)
    assert _dialogue(messages) == [
        ("user", "outer ask"),
        ("assistant", "nested answer"),
    ]
    data = provider._parse_session_file(path)
    assert data is not None
    assert data["message_count"] == 2


def test_rollback_after_abort_removes_aborted_turn(tmp_path: Path) -> None:
    """num_turns=1 removes the aborted turn; the completed turn 1 survives.

    Design choice: thread_rolled_back flushes the in-progress (aborted) turn
    so it counts as one of the N removed turns. Here N=1 removes the aborted
    turn 2 but keeps the completed turn 1 and the replacement turn 3.
    """
    path = tmp_path / "codex.jsonl"
    entries = [
        HEADER,
        # Turn 1: completed.
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "1"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
         "payload": {"type": "user_message", "message": "keep me"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
         "payload": {"role": "assistant", "content": [{"text": "kept answer"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "1"}},
        # Turn 2: aborted (no task_complete).
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "2"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:03Z",
         "payload": {"type": "user_message", "message": "abort me"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:04Z",
         "payload": {"role": "assistant", "content": [{"text": "aborted answer"}]}},
        {"type": "event_msg", "payload": {"type": "turn_aborted"}},
        # Roll back a single turn (the aborted one).
        {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}},
        # Turn 3: replacement.
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "3"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:05Z",
         "payload": {"type": "user_message", "message": "replacement"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:06Z",
         "payload": {"role": "assistant", "content": [{"text": "replacement answer"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "3"}},
    ]
    write_jsonl(path, entries)
    provider = codex.CodexProvider()
    session = make_session(id="session", provider=Provider.CODEX, source_path=str(path))
    messages = provider.get_messages(session)
    assert _dialogue(messages) == [
        ("user", "keep me"),
        ("assistant", "kept answer"),
        ("user", "replacement"),
        ("assistant", "replacement answer"),
    ]
    # Transcript and metadata must agree on the active dialogue count.
    data = provider._parse_session_file(path)
    assert data is not None
    assert data["message_count"] == len(_dialogue(messages)) == 4
    assert data["summary"] == "keep me"


def test_non_dict_jsonl_lines_do_not_crash(tmp_path: Path) -> None:
    """A JSONL array/string/non-dict-payload line is skipped, not fatal."""
    path = tmp_path / "codex.jsonl"
    entries = [
        HEADER,
        [1, 2],  # non-dict entry
        "just a string",  # non-dict entry
        {"type": "event_msg", "payload": "not-a-dict"},  # non-dict payload
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "1"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
         "payload": {"type": "user_message", "message": "hello there"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
         "payload": {"role": "assistant", "content": [{"text": "hi back"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "1"}},
    ]
    write_jsonl(path, entries)

    # _active_dialogue_metadata must not raise.
    count, first = codex._active_dialogue_metadata(path)
    assert count == 2
    assert first == "hello there"

    # _parse_session_file must not raise and returns the same count.
    provider = codex.CodexProvider()
    data = provider._parse_session_file(path)
    assert data is not None
    assert data["message_count"] == 2

    # get_messages must not raise either.
    session = make_session(id="session", provider=Provider.CODEX, source_path=str(path))
    messages = provider.get_messages(session)
    assert _dialogue(messages) == [
        ("user", "hello there"),
        ("assistant", "hi back"),
    ]


def test_rollback_with_no_completed_turns(tmp_path: Path) -> None:
    """A rollback before any turn completes doesn't crash and clears state."""
    path = tmp_path / "codex.jsonl"
    entries = [
        HEADER,
        # In-progress turn, then an immediate rollback of 1 (nothing completed).
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "1"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
         "payload": {"type": "user_message", "message": "aborted early"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
         "payload": {"role": "assistant", "content": [{"text": "abandoned"}]}},
        {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}},
        # Fresh turn after the rollback.
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "2"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:03Z",
         "payload": {"type": "user_message", "message": "restart"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:04Z",
         "payload": {"role": "assistant", "content": [{"text": "restarted"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "2"}},
    ]
    write_jsonl(path, entries)
    provider = codex.CodexProvider()
    session = make_session(id="session", provider=Provider.CODEX, source_path=str(path))
    # The in-progress turn is flushed then removed by the rollback, leaving
    # only the fresh turn.
    messages = provider.get_messages(session)
    assert _dialogue(messages) == [
        ("user", "restart"),
        ("assistant", "restarted"),
    ]
    data = provider._parse_session_file(path)
    assert data is not None
    assert data["message_count"] == len(_dialogue(messages)) == 2
    assert data["summary"] == "restart"


def test_empty_dialogue_records_do_not_shift_rollback_turns(tmp_path: Path) -> None:
    """A record that renders nothing (empty user_message, blank assistant
    text) must not open or extend a turn in the metadata replay: it would
    become a phantom turn that _parse_rollout_file never sees, and a later
    rollback would cut different turns in the two replays."""
    path = tmp_path / "codex.jsonl"
    entries = [
        HEADER,
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "1"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
         "payload": {"type": "user_message", "message": "keep me"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
         "payload": {"role": "assistant", "content": [{"text": "kept answer"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "1"}},
        # Out-of-turn records that render nothing.
        {"type": "event_msg", "payload": {"type": "user_message", "message": ""}},
        {"type": "response_item", "payload": {"role": "assistant", "content": [{"text": "  "}]}},
        # Rolls back the LAST REAL turn in both replays, not a phantom one.
        {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "2"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:03Z",
         "payload": {"type": "user_message", "message": "replacement"}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:04Z",
         "payload": {"role": "assistant", "content": [{"text": "replacement answer"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "2"}},
    ]
    write_jsonl(path, entries)
    provider = codex.CodexProvider()
    session = make_session(id="session", provider=Provider.CODEX, source_path=str(path))
    messages = provider.get_messages(session)
    assert _dialogue(messages) == [
        ("user", "replacement"),
        ("assistant", "replacement answer"),
    ]
    data = provider._parse_session_file(path)
    assert data is not None
    assert data["message_count"] == len(_dialogue(messages)) == 2
    assert data["summary"] == "replacement"


def test_empty_dialogue_records_not_counted_without_rollback(tmp_path: Path) -> None:
    """message_count matches the rendered transcript: empty user_message and
    blank assistant records are excluded even with no rollback at all."""
    path = tmp_path / "codex.jsonl"
    entries = [
        HEADER,
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "1"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": ""}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
         "payload": {"type": "user_message", "message": "hello"}},
        {"type": "response_item", "payload": {"role": "assistant", "content": [{"text": ""}]}},
        {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
         "payload": {"role": "assistant", "content": [{"text": "hi back"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "1"}},
    ]
    write_jsonl(path, entries)
    provider = codex.CodexProvider()
    session = make_session(id="session", provider=Provider.CODEX, source_path=str(path))
    messages = provider.get_messages(session)
    assert _dialogue(messages) == [("user", "hello"), ("assistant", "hi back")]
    data = provider._parse_session_file(path)
    assert data is not None
    assert data["message_count"] == len(_dialogue(messages)) == 2
    assert data["summary"] == "hello"
