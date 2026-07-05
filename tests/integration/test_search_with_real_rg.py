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


def _agent_record(
    *,
    session_id: str | None,
    agent_id: str,
    text: str,
    cwd: str = "/Users/me/repo",
    parent_session_id: str | None = None,
) -> dict:
    """A realistic Claude sub-agent JSONL record carrying the parent sessionId."""
    rec: dict = {
        "agentId": agent_id,
        "isSidechain": True,
        "timestamp": "2026-01-01T00:00:00Z",
        "cwd": cwd,
        "message": {"role": "user", "content": text},
    }
    if session_id is not None:
        rec["sessionId"] = session_id
    if parent_session_id is not None:
        rec["parentSessionId"] = parent_session_id
    return rec


def test_ripgrep_search_agent_file_current_layout(tmp_search_dirs) -> None:
    """A hit only inside a current-layout agent file is attributed to the
    parent sessionId and tagged with the agent_id."""
    _require_rg()
    parent = "parent-sess-1"
    agent_file = (
        tmp_search_dirs["claude_projects"]
        / "-Users-me-repo"
        / parent
        / "subagents"
        / "agent-abc123.jsonl"
    )
    write_jsonl(
        agent_file,
        [_agent_record(session_id=parent, agent_id="abc123", text="Needle token in subagent")],
    )

    results = search.ripgrep_search("needle token")
    hits = [r for r in results if r.provider is Provider.CLAUDE and r.agent_id]
    assert len(hits) == 1
    assert hits[0].session_id == parent
    assert hits[0].agent_id == "abc123"
    assert hits[0].project_path == "/Users/me/repo"


def test_ripgrep_search_agent_file_legacy_layout(tmp_search_dirs) -> None:
    """Legacy {project}/subagents/agent-*.jsonl hits resolve via the record's
    own sessionId and still carry agent_id."""
    _require_rg()
    parent = "parent-sess-2"
    agent_file = (
        tmp_search_dirs["claude_projects"]
        / "-Users-me-repo"
        / "subagents"
        / "agent-leg99.jsonl"
    )
    write_jsonl(
        agent_file,
        [_agent_record(session_id=parent, agent_id="leg99", text="Needle token legacy subagent")],
    )

    results = search.ripgrep_search("needle token")
    hits = [r for r in results if r.provider is Provider.CLAUDE and r.agent_id]
    assert len(hits) == 1
    assert hits[0].session_id == parent
    assert hits[0].agent_id == "leg99"


def test_ripgrep_search_agent_file_fork_context_ref(tmp_search_dirs) -> None:
    """A matched line with no sessionId but a parentSessionId resolves the
    parent via parentSessionId."""
    _require_rg()
    agent_file = (
        tmp_search_dirs["claude_projects"]
        / "-Users-me-repo"
        / "some-dir"
        / "subagents"
        / "agent-fork1.jsonl"
    )
    write_jsonl(
        agent_file,
        [
            _agent_record(
                session_id=None,
                agent_id="fork1",
                text="Needle token in fork ref",
                parent_session_id="parent-fork",
            )
        ],
    )

    results = search.ripgrep_search("needle token")
    hits = [r for r in results if r.provider is Provider.CLAUDE and r.agent_id]
    assert len(hits) == 1
    assert hits[0].session_id == "parent-fork"
    assert hits[0].agent_id == "fork1"


def test_ripgrep_search_agent_file_parent_from_directory(tmp_search_dirs) -> None:
    """A matched line with neither sessionId nor parentSessionId derives the
    parent id from the current-layout grandparent directory name."""
    _require_rg()
    parent = "dir-derived-parent"
    agent_file = (
        tmp_search_dirs["claude_projects"]
        / "-Users-me-repo"
        / parent
        / "subagents"
        / "agent-nodir.jsonl"
    )
    write_jsonl(
        agent_file,
        [_agent_record(session_id=None, agent_id="nodir", text="Needle token no ids")],
    )

    results = search.ripgrep_search("needle token")
    hits = [r for r in results if r.provider is Provider.CLAUDE and r.agent_id]
    assert len(hits) == 1
    assert hits[0].session_id == parent
    assert hits[0].agent_id == "nodir"


