from __future__ import annotations

from pathlib import Path

from sesh.models import Provider
from sesh.providers import gemini
from tests.helpers import make_session, write_gemini_session


def test_delete_session_removes_json_file(tmp_path: Path) -> None:
    file_path = tmp_path / "chats" / "session-2026-01-01T00-00-del1.json"
    write_gemini_session(file_path, session_id="del-me")

    session = make_session(
        id="del-me",
        provider=Provider.GEMINI,
        source_path=str(file_path),
    )
    gemini.GeminiProvider().delete_session(session)
    assert not file_path.exists()


def test_delete_session_missing_file_is_noop(tmp_path: Path) -> None:
    session = make_session(
        id="ghost",
        provider=Provider.GEMINI,
        source_path=str(tmp_path / "chats" / "session-gone.json"),
    )
    gemini.GeminiProvider().delete_session(session)  # must not raise


def test_delete_session_without_source_path_is_noop() -> None:
    session = make_session(id="nopath", provider=Provider.GEMINI, source_path=None)
    gemini.GeminiProvider().delete_session(session)  # must not raise


def test_delete_history_removes_session_entries_from_logs(tmp_path: Path) -> None:
    import json

    project_dir = tmp_path / "tmp" / "proj"
    file_path = project_dir / "chats" / "session-2026-01-01T00-00-del1.json"
    write_gemini_session(file_path, session_id="del-me")
    logs = project_dir / "logs.json"
    logs.write_text(json.dumps([
        {"sessionId": "del-me", "messageId": 0, "type": "user", "message": "a"},
        {"sessionId": "other", "messageId": 0, "type": "user", "message": "b"},
        {"sessionId": "del-me", "messageId": 1, "type": "user", "message": "c"},
    ], indent=2))
    session = make_session(id="del-me", provider=Provider.GEMINI, source_path=str(file_path))

    report = gemini.GeminiProvider().delete_history(session)

    assert report.supported
    assert report.removed == {str(logs): 2}
    assert [e["message"] for e in json.loads(logs.read_text())] == ["b"]


def test_delete_history_ignores_non_chats_layout(tmp_path: Path) -> None:
    session = make_session(
        id="x", provider=Provider.GEMINI, source_path=str(tmp_path / "elsewhere" / "s.json")
    )
    report = gemini.GeminiProvider().delete_history(session)
    assert report.supported
    assert report.removed == {}


def test_providers_without_session_keyed_history_are_unsupported() -> None:
    from sesh.providers.copilot import CopilotProvider
    from sesh.providers.opencode import OpencodeProvider

    for provider_cls, provider in (
        (CopilotProvider, Provider.COPILOT),
        (OpencodeProvider, Provider.OPENCODE),
    ):
        report = provider_cls().delete_history(make_session(id="x", provider=provider))
        assert report.supported is False
