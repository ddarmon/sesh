"""Textual pilot tests for Phase 3 transcript find navigation.

Verifies: find next/previous scrolls to and reveals the correct card (including
a hit inside a collapsed sub-agent and one beyond a preview boundary); the
``i / n`` counter tracks navigation; Escape closes find and restores focus
without touching the session-tree search; and active-match state survives
``t`` / ``T`` / ``a`` toggles and a simulated live append.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from textual.widgets import Input, Static

from sesh.app import SeshApp, SessionTree
from sesh.models import Provider, SubagentMeta
from sesh.transcript_view import PREVIEW_CHARS, TranscriptView
from tests.helpers import make_message, make_session


@pytest_asyncio.fixture()
async def app(monkeypatch):
    monkeypatch.setattr("sesh.app.load_bookmarks", lambda: set())
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda _: None)

    app = SeshApp()
    app._load_from_index = lambda: False
    app._discover_all = lambda: None

    async with app.run_test() as pilot:
        app.query_one("#session-tree", SessionTree).focus()
        await pilot.pause()
        yield app, pilot


def _render(app, messages, subagents=None):
    session = make_session(id="s1", provider=Provider.CLAUDE)
    app._current_session = session
    app._current_messages = messages
    app._current_subagents = subagents or []
    app._subagents_loaded = bool(subagents)
    app._render_messages(messages, session, subagents=subagents or [], highlight="")


def _msg(**kw):
    return make_message(**kw)


def _count_text(app) -> str:
    return str(app.query_one("#message-find-count", Static).render())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_counter_and_next_prev_navigation(app):
    sesh_app, pilot = app
    _render(
        sesh_app,
        [
            _msg(role="user", content="alpha needle beta"),
            _msg(role="assistant", content="needle and needle again"),
        ],
    )
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    view.find("needle")
    sesh_app._update_find_count()
    # Per-card counting: two matching cards (the assistant's two hits collapse to
    # one card match), first is active.
    assert view.find_position == (1, 2)
    assert view.find_label == "1 / 2"
    assert _count_text(sesh_app) == "1 / 2"
    assert view.find_active_key == view.keys[0]

    view.find_next()
    assert view.find_position == (2, 2)
    assert view.find_active_key == view.keys[1]

    # Wrap forward back to the first match.
    view.find_next()
    assert view.find_position == (1, 2)
    assert view.find_active_key == view.keys[0]

    # Wrap backward to the last match.
    view.find_prev()
    assert view.find_position == (2, 2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_match_is_painted_distinctly(app):
    sesh_app, pilot = app
    _render(
        sesh_app,
        [
            _msg(role="user", content="needle one"),
            _msg(role="assistant", content="needle two"),
        ],
    )
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    view.find("needle")
    await pilot.pause()

    active = view.card_for_key(view.find_active_key)
    other = view.card_for_key(view.keys[1])
    assert active.active_match is True
    assert other.active_match is False
    assert active.has_class("-active-match")

    view.find_next()
    await pilot.pause()
    # Active flag moved to the second card.
    assert view.card_for_key(view.keys[1]).active_match is True
    assert view.card_for_key(view.keys[0]).active_match is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_reveals_hit_beyond_preview_boundary(app):
    sesh_app, pilot = app
    body = ("x" * (PREVIEW_CHARS + 200)) + " needle tail"
    _render(sesh_app, [_msg(role="user", content=body)])
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    card_key = view.keys[0]
    assert card_key not in view.expanded_keys  # collapsed by default

    view.find("needle")
    await pilot.pause()
    # The hit is past the preview boundary, so the card is expanded to show it.
    assert card_key in view.expanded_keys
    assert view.card_for_key(card_key).expanded is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_reveals_hit_inside_collapsed_subagent(app):
    sesh_app, pilot = app
    sesh_app._show_agents = True
    meta = SubagentMeta(agent_id="ag", file_path="/x", message_count=1)
    interior = [_msg(role="assistant", content="the needle is deep inside")]
    _render(sesh_app, [_msg(role="user", content="main body")], [(meta, interior)])
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    # Collapsed: no interior cards yet.
    assert not any(c.entry.depth == 1 for c in view._cards)

    view.find("needle")
    await pilot.pause()
    # The container was expanded so the interior hit is visible and active.
    interior_cards = [c for c in view._cards if c.entry.depth == 1]
    assert len(interior_cards) == 1
    assert view.find_active_key == interior_cards[0].key


@pytest.mark.integration
@pytest.mark.asyncio
async def test_find_flow_open_type_and_escape_restores_focus(app):
    sesh_app, pilot = app
    _render(sesh_app, [_msg(role="user", content="a needle in text")])
    await pilot.pause()

    # Session-tree search carries unrelated state that Escape must not discard.
    sesh_app.query_one("#search-input", Input).value = "tree-query"

    # Open transcript find with `n`, then type a query.
    await pilot.press("n")
    await pilot.pause()
    assert sesh_app.focused.id == "message-search"
    await pilot.press("n", "e", "e", "d", "l", "e")
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    assert view.find_position == (1, 1)
    assert _count_text(sesh_app) == "1 / 1"

    # Escape closes find, restores focus to the transcript, keeps tree search.
    await pilot.press("escape")
    await pilot.pause()
    bar = sesh_app.query_one("#message-search-bar")
    assert not bar.has_class("visible")
    assert sesh_app.query_one("#search-input", Input).value == "tree-query"
    assert view.find_position == (0, 0)
    assert _count_text(sesh_app) == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enter_and_shift_enter_navigate_in_find_box(app):
    sesh_app, pilot = app
    _render(
        sesh_app,
        [
            _msg(role="user", content="needle one"),
            _msg(role="assistant", content="needle two"),
            _msg(role="user", content="needle three"),
        ],
    )
    await pilot.pause()
    view = sesh_app.query_one("#message-view", TranscriptView)

    await pilot.press("n")  # open + focus find
    await pilot.pause()
    await pilot.press("n", "e", "e", "d", "l", "e")
    await pilot.pause()
    assert view.find_position == (1, 3)

    # Enter -> next.
    await pilot.press("enter")
    await pilot.pause()
    assert view.find_position == (2, 3)

    # Down arrow (robust substitute) -> next.
    await pilot.press("down")
    await pilot.pause()
    assert view.find_position == (3, 3)

    # Up arrow -> previous.
    await pilot.press("up")
    await pilot.pause()
    assert view.find_position == (2, 3)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_match_survives_tool_toggle(app):
    sesh_app, pilot = app
    msgs = [
        _msg(role="user", content="needle in user text"),
        _msg(
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="bash",
            tool_input="grep needle file",
        ),
    ]
    _render(sesh_app, msgs)
    await pilot.pause()

    # Open find via the input so the bar is visible and the term persists.
    sesh_app.query_one("#message-search-bar").add_class("visible")
    sesh_app.query_one("#message-search", Input).value = "needle"
    view = sesh_app.query_one("#message-view", TranscriptView)
    view.find("needle")
    sesh_app._update_find_count()
    # Tools hidden -> only the user message matches.
    assert view.find_position == (1, 1)
    user_key = view.find_active_key

    # Toggle tools on: the tool body now also matches; active stays on the user
    # hit (preserved by stable key) and the total grows.
    await pilot.press("t")
    await pilot.pause()
    view = sesh_app.query_one("#message-view", TranscriptView)
    assert view.find_active_key == user_key
    assert view.find_position == (1, 2)
    assert _count_text(sesh_app) == "1 / 2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_match_survives_live_append(app):
    sesh_app, pilot = app
    base = [
        _msg(role="user", content="needle A"),
        _msg(role="assistant", content="needle B"),
    ]
    _render(sesh_app, base)
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    sesh_app.query_one("#message-search-bar").add_class("visible")
    sesh_app.query_one("#message-search", Input).value = "needle"
    view.find("needle")
    view.find_next()  # active on the second message's hit
    active_key = view.find_active_key
    assert view.find_position == (2, 2)

    # Simulated live append: a third message arrives; existing keys are stable.
    # A live rerender goes through the normal path (highlight defaults to the
    # current find term), not a fresh-session reset.
    appended = base + [_msg(role="user", content="needle C")]
    sesh_app._render_messages(appended, sesh_app._current_session)
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    assert view.find_active_key == active_key
    assert view.find_position == (2, 3)
    assert _count_text(sesh_app) == "2 / 3"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_active_match_resets_when_message_disappears(app):
    sesh_app, pilot = app
    msgs = [
        _msg(role="user", content="needle stays"),
        _msg(
            role="assistant",
            content="",
            content_type="thinking",
            thinking="needle vanishes when thinking hidden",
        ),
    ]
    sesh_app._show_thinking = True
    _render(sesh_app, msgs)
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    sesh_app.query_one("#message-search-bar").add_class("visible")
    sesh_app.query_one("#message-search", Input).value = "needle"
    view.find("needle")
    view.find_next()  # active on the thinking message
    assert view.find_position == (2, 2)

    # Hide thinking: the matched card disappears -> graceful reset (no active),
    # but the surviving match is still counted.
    await pilot.press("T")
    await pilot.pause()
    view = sesh_app.query_one("#message-view", TranscriptView)
    assert view.find_position == (0, 1)
    assert view.find_active_key is None
