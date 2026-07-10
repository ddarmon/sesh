"""Textual pilot tests for the complete-reading transcript viewer.

Verifies the Phase 2 contract: long bodies show an omission marker, expansion
reveals the full tail, copy always yields the complete body, and expansion +
cursor state survive tool/thinking/agent toggles and rerenders.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from sesh.app import SeshApp, SessionTree
from sesh.models import Provider, SubagentMeta
from sesh.transcript_view import PREVIEW_CHARS, TranscriptView, omission_marker
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
    app._render_messages(messages, session)


LONG = "A" * (PREVIEW_CHARS + 500)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_long_message_shows_omission_marker_and_expands(app):
    sesh_app, pilot = app
    _render(sesh_app, [make_message(role="user", content=LONG)])
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    card = view._cards[0]
    # Collapsed: marker present, tail not shown.
    rendered = card.render()
    text = "".join(str(seg) for seg in rendered.renderables)
    assert omission_marker(500) in text
    assert card.full_body == LONG  # the complete body lives in the model

    # Expand via Enter on the focused transcript.
    view.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    assert card.key in view.expanded_keys
    assert card.expanded is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_short_message_has_no_marker(app):
    sesh_app, pilot = app
    _render(sesh_app, [make_message(role="user", content="short body")])
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    card = view._cards[0]
    assert card.expandable is False
    rendered = card.render()
    text = "".join(str(seg) for seg in rendered.renderables)
    assert "more character" not in text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_copy_returns_complete_body(app):
    sesh_app, pilot = app
    captured: list[str] = []
    sesh_app._copy_text = lambda t: captured.append(t)

    _render(sesh_app, [make_message(role="assistant", content=LONG)])
    await pilot.pause()

    sesh_app.action_copy_focused_message()
    assert captured == [LONG]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expansion_survives_tool_toggle(app):
    sesh_app, pilot = app
    msgs = [
        make_message(role="user", content=LONG),
        make_message(
            role="assistant", content="", content_type="tool_use",
            tool_name="bash", tool_input="pwd",
        ),
    ]
    _render(sesh_app, msgs)
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    user_key = view._cards[0].key
    view.reveal_key(user_key)
    view.toggle_active()  # expand the long user message
    await pilot.pause()
    assert user_key in view.expanded_keys

    # Toggle tools on -> recompose adds the tool card; user stays expanded.
    await pilot.press("t")
    await pilot.pause()
    view = sesh_app.query_one("#message-view", TranscriptView)
    assert user_key in view.expanded_keys
    assert view.card_for_key(user_key).expanded is True
    # The tool card is now present.
    assert any("call" in c.entry.header for c in view._cards)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_thinking_message_expands(app):
    sesh_app, pilot = app
    sesh_app._show_thinking = True
    think = make_message(
        role="assistant", content="", content_type="thinking", thinking=LONG,
    )
    _render(sesh_app, [think])
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    card = view._cards[0]
    assert card.expandable is True
    card_key = card.key
    view.reveal_key(card_key)
    view.toggle_active()
    await pilot.pause()
    assert card_key in view.expanded_keys


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subagent_block_expands_to_reveal_interior(app):
    sesh_app, pilot = app
    sesh_app._show_agents = True
    meta = SubagentMeta(
        agent_id="ag", file_path="/x", description="Do work",
        agent_type="fork", message_count=1,
    )
    interior = [make_message(role="assistant", content="agent interior body")]
    _render(sesh_app, [make_message(role="user", content="main")], [(meta, interior)])
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    agent_card = next(c for c in view._cards if c.is_agent)
    # Collapsed: interior not present.
    assert not any(c.entry.depth == 1 for c in view._cards)

    view.reveal_key(agent_card.key)
    view.toggle_active()
    await pilot.pause()
    view = sesh_app.query_one("#message-view", TranscriptView)
    interior_cards = [c for c in view._cards if c.entry.depth == 1]
    assert len(interior_cards) == 1
    assert interior_cards[0].full_body == "agent interior body"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reveal_key_expands_containing_agent(app):
    sesh_app, pilot = app
    sesh_app._show_agents = True
    meta = SubagentMeta(agent_id="ag", file_path="/x", message_count=1)
    interior = [make_message(role="assistant", content="hidden hit")]
    _render(sesh_app, [make_message(role="user", content="main")], [(meta, interior)])
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    # Find the interior key from the composed transcript.
    from sesh.transcript import compose_transcript
    items = compose_transcript(
        [make_message(role="user", content="main")], [(meta, interior)]
    )
    agent_item = next(i for i in items if i.kind == "agent")
    interior_key = agent_item.interior[0].key

    # Interior is hidden while the agent is collapsed; reveal_key must surface it.
    assert view.card_for_key(interior_key) is None
    assert view.reveal_key(interior_key) is True
    assert view.card_for_key(interior_key) is not None
    assert view.active_key == interior_key


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_session_renders_placeholder(app):
    sesh_app, pilot = app
    _render(sesh_app, [])
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    assert view._cards == []
    assert view.active_key is None
    assert view._placeholder is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hidden_only_session_shows_toggle_hint(app):
    sesh_app, pilot = app
    # A single tool message with tools hidden -> no visible cards, hint shown.
    tool = make_message(
        role="assistant", content="", content_type="tool_use",
        tool_name="bash", tool_input="pwd",
    )
    _render(sesh_app, [tool])
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    assert view._cards == []
    assert view._placeholder is not None
    assert "Press t" in view._empty_message
