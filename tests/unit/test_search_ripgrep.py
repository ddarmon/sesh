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
    monkeypatch.setattr(
        search,
        "_decode_cursor_projects_path",
        lambda encoded, **_: f"/decoded/{encoded}",
    )

    results = search._search_cursor_transcripts("rg", "needle", base, None)
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

    results = search._search_cursor_stores(
        "needletoken", tmp_search_dirs["cursor_chats"], None,
    )
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
        lambda rg, q, cursor_projects, host: [
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
        lambda q, cursor_chats, host: [
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
        lambda rg, q, cursor_projects, host: [
            SearchResult(
                session_id="cursor-1",
                project_path="/Users/me/repo",
                provider=Provider.CURSOR,
                matched_line="needle",
                file_path="/tmp/x.txt",
            )
        ],
    )
    monkeypatch.setattr(
        search,
        "_search_cursor_stores",
        lambda q, cursor_chats, host: [],
    )
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


def test_early_dedup_skips_cwd_lookup_for_duplicates(
    tmp_search_dirs, monkeypatch,
) -> None:
    """When the same session appears twice in rg output, the second match
    skips cwd file I/O entirely (dedup fires before cwd resolution)."""
    claude_file = tmp_search_dirs["claude_projects"] / "proj" / "a.jsonl"
    write_jsonl(
        claude_file,
        [
            {"sessionId": "s1", "message": {"content": "needle one"}},
            {"sessionId": "s1", "message": {"content": "needle two"}},
        ],
    )

    line1 = json.dumps({"sessionId": "s1", "message": {"content": "needle one"}})
    line2 = json.dumps({"sessionId": "s1", "message": {"content": "needle two"}})
    stdout = "\n".join([
        _rg_match(str(claude_file), line1),
        _rg_match(str(claude_file), line2),
    ])
    monkeypatch.setattr(search.shutil, "which", lambda _: "rg")
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout))
    monkeypatch.setattr(
        search, "_search_cursor_transcripts", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        search, "_search_cursor_stores", lambda *a, **k: [],
    )

    results = search.ripgrep_search("needle")
    assert len(results) == 1
    assert results[0].session_id == "s1"


def test_cwd_lookup_skips_file_io(tmp_search_dirs, monkeypatch) -> None:
    """When cwd_lookup provides the project path, no file I/O is needed."""
    codex_file = (
        tmp_search_dirs["codex_sessions"]
        / "123e4567-e89b-12d3-a456-426614174000.jsonl"
    )
    write_jsonl(codex_file, [
        {"type": "session_meta", "payload": {"id": "codex-1", "cwd": "/should/not/read"}},
        {"type": "event_msg", "payload": {"message": "needle"}},
    ])

    matched = json.dumps({"type": "event_msg", "payload": {"message": "needle"}})
    stdout = _rg_match(str(codex_file), matched)

    open_calls = []
    original_open = open

    def tracking_open(path, *a, **k):
        open_calls.append(str(path))
        return original_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", tracking_open)
    monkeypatch.setattr(search.shutil, "which", lambda _: "rg")
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout))
    monkeypatch.setattr(search, "_search_cursor_transcripts", lambda *a, **k: [])
    monkeypatch.setattr(search, "_search_cursor_stores", lambda *a, **k: [])

    lookup = {("123e4567-e89b-12d3-a456-426614174000", "codex"): "/from/index"}
    results = search.ripgrep_search("needle", cwd_lookup=lookup)

    assert len(results) == 1
    assert results[0].project_path == "/from/index"
    assert str(codex_file) not in open_calls


def test_cursor_store_like_filters_non_matching_blobs(tmp_search_dirs) -> None:
    """SQL LIKE pre-filters blobs so non-matching rows are not JSON-parsed in Python."""
    store_db = tmp_search_dirs["cursor_chats"] / "hash1" / "sess1" / "store.db"
    create_store_db(
        store_db,
        blobs=[
            {"content": "Workspace Path: /Users/me/repo\nmetadata"},
            {"role": "user", "content": "no match here"},
            {"role": "user", "content": "this has the needle token"},
        ],
    )

    results = search._search_cursor_stores(
        "needle", tmp_search_dirs["cursor_chats"], None,
    )
    assert len(results) == 1
    assert results[0].project_path == "/Users/me/repo"
    assert "needle" in results[0].matched_line
