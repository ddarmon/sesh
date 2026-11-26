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


def test_ripgrep_search_workflow_agent_file(tmp_search_dirs) -> None:
    """A hit inside a Workflow-tool agent file
    ({sessionId}/subagents/workflows/{wf}/agent-*.jsonl) is attributed to the
    parent session (four dirs up) and tagged with the agent_id."""
    _require_rg()
    parent = "parent-wf"
    agent_file = (
        tmp_search_dirs["claude_projects"]
        / "-Users-me-repo"
        / parent
        / "subagents"
        / "workflows"
        / "wf_a1be27ca-98b"
        / "agent-wf7.jsonl"
    )
    write_jsonl(
        agent_file,
        [_agent_record(session_id=parent, agent_id="wf7", text="Needle token in workflow")],
    )

    results = search.ripgrep_search("needle token")
    hits = [r for r in results if r.provider is Provider.CLAUDE and r.agent_id]
    assert len(hits) == 1
    assert hits[0].session_id == parent
    assert hits[0].agent_id == "wf7"
    assert hits[0].project_path == "/Users/me/repo"


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


def test_ripgrep_search_codex_subagent_attributes_root(tmp_search_dirs) -> None:
    """Codex child hits retain match provenance and link to the root rollout.

    A pre-subagent index could list the child id as a root session.  The file
    header remains authoritative even when that stale lookup entry is present.
    """
    _require_rg()
    root_id = "123e4567-e89b-12d3-a456-426614174000"
    child_id = "123e4567-e89b-12d3-a456-426614174001"
    root_file = tmp_search_dirs["codex_sessions"] / f"rollout-{root_id}.jsonl"
    child_file = tmp_search_dirs["codex_sessions"] / f"rollout-{child_id}.jsonl"
    write_jsonl(root_file, [{
        "type": "session_meta",
        "timestamp": "2026-07-11T18:00:00Z",
        "payload": {"id": root_id, "cwd": "/Users/me/codex"},
    }])
    write_jsonl(child_file, [
        {
            "type": "session_meta",
            "timestamp": "2026-07-11T18:00:01Z",
            "payload": {
                "id": child_id,
                "session_id": root_id,
                "parent_thread_id": root_id,
                "thread_source": "subagent",
                "cwd": "/Users/me/codex",
                "agent_path": "/root/reviewer",
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-07-11T18:00:02Z",
            "payload": {"type": "agent_message", "message": "unique child needle"},
        },
    ])

    stale_lookup = {(child_id, Provider.CODEX.value): "/stale/index/project"}
    hits = [
        r for r in search.ripgrep_search(
            "unique child needle", cwd_lookup=stale_lookup
        )
        if r.provider is Provider.CODEX
    ]

    assert len(hits) == 1
    assert hits[0].session_id == root_id
    assert hits[0].agent_id == child_id
    assert hits[0].project_path == "/Users/me/codex"
    assert hits[0].file_path == str(child_file)
    assert hits[0].root_file_path == str(root_file)


def test_ripgrep_search_finds_cline_transcript(tmp_search_dirs) -> None:
    """Real rg binary finds a query term inside a Cline transcript."""
    _require_rg()
    from tests.helpers import write_cline_session

    data_dir = tmp_search_dirs["cline_sessions"].parent
    write_cline_session(
        data_dir,
        session_id="1788435477952_eibi9",
        workspace_root="/Users/me/cline-repo",
        messages=[{
            "id": "m1", "role": "user", "ts": 1788435478090,
            "content": [{
                "type": "text",
                "text": '<user_input mode="act">Cline needle token</user_input>',
            }],
        }],
    )

    results = search.ripgrep_search("cline needle")
    hits = [r for r in results if r.provider is Provider.CLINE]
    assert len(hits) == 1
    assert hits[0].session_id == "1788435477952_eibi9"
    assert hits[0].project_path == "/Users/me/cline-repo"
    assert hits[0].file_path.endswith("1788435477952_eibi9.messages.json")
    assert hits[0].host is None


def test_ripgrep_search_skips_cline_session_without_record(tmp_search_dirs) -> None:
    """A transcript with no sibling record has no recoverable project path."""
    _require_rg()
    from tests.helpers import write_cline_session

    data_dir = tmp_search_dirs["cline_sessions"].parent
    write_cline_session(
        data_dir, session_id="1_a", workspace_root="/Users/me/cline-repo",
        messages=[{"id": "m1", "role": "user", "ts": 1, "content": "orphan needle"}],
    )
    (data_dir / "sessions" / "1_a" / "1_a.json").unlink()

    results = search.ripgrep_search("orphan needle")
    assert [r for r in results if r.provider is Provider.CLINE] == []


def test_aggregated_search_includes_cline_host(tmp_aggregation_search_dirs) -> None:
    """Aggregation mode scans each host's ~/.cline/data and tags the host."""
    _require_rg()
    from tests.helpers import write_cline_session

    for host in ("laptop", "desktop"):
        write_cline_session(
            tmp_aggregation_search_dirs[host]["cline_sessions"].parent,
            session_id="1_a",
            workspace_root=f"/Users/me/{host}",
            messages=[{
                "id": "m1", "role": "user", "ts": 1,
                "content": "aggregated cline needle",
            }],
        )

    results = search.ripgrep_search(
        "aggregated cline needle",
        aggregation_root=tmp_aggregation_search_dirs["root"],
    )
    hits = [r for r in results if r.provider is Provider.CLINE]
    assert {r.host for r in hits} == {"laptop", "desktop"}
    assert {r.project_path for r in hits} == {
        "/Users/me/laptop", "/Users/me/desktop",
    }


def test_ripgrep_search_skips_cline_subagent_transcripts(tmp_search_dirs) -> None:
    """Sub-agent sessions are hidden by discovery, so search must not surface them.

    A hit attributed to a session the tree never lists could neither be opened
    nor safely deleted by `sesh clean`.
    """
    _require_rg()
    from tests.helpers import write_cline_session

    data_dir = tmp_search_dirs["cline_sessions"].parent
    write_cline_session(
        data_dir, session_id="1_a", workspace_root="/Users/me/cline-repo",
        messages=[{"id": "m1", "role": "user", "ts": 1, "content": "shared needle text"}],
    )
    write_cline_session(
        data_dir, session_id="2_b", workspace_root="/Users/me/cline-repo",
        is_subagent=True, parent_session_id="1_a",
        messages=[{"id": "m1", "role": "user", "ts": 1, "content": "shared needle text"}],
    )

    results = search.ripgrep_search("shared needle text")
    hits = [r for r in results if r.provider is Provider.CLINE]
    assert [r.session_id for r in hits] == ["1_a"]


def test_ripgrep_search_finds_agy_transcript(tmp_search_dirs) -> None:
    """Real rg binary finds a query term inside an agy transcript, once."""
    _require_rg()
    from tests.helpers import agy_reply, agy_user, write_agy_conversation

    conversation_id = "11111111-1111-4111-8111-111111111111"
    transcript = write_agy_conversation(
        tmp_search_dirs["agy_data"],
        conversation_id=conversation_id,
        workspace="/Users/me/agy repo",
        records=[agy_user(0, "Agy needle token"), agy_reply(1, "found it")],
    )
    # agy also keeps chunked copies of the same records; they must not
    # produce a second hit for the session.
    chunk = transcript.parent / "chunks" / "transcript_full" / "00000000.jsonl"
    chunk.parent.mkdir(parents=True)
    chunk.write_text(transcript.read_text())

    results = search.ripgrep_search("agy needle")
    hits = [r for r in results if r.provider is Provider.AGY]
    assert len(hits) == 1
    assert hits[0].session_id == conversation_id
    assert hits[0].project_path == "/Users/me/agy repo"
    assert hits[0].file_path == str(transcript)
    assert hits[0].agent_id is None
    assert hits[0].root_file_path is None
    assert hits[0].host is None


def test_ripgrep_search_attributes_agy_subagent_hit_to_root(tmp_search_dirs) -> None:
    """A hit inside a sub-agent conversation points at the listed root session."""
    _require_rg()
    from tests.helpers import agy_reply, write_agy_conversation

    root_id = "11111111-1111-4111-8111-111111111111"
    child_id = "22222222-2222-4222-8222-222222222222"
    data_dir = tmp_search_dirs["agy_data"]
    root_file = write_agy_conversation(
        data_dir, conversation_id=root_id, workspace="/Users/me/agy-repo",
    )
    child_file = write_agy_conversation(
        data_dir, conversation_id=child_id, parent_id=root_id,
        workspace="/Users/me/agy-repo",
        records=[agy_reply(0, "subagent only needle")],
    )

    results = search.ripgrep_search("subagent only needle")
    hits = [r for r in results if r.provider is Provider.AGY]
    assert len(hits) == 1
    assert hits[0].session_id == root_id
    assert hits[0].agent_id == child_id
    assert hits[0].project_path == "/Users/me/agy-repo"
    assert hits[0].file_path == str(child_file)
    assert hits[0].root_file_path == str(root_file)


def test_aggregated_search_includes_agy_host(tmp_aggregation_search_dirs) -> None:
    """Aggregation mode scans each host's ~/.gemini/antigravity-cli."""
    _require_rg()
    from tests.helpers import agy_user, write_agy_conversation

    for host in ("laptop", "desktop"):
        write_agy_conversation(
            tmp_aggregation_search_dirs[host]["agy_data"],
            workspace=f"/Users/me/{host}",
            records=[agy_user(0, "aggregated agy needle")],
        )

    results = search.ripgrep_search(
        "aggregated agy needle",
        aggregation_root=tmp_aggregation_search_dirs["root"],
    )
    hits = [r for r in results if r.provider is Provider.AGY]
    assert {r.host for r in hits} == {"laptop", "desktop"}
    assert {r.project_path for r in hits} == {
        "/Users/me/laptop", "/Users/me/desktop",
    }


# ---------------------------------------------------------------------------
# Text-only matching: opaque payloads and metadata never produce a hit
# ---------------------------------------------------------------------------

# A base64-looking run that happens to contain "wiz" (case-insensitively).
_BLOB = "gAAAAABpD2iHCox57CKV64orXrGHWSUQzvXZv2NlJVS7TftDKwiZ9xcSlTwqV" * 4


def _claude_blob_record(session_id: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "cwd": "/Users/me/repo",
        "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "considering", "signature": _BLOB},
        ]},
    }


