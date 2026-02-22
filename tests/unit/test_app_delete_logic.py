from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

if sys.version_info < (3, 10):
    pytest.skip("app.py requires Python 3.10+ syntax at import time", allow_module_level=True)

from sesh.app import SeshApp
from sesh.models import Project, Provider
from tests.helpers import make_session


def _make_app_for_delete():
    app = SeshApp()
    calls = {"status": [], "populate": []}
    app.query_one = lambda *a, **k: SimpleNamespace(value="filter")
    app._set_status = lambda text: calls["status"].append(text)
    app._populate_tree = lambda **kwargs: calls["populate"].append(kwargs)
    return app, calls


def _patch_provider_delete(monkeypatch, provider: Provider, fn):
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.cursor as cursor_mod

    class Noop:
        def delete_session(self, session):
            return None

    class Impl:
        def delete_session(self, session):
            return fn(session)

    monkeypatch.setattr(claude_mod, "ClaudeProvider", Noop)
    monkeypatch.setattr(codex_mod, "CodexProvider", Noop)
    monkeypatch.setattr(cursor_mod, "CursorProvider", Noop)
    if provider == Provider.CLAUDE:
        monkeypatch.setattr(claude_mod, "ClaudeProvider", Impl)
    elif provider == Provider.CODEX:
        monkeypatch.setattr(codex_mod, "CodexProvider", Impl)
    elif provider == Provider.CURSOR:
        monkeypatch.setattr(cursor_mod, "CursorProvider", Impl)


def test_removes_session_from_memory(monkeypatch) -> None:
    app, calls = _make_app_for_delete()
    target = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    keep = make_session(id="s2", provider=Provider.CLAUDE, project_path="/repo")
    app.sessions = {"/repo": [target, keep]}
    app.projects = {
        "/repo": Project(
            path="/repo",
            display_name="repo",
            providers={Provider.CLAUDE},
            session_count=2,
            latest_activity=keep.timestamp,
        )
    }

    _patch_provider_delete(monkeypatch, Provider.CLAUDE, lambda s: None)
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda bookmarks: None)
    app._delete_session(target)

    assert [s.id for s in app.sessions["/repo"]] == ["s2"]
    assert calls["status"][-1] == "Session deleted"


def test_removes_bookmark_and_saves(monkeypatch) -> None:
    app, _calls = _make_app_for_delete()
    target = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    app.sessions = {"/repo": [target]}
    app.projects = {
        "/repo": Project(path="/repo", display_name="repo", providers={Provider.CLAUDE}, session_count=1)
    }
    app._bookmarks = {("claude", "s1"), ("codex", "x")}

    saved = []
    _patch_provider_delete(monkeypatch, Provider.CLAUDE, lambda s: None)
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda bookmarks: saved.append(set(bookmarks)))

    app._delete_session(target)

    assert ("claude", "s1") not in app._bookmarks
    assert ("codex", "x") in app._bookmarks
    assert saved and ("claude", "s1") not in saved[-1]


def test_updates_project_metadata(monkeypatch) -> None:
    app, _calls = _make_app_for_delete()
    target = make_session(
        id="old",
        provider=Provider.CLAUDE,
        project_path="/repo",
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    keep = make_session(
        id="new",
        provider=Provider.CODEX,
        project_path="/repo",
        timestamp=datetime(2025, 1, 3, tzinfo=timezone.utc),
    )
    app.sessions = {"/repo": [target, keep]}
    app.projects = {
        "/repo": Project(
            path="/repo",
            display_name="repo",
            providers={Provider.CLAUDE, Provider.CODEX},
            session_count=2,
            latest_activity=target.timestamp,
        )
    }

    _patch_provider_delete(monkeypatch, Provider.CLAUDE, lambda s: None)
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda bookmarks: None)

    app._delete_session(target)
    proj = app.projects["/repo"]
    assert proj.session_count == 1
    assert proj.providers == {Provider.CODEX}
    assert proj.latest_activity == keep.timestamp


def test_deletes_project_when_last_session(monkeypatch) -> None:
    app, _calls = _make_app_for_delete()
    target = make_session(id="s1", provider=Provider.CURSOR, project_path="/repo")
    app.sessions = {"/repo": [target]}
    app.projects = {
        "/repo": Project(path="/repo", display_name="repo", providers={Provider.CURSOR}, session_count=1)
    }

    _patch_provider_delete(monkeypatch, Provider.CURSOR, lambda s: None)
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda bookmarks: None)
    app._delete_session(target)

    assert "/repo" not in app.sessions
    assert "/repo" not in app.projects


def test_provider_exception_sets_error_status(monkeypatch) -> None:
    app, calls = _make_app_for_delete()
    target = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    app.sessions = {"/repo": [target]}
    app.projects = {
        "/repo": Project(path="/repo", display_name="repo", providers={Provider.CLAUDE}, session_count=1)
    }

    _patch_provider_delete(monkeypatch, Provider.CLAUDE, lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda bookmarks: None)
    app._delete_session(target)

    assert calls["status"][-1] == "Error deleting session"
    assert "/repo" in app.sessions


def test_id_collision_regression_only_removes_matching_provider(monkeypatch) -> None:
    app, _calls = _make_app_for_delete()
    claude_session = make_session(id="same", provider=Provider.CLAUDE, project_path="/repo")
    codex_session = make_session(id="same", provider=Provider.CODEX, project_path="/repo")
    app.sessions = {"/repo": [claude_session, codex_session]}
    app.projects = {
        "/repo": Project(
            path="/repo",
            display_name="repo",
            providers={Provider.CLAUDE, Provider.CODEX},
            session_count=2,
        )
    }

    deleted = []
    _patch_provider_delete(monkeypatch, Provider.CLAUDE, lambda s: deleted.append((s.provider, s.id)))
    monkeypatch.setattr("sesh.app.save_bookmarks", lambda bookmarks: None)

    app._delete_session(claude_session)

    assert deleted == [(Provider.CLAUDE, "same")]
    remaining = app.sessions["/repo"]
    assert len(remaining) == 1
    assert remaining[0].provider is Provider.CODEX
    assert remaining[0].id == "same"
