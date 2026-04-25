from __future__ import annotations

import sys
from types import ModuleType
from pathlib import Path

import pytest


def _install_textual_stubs() -> None:
    if "textual" in sys.modules:
        return

    textual = ModuleType("textual")
    app_mod = ModuleType("textual.app")
    binding_mod = ModuleType("textual.binding")
    containers_mod = ModuleType("textual.containers")
    screen_mod = ModuleType("textual.screen")
    widgets_mod = ModuleType("textual.widgets")

    class _Base:
        def __init__(self, *args, **kwargs):
            pass

    class App(_Base):
        pass

    class ModalScreen(_Base):
        @classmethod
        def __class_getitem__(cls, item):
            return cls

    class Binding(_Base):
        pass

    class Horizontal(_Base):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class Vertical(Horizontal):
        pass

    class Tree(_Base):
        class NodeSelected:
            pass

    class RichLog(_Base):
        pass

    class Button(_Base):
        pass

    class Header(_Base):
        pass

    class Input(_Base):
        class Changed:
            pass

        class Submitted:
            pass

    class Label(_Base):
        pass

    class Static(_Base):
        pass

    class Checkbox(_Base):
        class Changed:
            pass

    class ListItem(_Base):
        pass

    class ListView(_Base):
        class Selected:
            pass

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.index = None

        def clear(self):
            pass

        def append(self, _item):
            pass

    app_mod.App = App
    app_mod.ComposeResult = list
    binding_mod.Binding = Binding
    containers_mod.Horizontal = Horizontal
    containers_mod.Vertical = Vertical
    screen_mod.ModalScreen = ModalScreen
    widgets_mod.Button = Button
    widgets_mod.Checkbox = Checkbox
    widgets_mod.Header = Header
    widgets_mod.Input = Input
    widgets_mod.Label = Label
    widgets_mod.ListItem = ListItem
    widgets_mod.ListView = ListView
    widgets_mod.RichLog = RichLog
    widgets_mod.Static = Static
    widgets_mod.Tree = Tree

    sys.modules["textual"] = textual
    sys.modules["textual.app"] = app_mod
    sys.modules["textual.binding"] = binding_mod
    sys.modules["textual.containers"] = containers_mod
    sys.modules["textual.screen"] = screen_mod
    sys.modules["textual.widgets"] = widgets_mod


try:
    import textual  # noqa: F401
except ModuleNotFoundError:
    _install_textual_stubs()


@pytest.fixture()
def tmp_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from sesh import bookmarks, cache, paths, preferences

    cache_dir = tmp_path / "cache" / "sesh"
    config_dir = tmp_path / "config" / "sesh"
    monkeypatch.setattr(paths, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(paths, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache, "CACHE_FILE", cache_dir / "sessions.json")
    monkeypatch.setattr(cache, "INDEX_FILE", cache_dir / "index.json")
    monkeypatch.setattr(cache, "PROJECT_PATHS_FILE", cache_dir / "project_paths.json")
    monkeypatch.setattr(bookmarks, "BOOKMARKS_FILE", config_dir / "bookmarks.json")
    monkeypatch.setattr(preferences, "PREFERENCES_FILE", config_dir / "preferences.json")
    return cache_dir


@pytest.fixture(autouse=True)
def isolate_app_preferences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep SeshApp tests independent of any real user preference file."""
    import sesh.app as app_mod

    default_prefs = {
        "provider_filter": None,
        "sort_mode": "date",
        "show_tools": False,
        "show_thinking": False,
        "fullscreen": False,
    }
    monkeypatch.setattr(app_mod, "load_preferences", lambda: dict(default_prefs))
    monkeypatch.setattr(app_mod, "save_preferences", lambda _prefs: None)


@pytest.fixture()
def tmp_claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from sesh.providers import claude

    claude_dir = tmp_path / ".claude"
    monkeypatch.setattr(claude, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(claude, "PROJECTS_DIR", claude_dir / "projects")
    monkeypatch.setattr(claude, "HISTORY_FILE", claude_dir / "history.jsonl")
    return claude_dir


@pytest.fixture()
def tmp_codex_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from sesh.providers import codex

    codex_dir = tmp_path / ".codex" / "sessions"
    monkeypatch.setattr(codex, "CODEX_DIR", codex_dir)
    return codex_dir


@pytest.fixture()
def tmp_copilot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from sesh.providers import copilot

    copilot_dir = tmp_path / ".copilot" / "session-state"
    monkeypatch.setattr(copilot, "COPILOT_DIR", copilot_dir)
    return copilot_dir


@pytest.fixture()
def tmp_cursor_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    from sesh.providers import cursor

    cursor_root = tmp_path / ".cursor"
    chats = cursor_root / "chats"
    projects = cursor_root / "projects"
    workspace_storage = tmp_path / "Cursor" / "User" / "workspaceStorage"

    monkeypatch.setattr(cursor, "CURSOR_CHATS_DIR", chats)
    monkeypatch.setattr(cursor, "CURSOR_PROJECTS_DIR", projects)
    monkeypatch.setattr(cursor, "WORKSPACE_STORAGE", workspace_storage)
    return {
        "cursor_root": cursor_root,
        "chats": chats,
        "projects": projects,
        "workspace_storage": workspace_storage,
    }


@pytest.fixture()
def tmp_search_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    from sesh import search

    claude_projects = tmp_path / ".claude" / "projects"
    codex_sessions = tmp_path / ".codex" / "sessions"
    cursor_projects = tmp_path / ".cursor" / "projects"
    cursor_chats = tmp_path / ".cursor" / "chats"

    monkeypatch.setattr(search, "CLAUDE_PROJECTS", claude_projects)
    monkeypatch.setattr(search, "CODEX_SESSIONS", codex_sessions)
    monkeypatch.setattr(search, "CURSOR_PROJECTS", cursor_projects)
    copilot_sessions = tmp_path / ".copilot" / "session-state"
    monkeypatch.setattr(search, "COPILOT_SESSIONS", copilot_sessions)
    monkeypatch.setattr(search, "CURSOR_CHATS", cursor_chats)
    return {
        "claude_projects": claude_projects,
        "codex_sessions": codex_sessions,
        "cursor_projects": cursor_projects,
        "cursor_chats": cursor_chats,
        "copilot_sessions": copilot_sessions,
    }


@pytest.fixture()
def tmp_snapshots_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect snapshot storage to a temp dir for tests."""
    from sesh import paths
    from sesh.snapshots import core as snapshots_core

    snap_dir = tmp_path / "data" / "sesh" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "SNAPSHOTS_DIR", snap_dir)
    monkeypatch.setattr(snapshots_core, "SNAPSHOTS_DIR", snap_dir)
    return snap_dir