def test_ripgrep_search_drops_blob_only_matches(tmp_search_dirs) -> None:
    """A query that appears only inside a thinking signature is not a hit."""
    _require_rg()
    project = tmp_search_dirs["claude_projects"] / "-Users-me-repo"
    write_jsonl(project / "blob-only.jsonl", [_claude_blob_record("blob-only")])

    assert search.ripgrep_search("wiz") == []


def test_ripgrep_search_reads_past_a_blob_first_match(tmp_search_dirs) -> None:
    """ripgrep's first matching line is a blob; the real hit is further down."""
    _require_rg()
    project = tmp_search_dirs["claude_projects"] / "-Users-me-repo"
    write_jsonl(project / "mixed.jsonl", [
        _claude_blob_record("mixed"),
        {"type": "user", "sessionId": "mixed", "cwd": "/Users/me/repo",
         "message": {"role": "user", "content": "Make Anki cards about Wiz."}},
    ])

    results = search.ripgrep_search("wiz")
    assert [r.session_id for r in results] == ["mixed"]
    assert results[0].matched_line == "Make Anki cards about Wiz."
    assert results[0].project_path == "/Users/me/repo"


def test_ripgrep_search_drops_metadata_only_matches(tmp_search_dirs) -> None:
    """A query found only in the record envelope (cwd) is not a hit."""
    _require_rg()
    project = tmp_search_dirs["claude_projects"] / "-Users-me-needlerepo"
    write_jsonl(project / "meta.jsonl", [
        {"type": "user", "sessionId": "meta", "cwd": "/Users/me/needlerepo",
         "message": {"role": "user", "content": "hello"}},
    ])

    assert search.ripgrep_search("needlerepo") == []