def test_ripgrep_search_main_session_has_no_agent_id(tmp_search_dirs) -> None:
    """A normal main-session Claude match leaves agent_id None."""
    _require_rg()
    claude_file = tmp_search_dirs["claude_projects"] / "-Users-me-repo" / "main.jsonl"
    write_jsonl(
        claude_file,
        [
            {
                "sessionId": "main-1",
                "cwd": "/Users/me/repo",
                "message": {"role": "user", "content": "Needle token in main session"},
            }
        ],
    )

    results = search.ripgrep_search("needle token")
    hits = [r for r in results if r.session_id == "main-1"]
    assert len(hits) == 1
    assert hits[0].agent_id is None


def test_ripgrep_search_multiple_agent_files_one_session(tmp_search_dirs) -> None:
    """Distinct sub-agent files of one session yield distinct rows."""
    _require_rg()
    parent = "multi-parent"
    base = tmp_search_dirs["claude_projects"] / "-Users-me-repo" / parent / "subagents"
    write_jsonl(
        base / "agent-one.jsonl",
        [_agent_record(session_id=parent, agent_id="one", text="Needle token from agent one")],
    )
    write_jsonl(
        base / "agent-two.jsonl",
        [_agent_record(session_id=parent, agent_id="two", text="Needle token from agent two")],
    )

    results = search.ripgrep_search("needle token")
    hits = [r for r in results if r.provider is Provider.CLAUDE and r.agent_id]
    assert {r.agent_id for r in hits} == {"one", "two"}
    assert all(r.session_id == parent for r in hits)
    assert len(hits) == 2


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


def test_ripgrep_search_finds_gemini_session_json(tmp_search_dirs, tmp_path: Path) -> None:
    """Real rg binary finds a query term inside a Gemini session JSON file."""
    _require_rg()
    from tests.helpers import write_gemini_projects, write_gemini_session

    gemini_tmp = tmp_search_dirs["gemini_tmp"]
    write_gemini_projects(gemini_tmp.parent, {"/Users/me/gem": "gem"})
    write_gemini_session(
        gemini_tmp / "gem" / "chats" / "session-2026-01-01T00-00-gem1.json",
        session_id="gemini-session-1",
        messages=[
            {
                "id": "u1",
                "timestamp": "2026-01-01T00:00:01.000Z",
                "type": "user",
                "content": [{"text": "Needle token in Gemini"}],
            }
        ],
    )

    results = search.ripgrep_search("needle token")
    gemini_hits = [r for r in results if r.provider is Provider.GEMINI]
    assert len(gemini_hits) == 1
    assert gemini_hits[0].session_id == "gemini-session-1"
    assert gemini_hits[0].project_path == "/Users/me/gem"


def test_ripgrep_search_gemini_unresolved_hash_uses_tmp_dir(tmp_search_dirs) -> None:
    """Unresolvable hash dirs fall back to the tmp dir path, matching discovery."""
    _require_rg()
    from tests.helpers import write_gemini_session

    gemini_tmp = tmp_search_dirs["gemini_tmp"]
    hashed = "ab" * 32
    write_gemini_session(
        gemini_tmp / hashed / "chats" / "session-2026-01-01T00-00-gem2.json",
        session_id="gemini-session-2",
        messages=[
            {
                "id": "u1",
                "timestamp": "2026-01-01T00:00:01.000Z",
                "type": "user",
                "content": [{"text": "hashed needle here"}],
            }
        ],
    )

    results = search.ripgrep_search("hashed needle")
    gemini_hits = [r for r in results if r.provider is Provider.GEMINI]
    assert len(gemini_hits) == 1
    assert gemini_hits[0].project_path == str(gemini_tmp / hashed)


def test_aggregated_search_includes_gemini_host(tmp_aggregation_search_dirs) -> None:
    """Aggregation mode scans each host's ~/.gemini/tmp and tags the host."""
    _require_rg()
    from tests.helpers import write_gemini_session

    laptop_gemini = tmp_aggregation_search_dirs["laptop"]["gemini_tmp"]
    write_gemini_session(
        laptop_gemini / ("cd" * 32) / "chats" / "session-2026-01-01T00-00-agg1.json",
        session_id="gemini-agg-1",
        messages=[
            {
                "id": "u1",
                "timestamp": "2026-01-01T00:00:01.000Z",
                "type": "user",
                "content": [{"text": "aggregated gemini needle"}],
            }
        ],
    )

    results = search.ripgrep_search(
        "aggregated gemini needle",
        aggregation_root=tmp_aggregation_search_dirs["root"],
    )
    gemini_hits = [r for r in results if r.provider is Provider.GEMINI]
    assert len(gemini_hits) == 1
    assert gemini_hits[0].host == "laptop"
    assert gemini_hits[0].session_id == "gemini-agg-1"