@pytest.fixture()
def fake_backend(monkeypatch: pytest.MonkeyPatch):
    """Replace get_backend() with a controllable in-memory backend."""
    from sesh.snapshots import backend as backend_mod
    from sesh.snapshots.backend import RestoreOutcome

    class FakeBackend:
        name = "fake"

        def __init__(self) -> None:
            self.captured_tabs: list = []
            self.restore_outcome = RestoreOutcome(launched=0, fellback=False, note=None)
            self.restore_calls: list = []

        def is_supported(self) -> bool:
            return True

        def capture(self):
            return list(self.captured_tabs)

        def restore(self, items):
            self.restore_calls.append(list(items))
            outcome = self.restore_outcome
            return RestoreOutcome(
                launched=outcome.launched or len(items),
                fellback=outcome.fellback,
                note=outcome.note,
            )

    fake = FakeBackend()
    monkeypatch.setattr(backend_mod, "get_backend", lambda: fake)
    # core.py imported get_backend at import time; override there too.
    from sesh.snapshots import core as snapshots_core
    monkeypatch.setattr(snapshots_core, "get_backend", lambda: fake)
    return fake


@pytest.fixture()
def tmp_move_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    from sesh import move

    claude_projects = tmp_path / ".claude" / "projects"
    codex_sessions = tmp_path / ".codex" / "sessions"
    cursor_chats = tmp_path / ".cursor" / "chats"
    cursor_projects = tmp_path / ".cursor" / "projects"
    workspace_storage = tmp_path / "Cursor" / "User" / "workspaceStorage"

    monkeypatch.setattr(move, "PROJECTS_DIR", claude_projects)
    monkeypatch.setattr(move, "CODEX_DIR", codex_sessions)
    monkeypatch.setattr(move, "CURSOR_CHATS_DIR", cursor_chats)
    monkeypatch.setattr(move, "CURSOR_PROJECTS_DIR", cursor_projects)
    copilot_sessions = tmp_path / ".copilot" / "session-state"
    monkeypatch.setattr(move, "COPILOT_DIR", copilot_sessions)
    monkeypatch.setattr(move, "WORKSPACE_STORAGE", workspace_storage)

    cache_dir = tmp_path / "cache" / "sesh"
    monkeypatch.setattr(move, "CACHE_FILE", cache_dir / "sessions.json")
    monkeypatch.setattr(move, "INDEX_FILE", cache_dir / "index.json")
    monkeypatch.setattr(move, "PROJECT_PATHS_FILE", cache_dir / "project_paths.json")

    return {
        "claude_projects": claude_projects,
        "codex_sessions": codex_sessions,
        "cursor_chats": cursor_chats,
        "cursor_projects": cursor_projects,
        "copilot_sessions": copilot_sessions,
        "workspace_storage": workspace_storage,
        "cache_dir": cache_dir,
    }
