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


def test_ripgrep_search_aggregation_finds_per_host(
    tmp_aggregation_search_dirs, tmp_search_dirs,
) -> None:
    """Aggregation mode scans every host subtree and tags each result with its host."""
    _require_rg()

    # Same project path on both hosts; the resulting matches must stay
    # separate and each must carry the right host.
    project_path = "/Users/me/agg"
    for host in ("laptop", "desktop"):
        host_dirs = tmp_aggregation_search_dirs[host]
        write_jsonl(
            host_dirs["claude_projects"] / "-Users-me-agg" / "a.jsonl",
            [
                {
                    "sessionId": f"claude-{host}",
                    "cwd": project_path,
                    "message": {
                        "role": "user",
                        "content": f"Needle token from {host}",
                    },
                }
            ],
        )

    # Salt the local-mode roots — they must NOT be scanned when an
    # aggregation_root is passed.
    write_jsonl(
        tmp_search_dirs["claude_projects"] / "-Users-me-local" / "leak.jsonl",
        [
            {
                "sessionId": "claude-local-leak",
                "cwd": "/Users/me/local",
                "message": {"role": "user", "content": "Needle token from LOCAL"},
            }
        ],
    )

    results = search.ripgrep_search(
        "needle token",
        aggregation_root=tmp_aggregation_search_dirs["root"],
    )

    hosts = sorted({r.host for r in results if r.host is not None})
    assert hosts == ["desktop", "laptop"]

    session_ids = {r.session_id for r in results}
    assert "claude-laptop" in session_ids
    assert "claude-desktop" in session_ids
    assert "claude-local-leak" not in session_ids


def test_ripgrep_search_aggregation_cursor(tmp_aggregation_search_dirs) -> None:
    """Cursor transcripts + store.db search both work in aggregation mode."""
    _require_rg()

    laptop = tmp_aggregation_search_dirs["laptop"]
    desktop = tmp_aggregation_search_dirs["desktop"]

    # Cursor transcript on laptop; the decoded project path won't exist
    # on the aggregator's filesystem but must still be returned (the
    # validate_locally probe is suppressed in aggregation mode).
    transcript = (
        laptop["cursor_projects"]
        / "Users-laptop-only-cursor"
        / "agent-transcripts"
        / "cursor-laptop.txt"
    )
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "user:\nNeedle token in laptop transcript\nassistant:\nok\n"
    )

    # Cursor store.db on desktop with an embedded Workspace Path.
    desktop_project = "/Users/me/desktop-cursor"
    md5 = hashlib.md5(desktop_project.encode()).hexdigest()
    create_store_db(
        desktop["cursor_chats"] / md5 / "store-desktop-1" / "store.db",
        blobs=[
            {"content": f"Workspace Path: {desktop_project}\n"},
            {"role": "user", "content": "Needle token in desktop store"},
        ],
    )

    results = search.ripgrep_search(
        "needle token",
        aggregation_root=tmp_aggregation_search_dirs["root"],
    )

    transcript_hits = [
        r for r in results
        if r.session_id == "cursor-laptop" and r.provider is Provider.CURSOR
    ]
    assert len(transcript_hits) == 1
    assert transcript_hits[0].host == "laptop"
    assert transcript_hits[0].project_path == "/Users/laptop/only/cursor"

    store_hits = [
        r for r in results
        if r.session_id == "store-desktop-1" and r.provider is Provider.CURSOR
    ]
    assert len(store_hits) == 1
    assert store_hits[0].host == "desktop"
    assert store_hits[0].project_path == desktop_project


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


def test_parallel_host_search_returns_all_hosts(
    tmp_aggregation_search_dirs,
) -> None:
    """Aggregation mode searches multiple hosts in parallel and returns results from all."""
    _require_rg()

    for host in ("laptop", "desktop"):
        host_dirs = tmp_aggregation_search_dirs[host]
        write_jsonl(
            host_dirs["claude_projects"] / "-Users-me-proj" / "a.jsonl",
            [
                {
                    "sessionId": f"s-{host}",
                    "cwd": "/Users/me/proj",
                    "message": {"role": "user", "content": f"parallel needle from {host}"},
                }
            ],
        )

    results = search.ripgrep_search(
        "parallel needle",
        aggregation_root=tmp_aggregation_search_dirs["root"],
    )

    hosts = {r.host for r in results}
    assert "laptop" in hosts
    assert "desktop" in hosts
    assert len(results) == 2
