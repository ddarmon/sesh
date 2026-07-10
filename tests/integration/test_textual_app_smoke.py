"""Textual pilot smoke tests for SeshApp.

These tests use Textual's headless ``run_test()`` driver to verify that
the TUI boots, renders expected widgets, and responds to key bindings
without requiring real provider data on disk.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from sesh import snapshots
from sesh.app import HelpScreen, SeshApp, SessionTree, SnapshotPreviewScreen, SnapshotsScreen
from sesh.models import Project, Provider, SearchResult, SubagentMeta
from tests.helpers import (
    make_message,
    make_session,
    make_snapshot,
    make_snapshot_resume,
    make_snapshot_tab,
)


@pytest_asyncio.fixture()
async def app(monkeypatch):
    """Yield a headless SeshApp with all I/O patched out."""
    monkeypatch.setattr("sesh.app.load_bookmarks", lambda: set())
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda _: None)

    app = SeshApp()
    # Prevent real discovery / index loading during on_mount.
    app._load_from_index = lambda: False
    app._discover_all = lambda: None

    async with app.run_test() as pilot:
        # Focus the tree so key bindings route to app actions, not the Input.
        app.query_one("#session-tree", SessionTree).focus()
        await pilot.pause()
        yield app, pilot


@pytest.mark.integration
@pytest.mark.asyncio
async def test_app_mounts_expected_widgets(app):
    """The app should render all core layout widgets on startup."""
    sesh_app, _pilot = app

    sesh_app.query_one("#session-tree")
    sesh_app.query_one("#message-view")
    sesh_app.query_one("#status-bar")
    sesh_app.query_one("#search-input")
    sesh_app.query_one("#provider-filter")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_filter_cycles_on_f(app):
    """Pressing 'f' cycles through provider filters: All -> Claude -> Codex -> Cursor -> Copilot -> pi -> Gemini -> opencode."""
    sesh_app, pilot = app

    assert sesh_app.current_filter is None

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.CLAUDE

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.CODEX

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.CURSOR

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.COPILOT

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.PI

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.GEMINI

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.OPENCODE

    await pilot.press("f")
    assert sesh_app.current_filter is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sort_cycles_on_s(app):
    """Pressing 's' cycles through sort modes: date -> name -> messages -> tokens -> timeline."""
    sesh_app, pilot = app

    assert sesh_app.sort_options[sesh_app.sort_index] == "date"

    await pilot.press("s")
    assert sesh_app.sort_options[sesh_app.sort_index] == "name"

    await pilot.press("s")
    assert sesh_app.sort_options[sesh_app.sort_index] == "messages"

    await pilot.press("s")
    assert sesh_app.sort_options[sesh_app.sort_index] == "tokens"

    await pilot.press("s")
    assert sesh_app.sort_options[sesh_app.sort_index] == "timeline"

    await pilot.press("s")
    assert sesh_app.sort_options[sesh_app.sort_index] == "date"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_input_focuses_on_slash(app):
    """Pressing '/' should focus the search input."""
    sesh_app, pilot = app

    await pilot.press("slash")
    assert sesh_app.query_one("#search-input").has_focus


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tree_populates_with_injected_sessions(app):
    """Injecting project/session data and calling _populate_tree fills the tree widget."""
    sesh_app, _pilot = app

    session = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    sesh_app.projects = {
        "/repo": Project(
            path="/repo",
            display_name="my-repo",
            providers={Provider.CLAUDE},
            session_count=1,
        )
    }
    sesh_app.sessions = {"/repo": [session]}
    sesh_app._populate_tree()

    tree = sesh_app.query_one("#session-tree")
    project_nodes = list(tree.root.children)
    assert len(project_nodes) == 1

    project_node = project_nodes[0]
    assert isinstance(project_node.data, Project)
    assert project_node.data.path == "/repo"

    session_nodes = list(project_node.children)
    assert len(session_nodes) == 1
    assert getattr(session_nodes[0].data, "id", None) == "s1"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_bar_shows_session_counts(app):
    """After populating sessions, the status bar should show project and session counts."""
    sesh_app, _pilot = app

    s1 = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    s2 = make_session(id="s2", provider=Provider.CODEX, project_path="/repo")
    sesh_app.projects = {
        "/repo": Project(
            path="/repo",
            display_name="my-repo",
            providers={Provider.CLAUDE, Provider.CODEX},
            session_count=2,
        )
    }
    sesh_app.sessions = {"/repo": [s1, s2]}
    sesh_app._populate_tree()

    assert "1 projects" in sesh_app._status_base
    assert "2 sessions" in sesh_app._status_base


@pytest.mark.integration
@pytest.mark.asyncio
async def test_escape_clears_search(app):
    """Pressing Escape after typing in search should clear the input."""
    sesh_app, pilot = app

    # Focus search via the slash binding
    await pilot.press("slash")
    search_input = sesh_app.query_one("#search-input")
    assert search_input.has_focus

    # Type into the focused search input
    await pilot.press("a", "b", "c")
    assert search_input.value == "abc"

    await pilot.press("escape")
    assert search_input.value == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_toggle_updates_state(app):
    """Pressing 't' toggles _show_tools, 'T' toggles _show_thinking."""
    sesh_app, pilot = app

    assert sesh_app._show_tools is False
    assert sesh_app._show_thinking is False

    await pilot.press("t")
    assert sesh_app._show_tools is True

    await pilot.press("t")
    assert sesh_app._show_tools is False

    await pilot.press("T")
    assert sesh_app._show_thinking is True

    await pilot.press("T")
    assert sesh_app._show_thinking is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agents_toggle_updates_state(app):
    """Pressing 'a' toggles _show_agents and shows Agents:ON in the status suffix."""
    sesh_app, pilot = app

    assert sesh_app._show_agents is False

    await pilot.press("a")
    assert sesh_app._show_agents is True
    assert "Agents:ON" in sesh_app._format_status_suffix()

    await pilot.press("a")
    assert sesh_app._show_agents is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_row_marks_agent_hits(app):
    """Search results with agent_id set get a ⑂ marker in their row label."""
    sesh_app, _pilot = app

    normal = SearchResult(
        session_id="s1",
        project_path="/repo",
        provider=Provider.CLAUDE,
        matched_line="plain hit",
        file_path="/tmp/.claude/projects/-repo/s1.jsonl",
    )
    agent_hit = SearchResult(
        session_id="s2",
        project_path="/repo",
        provider=Provider.CLAUDE,
        matched_line="agent hit",
        file_path="/tmp/.claude/projects/-repo/s2/subagents/agent-x.jsonl",
        agent_id="x",
    )
    sesh_app._show_search_results([normal, agent_hit], "hit")

    tree = sesh_app.query_one("#session-tree")
    leaves = list(tree.root.children[0].children)
    labels = [str(leaf.label) for leaf in leaves]
    assert not any("⑂" in lb for lb in labels if "plain hit" in lb)
    assert any("⑂" in lb for lb in labels if "agent hit" in lb)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_toggle_agents_renders_on_empty_main_thread(app):
    """[finding 5] Pressing 'a' reveals sub-agent threads even when the main
    thread parsed to zero messages (was a visual no-op before)."""
    from sesh.transcript_view import TranscriptView

    sesh_app, pilot = app

    session = make_session(id="empty-main", provider=Provider.CLAUDE)
    sesh_app._current_session = session
    sesh_app._current_messages = []  # no parseable main messages
    meta = SubagentMeta(agent_id="ag", file_path="/x", message_count=1)
    sesh_app._current_subagents = [(meta, [make_message(content="interior")])]
    sesh_app._subagents_loaded = True  # already loaded
    sesh_app._show_agents = False

    await pilot.press("a")
    await pilot.pause()

    assert sesh_app._show_agents is True
    view = sesh_app.query_one("#message-view", TranscriptView)
    assert any(card.is_agent for card in view._cards), (
        "sub-agent thread should render despite an empty main thread"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_toggle_agents_clears_auto_override(app):
    """[review] After a ⑂ auto-show override, 'a' must still be able to hide
    the threads: the first press clears the override (ON), the second hides."""
    sesh_app, pilot = app

    session = make_session(id="ovr", provider=Provider.CLAUDE)
    sesh_app._current_session = session
    sesh_app._current_messages = [make_message(content="main")]
    meta = SubagentMeta(agent_id="ag", file_path="/x", message_count=1)
    sesh_app._current_subagents = [(meta, [make_message(content="interior")])]
    sesh_app._subagents_loaded = True
    sesh_app._show_agents = False
    sesh_app._agents_override = True  # as set by opening a ⑂ search hit
    assert sesh_app._agents_visible is True

    await pilot.press("a")
    await pilot.pause()
    assert sesh_app._show_agents is True
    assert sesh_app._agents_override is False

    await pilot.press("a")
    await pilot.pause()
    assert sesh_app._show_agents is False
    assert sesh_app._agents_visible is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_toggle_tools_rerenders_agents_only_session(app):
    """[review] 't'/'T' re-render a session whose main thread is empty but
    which has visible sub-agent threads (interior honors the toggles)."""
    from sesh.transcript_view import TranscriptView

    sesh_app, pilot = app

    session = make_session(id="agents-only", provider=Provider.CLAUDE)
    sesh_app._current_session = session
    sesh_app._current_messages = []
    # Interior has a text message plus a tool call so the 't' toggle changes the
    # interior card set when the container is expanded.
    interior = [
        make_message(content="interior"),
        make_message(
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="bash",
            tool_input="pwd",
        ),
    ]
    meta = SubagentMeta(agent_id="ag", file_path="/x", message_count=2)
    sesh_app._current_subagents = [(meta, interior)]
    sesh_app._subagents_loaded = True
    sesh_app._show_agents = True
    sesh_app._render_messages([], session)
    await pilot.pause()

    view = sesh_app.query_one("#message-view", TranscriptView)
    agent_key = view._cards[0].key
    view.reveal_key(agent_key)  # expand the agent container
    # Ensure the agent is expanded so interior cards are visible.
    if agent_key not in view.expanded_keys:
        view.toggle_active()
    await pilot.pause()
    tool_cards_before = sum(1 for c in view._cards if "call" in c.entry.header)
    assert tool_cards_before == 0, "tool interior hidden while tools are off"

    await pilot.press("t")
    await pilot.pause()
    view = sesh_app.query_one("#message-view", TranscriptView)
    tool_cards_after = sum(1 for c in view._cards if "call" in c.entry.header)
    assert tool_cards_after == 1, "toggling tools reveals the interior tool card"

    await pilot.press("T")
    await pilot.pause()
    assert any(card.is_agent for card in view._cards), (
        "toggling thinking should re-render the spliced agent threads"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_load_messages_defers_subagents_when_hidden(app):
    """[finding 7/10] Sub-agent files are not read on select when agents are
    hidden; the main thread still loads."""
    import types

    sesh_app, _pilot = app
    # Run the render callback inline so we can drive the worker body directly.
    sesh_app.call_from_thread = lambda fn, *a: fn(*a)

    loads: list[object] = []
    sesh_app._load_subagents = lambda session: loads.append(session)
    main_calls: list[object] = []
    sesh_app._provider_for = lambda s: types.SimpleNamespace(
        get_messages=lambda sess: main_calls.append(sess) or [make_message(content="hi")]
    )

    session = make_session(id="s-lazy", provider=Provider.CLAUDE, source_path="/p")

    sesh_app._show_agents = False
    sesh_app._agents_override = False
    sesh_app._load_messages(session)
    assert main_calls, "main thread must load immediately"
    assert loads == [], "sub-agent load must be deferred while agents are hidden"

    # With agents visible, the sub-agent load fires as part of selection.
    sesh_app._show_agents = True
    sesh_app._load_messages(session)
    assert loads == [session]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_export_includes_subagents_when_toggled_on(app):
    """With 'a' ON, the clipboard export includes sub-agent sections."""
    sesh_app, _pilot = app

    captured: list[str] = []
    sesh_app._copy_text = lambda text: captured.append(text)

    session = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    sesh_app._current_session = session
    sesh_app._current_messages = [make_message(content="main message")]
    meta = SubagentMeta(
        agent_id="ag1",
        file_path="/tmp/agent-ag1.jsonl",
        description="Build the thing",
        agent_type="fork",
        message_count=1,
    )
    sesh_app._current_subagents = [(meta, [make_message(content="agent interior")])]

    # Off: no sub-agent section.
    sesh_app._show_agents = False
    sesh_app.action_export_session()
    assert "Sub-agent:" not in captured[-1]

    # On: sub-agent section present.
    sesh_app._show_agents = True
    sesh_app.action_export_session()
    assert "Sub-agent: Build the thing (ag1)" in captured[-1]
    assert "agent interior" in captured[-1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_browser_snapshot_uses_normalized_provider_and_visibility(app):
    """Browser views reload any provider and honor the TUI visibility flags."""
    from types import SimpleNamespace

    sesh_app, _pilot = app
    session = make_session(provider=Provider.PI)
    visible = make_message(role="user", content="visible")
    tool = make_message(
        role="assistant",
        content="",
        content_type="tool_use",
        tool_name="bash",
        tool_input="pwd",
    )
    sesh_app._provider_for = lambda _session: SimpleNamespace(
        get_messages=lambda _session: [visible, tool]
    )

    sesh_app._show_tools = False
    _session, messages, subagents = sesh_app._browser_snapshot(session)
    assert messages == [visible]
    assert subagents is None

    sesh_app._show_tools = True
    _session, messages, _subagents = sesh_app._browser_snapshot(session)
    assert messages == [visible, tool]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fullscreen_toggle_updates_state_and_class(app):
    """Pressing 'F' toggles fullscreen state and the main container class."""
    sesh_app, pilot = app
    main = sesh_app.query_one("#main")

    assert sesh_app._fullscreen is False
    assert not main.has_class("fullscreen")

    await pilot.press("F")
    await pilot.pause()
    assert sesh_app._fullscreen is True
    assert main.has_class("fullscreen")

    await pilot.press("F")
    await pilot.pause()
    assert sesh_app._fullscreen is False
    assert not main.has_class("fullscreen")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fullscreen_focus_moves_to_message_view(app):
    """Entering fullscreen moves focus off the hidden tree to the message view."""
    sesh_app, pilot = app

    tree = sesh_app.query_one("#session-tree")
    assert tree.has_focus

    await pilot.press("F")
    await pilot.pause()

    assert sesh_app.query_one("#message-view").has_focus


@pytest.mark.integration
@pytest.mark.asyncio
async def test_help_screen_opens_on_question_mark(app):
    """Pressing '?' opens the help modal screen."""
    sesh_app, pilot = app

    await pilot.press("question_mark")
    await pilot.pause()

    assert any(isinstance(screen, HelpScreen) for screen in sesh_app.screen_stack)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_help_screen_dismisses_on_escape(app):
    """Pressing Escape while help is open dismisses the modal."""
    sesh_app, pilot = app

    await pilot.press("question_mark")
    await pilot.pause()
    assert any(isinstance(screen, HelpScreen) for screen in sesh_app.screen_stack)

    await pilot.press("escape")
    await pilot.pause()

    assert not any(isinstance(screen, HelpScreen) for screen in sesh_app.screen_stack)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_help_screen_dismisses_on_question_mark(app):
    """Pressing '?' again while help is open dismisses it instead of nesting modals."""
    sesh_app, pilot = app

    await pilot.press("question_mark")
    await pilot.pause()
    assert any(isinstance(screen, HelpScreen) for screen in sesh_app.screen_stack)

    await pilot.press("question_mark")
    await pilot.pause()

    assert not any(isinstance(screen, HelpScreen) for screen in sesh_app.screen_stack)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshots_screen_opens_with_bracketed_host(
    app, tmp_snapshots_dir, monkeypatch
):
    """The snapshots modal should render summary labels literally, not as markup."""
    sesh_app, pilot = app

    monkeypatch.setattr("sesh.snapshots.backend.get_backend", lambda: object())
    snap = make_snapshot(
        host="host-name.local",
        tabs=[make_snapshot_tab(resume=make_snapshot_resume())],
    )
    snapshots.save(snap)

    await pilot.press("S")
    await pilot.pause()

    assert any(isinstance(screen, SnapshotsScreen) for screen in sesh_app.screen_stack)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_preview_screen_renders_literal_bracketed_text(
    app, tmp_snapshots_dir
):
    """Preview rows and warnings should render literal bracketed text without crashing."""
    sesh_app, pilot = app

    snap = make_snapshot(
        host="different-host",
        tabs=[
            make_snapshot_tab(
                cwd="/tmp/proj",
                resume=make_snapshot_resume(
                    provider=Provider.COPILOT,
                    session_id="93f1d5c1-6794-4614-af5b-cccaa64f354b",
                    cmd_args=[
                        "copilot",
                        "--resume=93f1d5c1-6794-4614-af5b-cccaa64f354b",
                    ],
                ),
            ),
        ],
    )
    snapshots.save(snap)

    sesh_app.push_screen(SnapshotPreviewScreen(snap.id))
    await pilot.pause()

    assert any(
        isinstance(screen, SnapshotPreviewScreen) for screen in sesh_app.screen_stack
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bookmark_toggle_on_session_node(app):
    """Pressing 'b' on a session node toggles its bookmark status."""
    sesh_app, pilot = app

    session = make_session(id="bm1", provider=Provider.CLAUDE, project_path="/repo")
    sesh_app.projects = {
        "/repo": Project(
            path="/repo",
            display_name="my-repo",
            providers={Provider.CLAUDE},
            session_count=1,
        )
    }
    sesh_app.sessions = {"/repo": [session]}
    sesh_app._populate_tree()

    tree = sesh_app.query_one("#session-tree")
    tree.focus()
    await pilot.pause()

    # Navigate down to the project node, then into the session node
    await pilot.press("down")  # project node
    await pilot.press("down")  # session node (project is auto-expanded)
    await pilot.pause()

    # Verify we're on a session node
    assert tree.cursor_node is not None
    assert hasattr(tree.cursor_node.data, "id")
    assert tree.cursor_node.data.id == "bm1"

    await pilot.press("b")
    assert ("claude", "bm1") in sesh_app._bookmarks

    # Re-select a session node after tree repopulation, then toggle bookmark off.
    sesh_app._reselect_node(tree, ("claude", "bm1"))
    await pilot.pause()
    assert tree.cursor_node is not None
    assert getattr(tree.cursor_node.data, "id", None) == "bm1"

    await pilot.press("b")
    assert ("claude", "bm1") not in sesh_app._bookmarks


@pytest.mark.integration
@pytest.mark.asyncio
async def test_aggregation_startup_skips_local_index(monkeypatch, tmp_path):
    """Aggregation-mode startup must not read the local index or show local sessions.

    The on-disk index is owned by local mode; loading it during on_mount would
    briefly flash unrelated local sessions before the mirrored hosts discover.
    """
    monkeypatch.setattr("sesh.app.load_bookmarks", lambda: set())
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda _: None)

    # Spy on the index loader; a correct aggregation startup never calls it.
    calls: list[int] = []

    def _spy_load_index():
        calls.append(1)
        return {
            "projects": [
                {
                    "path": "/local/repo",
                    "display_name": "local-repo",
                    "providers": ["claude"],
                    "session_count": 1,
                }
            ],
            "sessions": [],
        }

    monkeypatch.setattr("sesh.cache.load_index", _spy_load_index)

    app = SeshApp(aggregation_root=tmp_path)
    # Keep real _load_from_index (under test); stub only background discovery.
    app._discover_all = lambda: None

    async with app.run_test():
        pass

    assert calls == [], "load_index must not be called during aggregation startup"
    assert app.projects == {}
    assert app.sessions == {}
