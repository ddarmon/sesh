from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from sesh import search
from sesh.models import Provider
from tests.helpers import create_store_db, write_jsonl


pytestmark = [pytest.mark.integration, pytest.mark.requires_rg]


def _require_rg() -> None:
    if shutil.which("rg") is None:
        pytest.skip("rg not found on PATH")


def test_ripgrep_search_finds_claude_jsonl(tmp_search_dirs) -> None:
    """Real rg binary finds a query term inside a Claude JSONL fixture."""
    _require_rg()
    project_path = "/Users/me/repo"
    claude_file = tmp_search_dirs["claude_projects"] / "-Users-me-repo" / "a.jsonl"
    write_jsonl(
        claude_file,
        [
            {
                "sessionId": "claude-1",
                "cwd": project_path,
                "message": {"role": "user", "content": "Needle token in Claude"},
            }
        ],
    )

    results = search.ripgrep_search("needle token")
    assert any(r.provider is Provider.CLAUDE and r.session_id == "claude-1" for r in results)


def test_ripgrep_search_finds_codex_jsonl(tmp_search_dirs) -> None:
    """Real rg binary finds a query term inside a Codex JSONL fixture."""
    _require_rg()
    codex_file = tmp_search_dirs["codex_sessions"] / "abc-123e4567-e89b-12d3-a456-426614174000.jsonl"
    write_jsonl(
        codex_file,
        [
            {
                "type": "session_meta",
                "timestamp": "2025-01-01T00:00:00Z",
                "payload": {"id": "codex-1", "cwd": "/Users/me/codex"},
            },
            {
                "type": "event_msg",
                "timestamp": "2025-01-01T00:00:01Z",
                "payload": {"type": "user_message", "message": "Needle token in Codex"},
            },
        ],
    )

    results = search.ripgrep_search("needle token")
    assert any(
        r.provider is Provider.CODEX and r.project_path == "/Users/me/codex"
        for r in results
    )


def test_cursor_transcript_search(tmp_search_dirs) -> None:
    """Real rg binary finds a query term inside a Cursor .txt transcript."""
    _require_rg()
    transcript = (
        tmp_search_dirs["cursor_projects"]
        / "Users-me-cursor"
        / "agent-transcripts"
        / "cursor-1.txt"
    )
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("user:\nNeedle token in Cursor transcript\nassistant:\nok\n")

    results = search.ripgrep_search("needle token")
    assert any(r.provider is Provider.CURSOR and r.session_id == "cursor-1" for r in results)


def test_cursor_store_db_search(tmp_search_dirs) -> None:
    """Cursor store.db search (SQLite-based, not rg) finds the query in blob content."""
    _require_rg()
    project_path = "/Users/me/cursor-store"
    md5 = hashlib.md5(project_path.encode()).hexdigest()
    store_db = tmp_search_dirs["cursor_chats"] / md5 / "store-sess-1" / "store.db"
    create_store_db(
        store_db,
        blobs=[
            {"content": f"Workspace Path: {project_path}\n"},
            {"role": "user", "content": "Needle token in Cursor store"},
        ],
    )

    results = search.ripgrep_search("needle token")
    assert any(
        r.provider is Provider.CURSOR
        and r.session_id == "store-sess-1"
        and r.project_path == project_path
        for r in results
    )
