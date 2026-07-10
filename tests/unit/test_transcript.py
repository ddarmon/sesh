from __future__ import annotations

from datetime import datetime, timezone

from sesh import transcript
from sesh.models import SubagentMeta, filter_messages
from tests.helpers import make_message


def _keys(items: list[transcript.TranscriptItem]) -> list[str]:
    return [it.key for it in items]


def _subagent(agent_id="ag-1", first_timestamp=None, interior=None):
    meta = SubagentMeta(
        agent_id=agent_id,
        file_path=f"/proj/subagents/agent-{agent_id}.jsonl",
        description="investigate",
        agent_type="Explore",
        is_fork=True,
        first_timestamp=first_timestamp,
        message_count=len(interior or []),
    )
    if interior is None:
        interior = [make_message(role="assistant", content="nested", timestamp=None)]
    return meta, interior


# --- digest / key primitives -------------------------------------------------


def test_message_key_format_and_parts() -> None:
    key = transcript.message_key("main", "abc123", 0)
    assert key == "main-abc123-0"


def test_agent_anchor_uses_agent_id() -> None:
    assert transcript.agent_anchor("ag-42") == "agent-ag_42"


def test_digest_ignores_timestamp_formatting_but_reflects_content() -> None:
    a = make_message(role="user", content="hello", timestamp=None)
    b = make_message(role="user", content="hello", timestamp=None)
    c = make_message(role="user", content="world", timestamp=None)
    assert transcript.message_digest(a) == transcript.message_digest(b)
    assert transcript.message_digest(a) != transcript.message_digest(c)


def test_digest_distinguishes_content_type_for_same_text() -> None:
    """A tool_use and a text message with equal .content still differ."""
    text = make_message(role="assistant", content="x", timestamp=None)
    tool = make_message(
        role="assistant", content="x", content_type="tool_use",
        tool_name="Bash", tool_input="x", timestamp=None,
    )
    assert transcript.message_digest(text) != transcript.message_digest(tool)


# --- assign_message_keys -----------------------------------------------------


def test_duplicate_content_gets_distinct_occurrence_keys() -> None:
    dup = lambda: make_message(role="user", content="same", timestamp=None)  # noqa: E731
    keys = transcript.assign_message_keys([dup(), dup(), dup()])
    assert keys[0].endswith("-0")
    assert keys[1].endswith("-1")
    assert keys[2].endswith("-2")
    assert len(set(keys)) == 3


def test_keys_unique_within_transcript() -> None:
    msgs = [
        make_message(role="user", content="a", timestamp=None),
        make_message(role="assistant", content="a", timestamp=None),
        make_message(role="user", content="a", timestamp=None),
        make_message(role="assistant", content="b", timestamp=None),
    ]
    keys = transcript.assign_message_keys(msgs)
    assert len(set(keys)) == len(keys)


def test_keys_stable_across_append() -> None:
    a = make_message(role="user", content="q", timestamp=None)
    b = make_message(role="assistant", content="r", timestamp=None)
    before = transcript.assign_message_keys([a, b])
    after = transcript.assign_message_keys([a, b, make_message(content="new", timestamp=None)])
    assert after[:2] == before


def test_keys_stable_when_message_inserted_ahead() -> None:
    existing = make_message(
        role="assistant", content="", content_type="tool_use",
        tool_name="bash", tool_input="pwd", timestamp=None,
    )
    before = transcript.assign_message_keys([existing])
    after = transcript.assign_message_keys(
        [make_message(content="inserted", timestamp=None), existing]
    )
    assert before[0] == after[1]


def test_keys_stable_across_visibility_filtering() -> None:
    """A surviving message's key is identical whether tools/thinking are shown.

    Keys computed over the full list must equal keys computed over the
    tool/thinking-filtered list for every message that survives the filter.
    """
    dup_text = lambda: make_message(role="user", content="dup", timestamp=None)  # noqa: E731
    full = [
        dup_text(),
        make_message(role="assistant", content="", content_type="thinking",
                     thinking="secret", timestamp=None),
        make_message(role="assistant", content="", content_type="tool_use",
                     tool_name="Bash", tool_input="ls", timestamp=None),
        dup_text(),  # identical to the first, with hidden content in between
    ]
    full_keys = dict(zip(map(id, full), transcript.assign_message_keys(full)))

    visible = filter_messages(full, include_tools=False, include_thinking=False)
    visible_keys = transcript.assign_message_keys(visible)

    for m, key in zip(visible, visible_keys):
        assert full_keys[id(m)] == key
    # And the two identical texts are still distinguished.
    assert visible_keys[0] != visible_keys[1]


