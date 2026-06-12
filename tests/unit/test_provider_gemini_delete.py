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
