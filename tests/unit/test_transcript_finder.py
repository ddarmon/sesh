"""Unit tests for the pure transcript find-navigation model.

These cover match indexing across message / tool / thinking / sub-agent bodies,
matches beyond the collapsed-preview boundary, next/previous wraparound,
preserve-by-key on a live append, and graceful reset when the matched message
disappears --- all without mounting any Textual widget.
"""

from __future__ import annotations

from sesh.models import SubagentMeta
from sesh.transcript import compose_transcript
from sesh.transcript_view import (
    PREVIEW_CHARS,
    Match,
    TranscriptFinder,
    compute_matches,
)
from tests.helpers import make_message


def _msg(**kw):
    kw.setdefault("timestamp", None)
    return make_message(**kw)


# ---- compute_matches over composed items --------------------------------------


def test_compute_matches_over_text_bodies() -> None:
    items = compose_transcript(
        [
            _msg(role="user", content="find the needle here"),
            _msg(role="assistant", content="another needle, and needle again"),
        ]
    )
    matches = compute_matches(items, "needle")
    # Per-card counting: one match per matching body (the assistant's two hits
    # collapse to a single card match), in document order.
    assert len(matches) == 2
    assert matches[0].key == items[0].key
    assert matches[1].key == items[1].key


def test_compute_matches_empty_term_is_empty() -> None:
    items = compose_transcript([_msg(role="user", content="needle")])
    assert compute_matches(items, "") == []


def test_compute_matches_case_insensitive() -> None:
    items = compose_transcript([_msg(role="user", content="Needle NEEDLE needle")])
    # Case-insensitive match; per-card counting means one match for the one card.
    matches = compute_matches(items, "needle")
    assert len(matches) == 1
    assert matches[0].start == 0  # first span, at the leading "Needle"


def test_compute_matches_one_per_card_first_span() -> None:
    # A single card with several hits yields exactly one match at the first span.
    items = compose_transcript([_msg(role="user", content="a needle then needle")])
    matches = compute_matches(items, "needle")
    assert len(matches) == 1
    assert matches[0].key == items[0].key
    assert matches[0].start == 2  # offset of the first "needle"


def test_compute_matches_includes_tool_and_thinking_bodies() -> None:
    items = compose_transcript(
        [
            _msg(
                role="assistant",
                content="",
                content_type="tool_use",
                tool_name="bash",
                tool_input="grep needle",
            ),
            _msg(
                role="user",
                content="",
                content_type="tool_result",
                tool_name="bash",
                tool_output="needle found on line 3",
            ),
            _msg(
                role="assistant",
                content="",
                content_type="thinking",
                thinking="the needle is in the haystack",
            ),
        ]
    )
    matches = compute_matches(items, "needle")
    assert len(matches) == 3
    assert [m.key for m in matches] == [items[0].key, items[1].key, items[2].key]


def test_compute_matches_includes_subagent_interior() -> None:
    main = [_msg(role="user", content="top level")]
    meta = SubagentMeta(agent_id="ag", file_path="/x", message_count=1)
    interior = [_msg(role="assistant", content="needle inside the agent")]
    items = compose_transcript(main, [(meta, interior)])
    agent_item = next(i for i in items if i.kind == "agent")
    interior_key = agent_item.interior[0].key

    matches = compute_matches(items, "needle")
    assert len(matches) == 1
    # The hit is attributed to the interior message key, not the container.
    assert matches[0].key == interior_key


def test_compute_matches_beyond_preview_boundary() -> None:
    body = ("x" * (PREVIEW_CHARS + 100)) + "needle"
    items = compose_transcript([_msg(role="user", content=body)])
    matches = compute_matches(items, "needle")
    assert len(matches) == 1
    # The span sits past the preview boundary (full-body matching, not preview).
    assert matches[0].start >= PREVIEW_CHARS


# ---- TranscriptFinder navigation ----------------------------------------------


def _finder(term: str, items) -> TranscriptFinder:
    f = TranscriptFinder()
    f.set_term(term, items)
    return f


def test_set_term_jumps_to_first_match() -> None:
    # Two separate cards -> two matches (per-card counting).
    items = compose_transcript(
        [
            _msg(role="user", content="needle one"),
            _msg(role="assistant", content="needle two"),
        ]
    )
    f = _finder("needle", items)
    assert f.count == 2
    assert f.active_index == 0
    assert f.position() == (1, 2)
    assert f.label() == "1 / 2"


def test_set_term_no_matches_label() -> None:
    items = compose_transcript([_msg(role="user", content="haystack")])
    f = _finder("needle", items)
    assert f.count == 0
    assert f.active is None
    assert f.position() == (0, 0)
    assert f.label() == "No matches"


