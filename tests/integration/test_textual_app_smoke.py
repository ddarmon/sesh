"""Textual pilot smoke tests for SeshApp.

These tests use Textual's headless ``run_test()`` driver to verify that
the TUI boots, renders expected widgets, and responds to key bindings
without requiring real provider data on disk.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from sesh.app import SeshApp, SessionTree
from sesh.models import Project, Provider
from tests.helpers import make_session


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
    """Pressing 'f' cycles through provider filters: All -> Claude -> Codex -> Cursor."""
    sesh_app, pilot = app

    assert sesh_app.current_filter is None

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.CLAUDE

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.CODEX

    await pilot.press("f")
    assert sesh_app.current_filter == Provider.CURSOR

    await pilot.press("f")
    assert sesh_app.current_filter is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sort_cycles_on_s(app):
    """Pressing 's' cycles through sort modes: date -> name -> messages -> timeline."""
    sesh_app, pilot = app

    assert sesh_app.sort_options[sesh_app.sort_index] == "date"

    await pilot.press("s")
    assert sesh_app.sort_options[sesh_app.sort_index] == "name"

    await pilot.press("s")
    assert sesh_app.sort_options[sesh_app.sort_index] == "messages"

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
    assert len(list(tree.root.children)) >= 1


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
