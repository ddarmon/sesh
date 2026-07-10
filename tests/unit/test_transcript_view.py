"""Unit tests for the pure helpers behind the TUI transcript viewer.

These cover preview/omission boundaries, the case-insensitive matcher, message
normalization, and row composition (agent collapse/expand) without mounting any
Textual widgets.
"""

from __future__ import annotations

from sesh.models import SubagentMeta
from sesh.transcript import compose_transcript
from sesh.transcript_view import (
    PREVIEW_CHARS,
    CardEntry,
    build_rows,
    find_match_spans,
    normalize_message,
    omission_marker,
    preview_and_omitted,
)
from tests.helpers import make_message


def test_omission_marker_pluralization() -> None:
    assert omission_marker(1) == "… 1 more character"
    assert omission_marker(4280) == "… 4,280 more characters"


def test_preview_and_omitted_short_body_not_truncated() -> None:
    body = "x" * PREVIEW_CHARS
    shown, omitted = preview_and_omitted(body)
    assert shown == body
    assert omitted == 0


def test_preview_and_omitted_boundary() -> None:
    # One over the limit -> exactly one omitted character.
    body = "x" * (PREVIEW_CHARS + 1)
    shown, omitted = preview_and_omitted(body)
    assert len(shown) == PREVIEW_CHARS
    assert omitted == 1


def test_preview_and_omitted_large_body() -> None:
    body = "y" * (PREVIEW_CHARS + 5000)
    shown, omitted = preview_and_omitted(body)
    assert shown == body[:PREVIEW_CHARS]
    assert omitted == 5000


def test_find_match_spans_case_insensitive_nonoverlapping() -> None:
    spans = find_match_spans("Needle and needle", "needle")
    assert spans == [(0, 6), (11, 17)]


def test_find_match_spans_literal_not_regex() -> None:
    # Regex metacharacters must be treated literally.
    spans = find_match_spans("a.b (x)", ".b (")
    assert spans == [(1, 5)]


def test_find_match_spans_empty_term_or_no_match() -> None:
    assert find_match_spans("hello", "") == []
    assert find_match_spans("hello", "zzz") == []


def test_normalize_message_per_type() -> None:
    user = make_message(role="user", content="hi", timestamp=None)
    assert normalize_message(user) == ("user", "User", "hi")

    asst = make_message(role="assistant", content="yo", timestamp=None)
    assert normalize_message(asst) == ("assistant", "Assistant", "yo")

    think = make_message(
        role="assistant", content="", content_type="thinking",
        thinking="reasoning", timestamp=None,
    )
    assert normalize_message(think) == ("thinking", "Thinking", "reasoning")

    call = make_message(
        role="assistant", content="", content_type="tool_use",
        tool_name="bash", tool_input="pwd", timestamp=None,
    )
    assert normalize_message(call) == ("tool", "bash (call)", "pwd")

    result = make_message(
        role="user", content="", content_type="tool_result",
        tool_name="bash", tool_output="/repo", timestamp=None,
    )
    assert normalize_message(result) == ("tool", "bash (result)", "/repo")


def test_build_rows_plain_messages() -> None:
    msgs = [
        make_message(role="user", content="a", timestamp=None),
        make_message(role="assistant", content="b", timestamp=None),
    ]
    items = compose_transcript(msgs)
    rows = build_rows(items, set())
    assert [r.role for r in rows] == ["user", "assistant"]
    assert all(r.depth == 0 and not r.agent for r in rows)
    # Keys line up with the composed items.
    assert [r.key for r in rows] == [items[0].key, items[1].key]


def test_build_rows_agent_collapsed_hides_interior() -> None:
    main = [make_message(role="user", content="hi", timestamp=None)]
    meta = SubagentMeta(agent_id="ag", file_path="/x", message_count=1)
    interior = [make_message(role="assistant", content="deep", timestamp=None)]
    items = compose_transcript(main, [(meta, interior)])
    rows = build_rows(items, set())  # nothing expanded
    assert any(r.agent for r in rows)
    # Interior message is not present while the container is collapsed.
    assert all(r.depth == 0 for r in rows)


def test_build_rows_agent_expanded_reveals_indented_interior() -> None:
    main = [make_message(role="user", content="hi", timestamp=None)]
    meta = SubagentMeta(agent_id="ag", file_path="/x", message_count=1)
    interior = [make_message(role="assistant", content="deep", timestamp=None)]
    items = compose_transcript(main, [(meta, interior)])
    agent_item = next(i for i in items if i.kind == "agent")
    rows = build_rows(items, {agent_item.key})
    interior_rows = [r for r in rows if r.depth == 1]
    assert len(interior_rows) == 1
    assert interior_rows[0].parent_key == agent_item.key
    assert interior_rows[0].body == "deep"


def test_card_entry_defaults() -> None:
    entry = CardEntry(key="k", role="user", header="User", body="hi")
    assert entry.depth == 0
    assert entry.agent is False
    assert entry.parent_key is None