def test_empty_term_label_blank() -> None:
    items = compose_transcript([_msg(role="user", content="needle")])
    f = _finder("", items)
    assert f.label() == ""


def test_next_prev_wraparound() -> None:
    # Three cards -> three matches (one per card) to exercise wraparound.
    items = compose_transcript(
        [
            _msg(role="user", content="needle a"),
            _msg(role="assistant", content="needle b"),
            _msg(role="user", content="needle c"),
        ]
    )
    f = _finder("needle", items)
    assert f.active_index == 0
    assert f.next().key == items[1].key  # -> index 1
    assert f.active_index == 1
    f.next()
    assert f.active_index == 2
    # Wrap forward back to the first.
    f.next()
    assert f.active_index == 0
    # Wrap backward to the last.
    f.prev()
    assert f.active_index == 2


def test_next_from_no_active_goes_first() -> None:
    items = compose_transcript(
        [
            _msg(role="user", content="needle a"),
            _msg(role="assistant", content="needle b"),
        ]
    )
    f = TranscriptFinder()
    f.set_term("needle", items)
    # Force "no active" then navigate.
    f._active = -1
    assert f.next().key == items[0].key
    assert f.active_index == 0


def test_prev_from_no_active_goes_last() -> None:
    items = compose_transcript(
        [
            _msg(role="user", content="needle a"),
            _msg(role="assistant", content="needle b"),
        ]
    )
    f = TranscriptFinder()
    f.set_term("needle", items)
    f._active = -1
    f.prev()
    assert f.active_index == 1


def test_next_prev_no_matches_returns_none() -> None:
    items = compose_transcript([_msg(role="user", content="haystack")])
    f = _finder("needle", items)
    assert f.next() is None
    assert f.prev() is None
    assert f.active_index == -1


# ---- preserve-by-key on recompute (live append) -------------------------------


def test_recompute_preserves_active_by_key_on_append() -> None:
    first = _msg(role="user", content="needle A")
    second = _msg(role="assistant", content="needle B")
    items = compose_transcript([first, second])
    f = _finder("needle", items)
    f.next()  # active on the second message's hit
    active = f.active
    assert active is not None and active.key == items[1].key

    # Live append: a new message arrives at the end; keys of existing messages
    # are stable, so the active hit is preserved.
    appended = compose_transcript(
        [first, second, _msg(role="user", content="needle C")]
    )
    f.recompute(appended)
    assert f.count == 3
    assert f.active_key == items[1].key


def test_recompute_prefers_identical_span_then_key() -> None:
    # Two identical-content messages -> distinct keys, same body/spans.
    a = _msg(role="user", content="needle here")
    b = _msg(role="assistant", content="needle here")
    items = compose_transcript([a, b])
    f = _finder("needle", items)
    f.next()  # active on b's hit
    key_b = items[1].key

    # Recompose unchanged; active should stay on the same key.
    same = compose_transcript([a, b])
    f.recompute(same)
    assert f.active_key == key_b


def test_recompute_resets_when_active_key_disappears() -> None:
    keep = _msg(role="user", content="needle keep")
    gone = _msg(role="assistant", content="needle gone")
    items = compose_transcript([keep, gone])
    f = _finder("needle", items)
    f.next()  # active on the second (gone) message
    assert f.active_key == items[1].key

    # The matched message disappears (e.g. a tool toggle hides it).
    shrunk = compose_transcript([keep])
    f.recompute(shrunk)
    assert f.count == 1
    # Reset gracefully: no active match points at a vanished key.
    assert f.active is None
    assert f.active_index == -1


def test_recompute_to_empty_resets() -> None:
    items = compose_transcript([_msg(role="user", content="needle")])
    f = _finder("needle", items)
    f.recompute(compose_transcript([]))
    assert f.count == 0
    assert f.active is None


def test_set_same_term_preserves_active() -> None:
    items = compose_transcript(
        [
            _msg(role="user", content="needle a"),
            _msg(role="assistant", content="needle b"),
        ]
    )
    f = _finder("needle", items)
    f.next()
    assert f.active_index == 1
    # Re-setting the identical term is a no-op change -> preserve, don't reset.
    f.set_term("needle", items)
    assert f.active_index == 1


def test_match_is_frozen_hashable() -> None:
    m = Match("k", 1, 5)
    assert (m.key, m.start, m.end) == ("k", 1, 5)
    assert m == Match("k", 1, 5)
    assert hash(m) == hash(Match("k", 1, 5))
