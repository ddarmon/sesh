from __future__ import annotations

from datetime import datetime, timezone

from sesh import discovery
from sesh.models import Provider
from tests.helpers import make_session


def test_discover_all_merges_projects_and_sorts_sessions(monkeypatch) -> None:
    cache_obj = object()
    calls: list[tuple[str, str, object]] = []

    class FakeClaudeProvider:
        def discover_projects(self):
            yield "/repo", "repo"

        def get_sessions(self, project_path: str, cache=None):
            calls.append(("claude", project_path, cache))
            return [
                make_session(
                    id="c1",
                    project_path=project_path,
                    provider=Provider.CLAUDE,
                    timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                )
            ]

    class FakeCodexProvider:
        def __init__(self, cache=None):
            self.cache = cache

        def discover_projects(self):
            yield "/repo", "repo"
            yield "/repo2", "repo2"

        def get_sessions(self, project_path: str, cache=None):
            calls.append(("codex", project_path, cache))
            if project_path == "/repo":
                return [
                    make_session(
                        id="x2",
                        project_path=project_path,
                        provider=Provider.CODEX,
                        timestamp=datetime(2025, 1, 3, tzinfo=timezone.utc),
                    ),
                    make_session(
                        id="x1",
                        project_path=project_path,
                        provider=Provider.CODEX,
                        timestamp=datetime(2025, 1, 2, tzinfo=timezone.utc),
                    ),
                ]
            return []

    class FakeCursorProvider:
        def discover_projects(self):
            yield "/repo2", "repo2"

        def get_sessions(self, project_path: str, cache=None):
            calls.append(("cursor", project_path, cache))
            return [
                make_session(
                    id="u1",
                    project_path=project_path,
                    provider=Provider.CURSOR,
                    timestamp=datetime(2025, 1, 4, tzinfo=timezone.utc),
                )
            ]

    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.cursor as cursor_mod

    monkeypatch.setattr(claude_mod, "ClaudeProvider", FakeClaudeProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", FakeCodexProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", FakeCursorProvider)

    projects, sessions = discovery.discover_all(cache=cache_obj)

    assert set(projects) == {"/repo", "/repo2"}
    assert [s.id for s in sessions["/repo"]] == ["x2", "x1", "c1"]
    assert [s.id for s in sessions["/repo2"]] == ["u1"]
    assert projects["/repo"].session_count == 3
    assert projects["/repo"].providers == {Provider.CLAUDE, Provider.CODEX}
    assert projects["/repo"].latest_activity == datetime(2025, 1, 3, tzinfo=timezone.utc)
    assert projects["/repo2"].providers == {Provider.CURSOR}
    assert all(call[2] is cache_obj for call in calls)


def test_discover_all_ignores_provider_exceptions(monkeypatch) -> None:
    class GoodClaudeProvider:
        def discover_projects(self):
            yield "/repo", "repo"

        def get_sessions(self, project_path: str, cache=None):
            return [
                make_session(
                    id="c1",
                    project_path=project_path,
                    provider=Provider.CLAUDE,
                    timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                )
            ]

    class BadCodexProvider:
        def __init__(self, cache=None):
            pass

        def discover_projects(self):
            raise RuntimeError("boom")

        def get_sessions(self, project_path: str, cache=None):
            return []

    class BadCursorProvider:
        def discover_projects(self):
            yield "/cursor", "cursor"

        def get_sessions(self, project_path: str, cache=None):
            raise RuntimeError("boom")

    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.cursor as cursor_mod

    monkeypatch.setattr(claude_mod, "ClaudeProvider", GoodClaudeProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", BadCodexProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", BadCursorProvider)

    projects, sessions = discovery.discover_all()
    assert set(projects) == {"/repo", "/cursor"}
    assert set(sessions) == {"/repo"}
    assert projects["/repo"].session_count == 1
    assert projects["/cursor"].session_count == 0