def test_missing_and_mixed_timestamps_are_deterministic() -> None:
    naive = make_message(role="user", content="t", timestamp=datetime(2026, 7, 5, 11, 0))
    aware = make_message(role="user", content="t",
                         timestamp=datetime(2026, 7, 5, 11, 0, tzinfo=timezone.utc))
    none_ts = make_message(role="user", content="t", timestamp=None)
    keys = transcript.assign_message_keys([naive, aware, none_ts])
    # All three differ (distinct timestamp material) and recompute identically.
    assert len(set(keys)) == 3
    assert transcript.assign_message_keys([naive, aware, none_ts]) == keys


# --- compose_transcript ------------------------------------------------------


def test_compose_plain_thread_has_message_items_only() -> None:
    msgs = [make_message(role="user", content="hi", timestamp=None)]
    items = transcript.compose_transcript(msgs)
    assert [it.kind for it in items] == ["message"]
    assert items[0].message is msgs[0]


def test_compose_anchors_subagent_chronologically() -> None:
    msgs = [
        make_message(role="user", content="early",
                     timestamp=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)),
        make_message(role="assistant", content="late",
                     timestamp=datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)),
    ]
    meta, interior = _subagent(
        first_timestamp=datetime(2025, 1, 1, 0, 30, tzinfo=timezone.utc)
    )
    items = transcript.compose_transcript(msgs, [(meta, interior)])
    assert [it.kind for it in items] == ["message", "agent", "message"]
    assert items[1].key == transcript.agent_anchor("ag-1")
    assert items[1].meta is meta


def test_compose_trails_subagent_without_timestamp() -> None:
    msgs = [make_message(role="user", content="hi",
                         timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc))]
    meta, interior = _subagent(first_timestamp=None)
    items = transcript.compose_transcript(msgs, [(meta, interior)])
    assert [it.kind for it in items] == ["message", "agent"]


def test_subagent_insertion_does_not_shift_main_keys() -> None:
    msgs = [
        make_message(role="user", content="a",
                     timestamp=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)),
        make_message(role="assistant", content="b",
                     timestamp=datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)),
    ]
    without = transcript.compose_transcript(msgs)
    meta, interior = _subagent(
        first_timestamp=datetime(2025, 1, 1, 0, 30, tzinfo=timezone.utc)
    )
    with_agent = transcript.compose_transcript(msgs, [(meta, interior)])
    main_keys_after = [it.key for it in with_agent if it.kind == "message"]
    assert main_keys_after == _keys(without)


def test_interior_keys_namespaced_by_agent_id() -> None:
    interior = [make_message(role="assistant", content="shared", timestamp=None)]
    meta_a, int_a = _subagent(agent_id="aaa", interior=list(interior))
    meta_b, int_b = _subagent(agent_id="bbb", interior=list(interior))
    items = transcript.compose_transcript([], [(meta_a, int_a), (meta_b, int_b)])
    agents = [it for it in items if it.kind == "agent"]
    key_a = agents[0].interior[0].key
    key_b = agents[1].interior[0].key
    # Same interior content, different agent namespace -> different keys.
    assert key_a != key_b
    assert key_a.startswith("aaa-")
    assert key_b.startswith("bbb-")


def test_interior_keys_differ_from_main_thread_for_same_content() -> None:
    main_msg = make_message(role="user", content="same everywhere", timestamp=None)
    interior_msg = make_message(role="user", content="same everywhere", timestamp=None)
    meta, _ = _subagent(agent_id="ag-9", interior=[interior_msg])
    items = transcript.compose_transcript([main_msg], [(meta, [interior_msg])])
    main = [it for it in items if it.kind == "message"][0]
    agent = [it for it in items if it.kind == "agent"][0]
    assert main.key.startswith("main-")
    assert agent.interior[0].key.startswith("ag_9-")
    assert main.key != agent.interior[0].key


def test_all_composed_keys_unique() -> None:
    msgs = [make_message(role="user", content="dup", timestamp=None) for _ in range(2)]
    meta, interior = _subagent(agent_id="z", interior=[
        make_message(role="user", content="dup", timestamp=None) for _ in range(2)
    ])
    items = transcript.compose_transcript(msgs, [(meta, interior)])
    all_keys = []
    for it in items:
        all_keys.append(it.key)
        all_keys.extend(x.key for x in it.interior)
    assert len(set(all_keys)) == len(all_keys)