def test_ripgrep_search_regex_snippet_shows_the_match(tmp_search_dirs) -> None:
    _require_rg()
    project = tmp_search_dirs["claude_projects"] / "-Users-me-repo"
    text = ("filler " * 80) + "we use Wiz daily " + ("filler " * 80)
    write_jsonl(project / "rx.jsonl", [
        {"type": "user", "sessionId": "wizard", "cwd": "/Users/me/repo",
         "message": {"role": "user", "content": "run the setup wizard"}},
    ])
    write_jsonl(project / "rx2.jsonl", [
        {"type": "user", "sessionId": "rx", "cwd": "/Users/me/repo",
         "message": {"role": "user", "content": text}},
    ])

    results = search.ripgrep_search(r"\bwiz\b")
    assert [r.session_id for r in results] == ["rx"]
    assert "Wiz" in results[0].matched_line


def test_ripgrep_search_codex_ignores_encrypted_reasoning(tmp_search_dirs) -> None:
    _require_rg()
    rollout = (
        tmp_search_dirs["codex_sessions"] / "2026" / "01" / "01"
        / "rollout-2026-01-01T00-00-00-11111111-2222-4333-8444-555555555555.jsonl"
    )
    write_jsonl(rollout, [
        {"type": "session_meta",
         "payload": {"id": "11111111-2222-4333-8444-555555555555", "cwd": "/Users/me/repo"}},
        {"type": "response_item",
         "payload": {"type": "reasoning", "summary": [], "encrypted_content": _BLOB}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
    ])

    assert search.ripgrep_search("wiz") == []
    assert [r.session_id for r in search.ripgrep_search("hello")] == [
        "11111111-2222-4333-8444-555555555555"
    ]


def test_ripgrep_search_gemini_ignores_non_message_fields(tmp_search_dirs) -> None:
    """A Gemini file is one JSON document; only its messages are searched."""
    _require_rg()
    from tests.helpers import write_gemini_projects, write_gemini_session

    gemini_tmp = tmp_search_dirs["gemini_tmp"]
    write_gemini_projects(gemini_tmp.parent, {"/Users/me/gem": "gem"})
    write_gemini_session(
        gemini_tmp / "gem" / "chats" / "session-2026-01-01T00-00-gem1.json",
        session_id="needle-in-the-id",
        messages=[{
            "id": "u1", "timestamp": "2026-01-01T00:00:01.000Z",
            "type": "user", "content": [{"text": "hello there"}],
        }],
    )

    assert [r for r in search.ripgrep_search("needle-in-the-id")
            if r.provider is Provider.GEMINI] == []
    hits = [r for r in search.ripgrep_search("hello there")
            if r.provider is Provider.GEMINI]
    assert len(hits) == 1
    assert hits[0].matched_line == "hello there"


def test_ripgrep_search_cline_ignores_non_message_fields(tmp_search_dirs) -> None:
    _require_rg()
    from tests.helpers import write_cline_session

    write_cline_session(
        tmp_search_dirs["cline_sessions"].parent,
        session_id="1788435477952_eibi9",
        workspace_root="/Users/me/cline-repo",
        messages=[{
            "id": "needle-message-id", "role": "user", "ts": 1788435478090,
            "content": [{"type": "text", "text": "plain words"}],
        }],
    )

    assert [r for r in search.ripgrep_search("needle-message-id")
            if r.provider is Provider.CLINE] == []
    assert len([r for r in search.ripgrep_search("plain words")
                if r.provider is Provider.CLINE]) == 1


def test_ripgrep_search_agy_ignores_rewound_steps(tmp_search_dirs) -> None:
    """Steps a /rewind abandoned stay in the file but not in the transcript."""
    _require_rg()
    from tests.helpers import agy_reply, agy_user, write_agy_conversation

    write_agy_conversation(
        tmp_search_dirs["agy_data"],
        records=[
            agy_user(0, "abandoned needle prompt"),
            agy_reply(1, "old reply"),
            agy_user(0, "the prompt that was kept"),
            agy_reply(1, "new reply"),
        ],
    )

    assert [r for r in search.ripgrep_search("abandoned needle")
            if r.provider is Provider.AGY] == []
    assert len([r for r in search.ripgrep_search("prompt that was kept")
                if r.provider is Provider.AGY]) == 1


def test_ripgrep_search_opencode_storage_ignores_ids(tmp_search_dirs) -> None:
    """A legacy opencode part matching only in an id does not claim the session."""
    _require_rg()
    from tests.helpers import write_opencode_storage_session

    write_opencode_storage_session(
        tmp_search_dirs["opencode_data"],
        session_id="ses_needleid",
        title="Fix the bug",
        messages=[{"id": "msg_1", "role": "user"}],
        parts={"msg_1": [{
            "id": "prt_1", "type": "text", "text": "plain words",
            "sessionID": "ses_needleid", "messageID": "msg_1",
        }]},
    )

    assert [r for r in search.ripgrep_search("ses_needleid")
            if r.provider is Provider.OPENCODE] == []
    hits = [r for r in search.ripgrep_search("plain words")
            if r.provider is Provider.OPENCODE]
    assert [r.session_id for r in hits] == ["ses_needleid"]
