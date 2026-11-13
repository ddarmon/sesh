"""Textual pilot tests for marking sessions (Space, V brush, *) and bulk actions.

Timeline view lines: a0 a1 a2 (project /alpha), b0 b1 (project /beta).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from sesh.app import ConfirmDeleteScreen, SeshApp, SessionTree
from sesh.models import Project, Provider
from tests.helpers import make_session

T0 = datetime(2026, 1, 10, tzinfo=timezone.utc)


@pytest_asyncio.fixture()
async def app(monkeypatch):
    """Yield a headless SeshApp in timeline view; deleting a1 fails."""
    monkeypatch.setattr("sesh.app.load_bookmarks", lambda: set())
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda _: None)
    monkeypatch.setattr("sesh.viewcache.remove_view", lambda _id: None)
    deleted = []

    class FakeClaude:
        def delete_session(self, session):
            if session.id == "a1":
                raise OSError("boom")
            deleted.append(session.id)

    monkeypatch.setattr("sesh.providers.claude.ClaudeProvider", FakeClaude)

    sesh_app = SeshApp()
    sesh_app._load_from_index = lambda: False
    sesh_app._discover_all = lambda: None
    sesh_app.sort_index = sesh_app.sort_options.index("timeline")

    ids = ["a0", "a1", "a2", "b0", "b1"]
    sessions = [
        make_session(
            id=sid,
            project_path=f"/{'alpha' if sid[0] == 'a' else 'beta'}",
            summary=sid,
            timestamp=T0 - timedelta(hours=i),
        )
        for i, sid in enumerate(ids)
    ]
    async with sesh_app.run_test() as pilot:
        for path in ("/alpha", "/beta"):
            sesh_app.sessions[path] = [s for s in sessions if s.project_path == path]
            sesh_app.projects[path] = Project(
                path=path,
                display_name=path[1:],
                providers={Provider.CLAUDE},
                session_count=0,
            )
        sesh_app._populate_tree()
        tree = sesh_app.query_one("#session-tree", SessionTree)
        tree.focus()
        tree.cursor_line = 0
        await pilot.pause()
        yield sesh_app, pilot, deleted


def _marked(*ids):
    return {("claude", sid) for sid in ids}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_space_marks_and_moves_down(app):
    """Space marks the session under the cursor and moves down."""
    sesh_app, pilot, _ = app
    await pilot.press("space", "space")
    assert sesh_app._selected == _marked("a0", "a1")
    assert sesh_app.query_one("#session-tree", SessionTree).cursor_line == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_brush_paints_gaps_jumps_and_unmarks(app):
    """The brush leaves gaps between strokes, paints across jumps, and unmarks from a marked row."""
    sesh_app, pilot, _ = app
    indicator = sesh_app.query_one("#selection-indicator")

    await pilot.press("V", "down", "V", "down", "down", "V")
    assert sesh_app._selected == _marked("a0", "a1", "b0")
    assert str(indicator.render()) == "Sel:3 BRUSH+"

    await pilot.press("escape", "up", "V", "home")
    assert sesh_app._selected == _marked("a0", "a1", "a2", "b0")

    await pilot.press("escape", "V", "down")
    assert sesh_app._selected == _marked("a2", "b0")
    assert str(indicator.render()) == "Sel:2 BRUSH-"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_asterisk_inverts_and_escape_clears(app):
    """* inverts every mark; Escape stops the brush before it clears the marks."""
    sesh_app, pilot, _ = app
    await pilot.press("space", "asterisk")
    assert sesh_app._selected == _marked("a1", "a2", "b0", "b1")

    await pilot.press("V", "escape")
    assert sesh_app._brush is None
    assert sesh_app._selected == _marked("a2", "b0", "b1")
    await pilot.press("escape")
    assert sesh_app._selected == set()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_delete_survives_one_failure(app):
    """Bulk delete lists the marks, keeps going past a failing session, and clears the marks."""
    sesh_app, pilot, deleted = app
    await pilot.press("V", "down", "down", "V", "d")
    await pilot.pause()
    assert isinstance(sesh_app.screen, ConfirmDeleteScreen)
    assert sesh_app.screen.summaries == ["a0", "a1", "a2"]

    await pilot.click("#confirm-yes")
    await pilot.pause()
    assert deleted == ["a0", "a2"]
    assert sesh_app._selected == set()
    assert sesh_app._status_base == "2 sessions deleted; 1 failed"
