from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from sesh import search
from sesh.models import Provider, SearchResult
from tests.helpers import create_store_db, write_jsonl


def _rg_match(file_path: str, line_text: str) -> str:
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": file_path},
                "lines": {"text": line_text},
            },
        }
    )


def test_search_cursor_transcripts_parses_and_dedups(
    tmp_search_dirs, monkeypatch
) -> None:
    """Cursor transcript rg output is parsed into SearchResults, deduped by (session_id, file)."""
    base = tmp_search_dirs["cursor_projects"]
    base.mkdir(parents=True, exist_ok=True)
    file1 = base / "Users-me-repo" / "agent-transcripts" / "sess1.txt"
    file2 = base / "Users-me-repo" / "agent-transcripts" / "sess1.txt"
    stdout = "\n".join(
        [
            _rg_match(str(file1), "first needle line"),
            _rg_match(str(file2), "second needle line"),
        ]
    )
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout))
    monkeypatch.setattr(search, "_decode_cursor_projects_path", lambda encoded: f"/decoded/{encoded}")

    results = search._search_cursor_transcripts("rg", "needle")
    assert len(results) == 1
    assert results[0].provider is Provider.CURSOR
    assert results[0].session_id == "sess1"
    assert results[0].project_path == "/decoded/Users-me-repo"


def test_search_cursor_stores_reads_sqlite(tmp_search_dirs) -> None:
    """Cursor store.db files are searched via SQLite (not rg) and matched case-insensitively."""
    store_db = tmp_search_dirs["cursor_chats"] / "hash1" / "sess1" / "store.db"
    create_store_db(
        store_db,
        blobs=[
            {"content": "Workspace Path: /Users/me/repo\nmeta"},
            {"role": "user", "content": "please find NeedleToken now"},
        ],
    )

    results = search._search_cursor_stores("needletoken")
    assert len(results) == 1
    result = results[0]
    assert result.provider is Provider.CURSOR
    assert result.session_id == "sess1"
    assert result.project_path == "/Users/me/repo"
    assert "NeedleToken" in result.matched_line


def test_ripgrep_search_parses_jsonl_and_merges_cursor_results(
    tmp_search_dirs, monkeypatch
) -> None:
    """Full ripgrep_search merges Claude JSONL, Codex JSONL, and Cursor results, deduping Cursor."""
    claude_file = tmp_search_dirs["claude_projects"] / "proj" / "a.jsonl"
    codex_file = (
        tmp_search_dirs["codex_sessions"]
        / "nested"
        / "codex-123e4567-e89b-12d3-a456-426614174000.jsonl"
    )

    write_jsonl(
        claude_file,
        [
            {
                "sessionId": "claude-1",
                "cwd": "/Users/me/repo",
                "message": {"content": "needle in claude"},
            }
        ],
    )
    write_jsonl(
        codex_file,
        [
            {
                "type": "session_meta",
                "payload": {"id": "codex-1", "cwd": "/Users/me/codex-repo"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "needle in codex"},
            },
        ],
    )

    matched_claude = json.dumps(
        {
            "sessionId": "claude-1",
            "cwd": "/Users/me/repo",
            "message": {"content": "needle in claude"},
        }
    )
    matched_codex = json.dumps(
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "needle in codex"},
        }
    )
    stdout = "\n".join(
        [
            _rg_match(str(claude_file), matched_claude),
            _rg_match(str(codex_file), matched_codex),
        ]
    )

    monkeypatch.setattr(search.shutil, "which", lambda _: "rg")
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout))
    monkeypatch.setattr(
        search,
        "_search_cursor_transcripts",
        lambda rg, q: [
            SearchResult(
                session_id="cursor-txt",
                project_path="/Users/me/cursor",
                provider=Provider.CURSOR,
                matched_line="needle txt",
                file_path="/tmp/one.txt",
            )
        ],
    )
    monkeypatch.setattr(
        search,
        "_search_cursor_stores",
        lambda q: [
            SearchResult(
                session_id="cursor-store",
                project_path="/Users/me/cursor",
                provider=Provider.CURSOR,
                matched_line="needle store",
                file_path="/tmp/store.db",
            ),
            SearchResult(
                session_id="cursor-txt",
                project_path="/Users/me/cursor",
                provider=Provider.CURSOR,
                matched_line="dupe",
                file_path="/tmp/store2.db",
            ),
        ],
    )

    results = search.ripgrep_search("needle")
    by_provider = {r.provider: [] for r in results}
    for r in results:
        by_provider.setdefault(r.provider, []).append(r)

    assert {r.session_id for r in by_provider[Provider.CLAUDE]} == {"claude-1"}
    codex_result = by_provider[Provider.CODEX][0]
    assert codex_result.session_id == "123e4567-e89b-12d3-a456-426614174000"
    assert codex_result.project_path == "/Users/me/codex-repo"
    assert {r.session_id for r in by_provider[Provider.CURSOR]} == {
        "cursor-txt",
        "cursor-store",
    }


def test_ripgrep_search_cursor_only_regression(tmp_search_dirs, monkeypatch) -> None:
    """Cursor search runs even when no Claude/Codex directories exist.

    Regression: ripgrep_search() previously returned early before reaching
    the Cursor search branches when JSONL directories were absent.
    """
    monkeypatch.setattr(search.shutil, "which", lambda _: "rg")
    monkeypatch.setattr(
        search,
        "_search_cursor_transcripts",
        lambda rg, q: [
            SearchResult(
                session_id="cursor-1",
                project_path="/Users/me/repo",
                provider=Provider.CURSOR,
                matched_line="needle",
                file_path="/tmp/x.txt",
            )
        ],
    )
    monkeypatch.setattr(search, "_search_cursor_stores", lambda q: [])
    monkeypatch.setattr(
        search.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("JSONL rg should not run")),
    )

    results = search.ripgrep_search("needle")
    assert len(results) == 1
    assert results[0].provider is Provider.CURSOR
    assert results[0].session_id == "cursor-1"


def test_ripgrep_search_returns_empty_when_rg_missing(monkeypatch) -> None:
    """When rg is not on PATH, ripgrep_search returns an empty list."""
    monkeypatch.setattr(search.shutil, "which", lambda _: None)
    assert search.ripgrep_search("needle") == []
