"""opencode 1.x (pre-2.0 SQLite) task-tool sub-agent sessions.

Fixture payloads are trimmed copies of rows written by opencode 1.19.3: a
``task`` tool call runs in a child ``session`` row whose ``parent_id`` is the
spawning session and whose title ends in ``(@{agent} subagent)``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sesh import search
from sesh.providers import opencode
from sesh.providers.opencode import OpencodeProvider
from tests.helpers import create_opencode_db

REPO = "/Users/me/repo"
ROOT = "ses_root"
HAIKU = "ses_haiku"
PRIMES = "ses_primes"


def _session(sid: str, title: str, created: int, **extra) -> dict:
    return {"id": sid, "directory": REPO, "title": title,
            "time_created": created, "time_updated": created + 1, **extra}


def _message(mid: str, sid: str, role: str, agent: str, created: int) -> dict:
    return {"id": mid, "session_id": sid, "time_created": created,
            "data": {"role": role, "agent": agent, "time": {"created": created}}}


def _part(pid: str, mid: str, sid: str, data: dict) -> dict:
    return {"id": pid, "message_id": mid, "session_id": sid, "data": data}


def _task_part(pid: str, mid: str, child: str, description: str) -> dict:
    return _part(pid, mid, ROOT, {
        "type": "tool", "tool": "task", "callID": f"call_{pid}",
        "state": {
            "status": "completed",
            "input": {"description": description, "prompt": "...",
                      "subagent_type": "general"},
            "output": f'<task id="{child}" state="completed">\n'
                      "<task_result>\ndone\n</task_result>\n</task>",
            "metadata": {"parentSessionId": ROOT, "sessionId": child},
        },
    })


def _create_task_db(db: Path, extra_sessions: list[dict] | None = None) -> None:
    create_opencode_db(
        db,
        sessions=[
            _session(ROOT, "New session", 1000, parent_id=None),
            # Created out of order: sub-agents are sorted by time_created.
            _session(PRIMES, "List prime numbers >100 (@general subagent)",
                     3000, parent_id=ROOT, tokens_output=402),
            _session(HAIKU, "Write haiku about rivers (@general subagent)",
                     2000, parent_id=ROOT),
            *(extra_sessions or []),
        ],
        messages=[
            _message("msg_r1", ROOT, "user", "build", 1001),
            _message("msg_r2", ROOT, "assistant", "build", 1002),
            _message("msg_h1", HAIKU, "user", "general", 2001),
            _message("msg_h2", HAIKU, "assistant", "general", 2002),
            _message("msg_p1", PRIMES, "user", "general", 3001),
        ],
        parts=[
            _part("prt_r1", "msg_r1", ROOT,
                  {"type": "text", "text": "Launch two sub-agents."}),
            _task_part("prt_r2", "msg_r2", HAIKU, "Write haiku about rivers"),
            _task_part("prt_r3", "msg_r2", PRIMES, "List prime numbers >100"),
            _part("prt_h1", "msg_h1", HAIKU,
                  {"type": "text", "text": "Write a poem about rivers."}),
            _part("prt_h2", "msg_h2", HAIKU, {"type": "step-start"}),
            _part("prt_h3", "msg_h2", HAIKU,
                  {"type": "reasoning", "text": "Short and evocative."}),
            _part("prt_h4", "msg_h2", HAIKU,
                  {"type": "text", "text": "River cuts through stone"}),
            _part("prt_p1", "msg_p1", PRIMES,
                  {"type": "text", "text": "List three primes above 100."}),
        ],
    )


def test_v1_child_sessions_are_counted_not_listed(tmp_opencode_dir: Path) -> None:
    _create_task_db(
        tmp_opencode_dir / "opencode.db",
        extra_sessions=[_session("ses_nested", "Nested (@explore subagent)",
                                 2500, parent_id=HAIKU)],
    )
    [root] = OpencodeProvider().get_sessions(REPO)
    assert root.id == ROOT
    assert root.subagent_count == 3


def test_v1_load_subagents_returns_child_threads(tmp_opencode_dir: Path) -> None:
    db = tmp_opencode_dir / "opencode.db"
    _create_task_db(db)
    [root] = OpencodeProvider().get_sessions(REPO)
    loaded = OpencodeProvider().load_subagents(root)

    assert [m.agent_id for m, _ in loaded] == [HAIKU, PRIMES]
    meta, messages = loaded[0]
    assert meta.description == "Write haiku about rivers"
    assert meta.agent_type == "general"
    assert meta.file_path == str(db)
    assert meta.first_timestamp == opencode._parse_timestamp(2000)
    assert [(m.role, m.content_type) for m in messages] == [
        ("user", "text"), ("assistant", "thinking"), ("assistant", "text"),
    ]
    assert meta.message_count == 2

    primes, _ = loaded[1]
    assert primes.agent_type is None  # no assistant reply yet
    assert primes.output_tokens == 402


def test_v1_delete_removes_child_sessions(tmp_opencode_dir: Path) -> None:
    db = tmp_opencode_dir / "opencode.db"
    _create_task_db(db, extra_sessions=[_session("ses_other", "Other", 5000)])
    provider = OpencodeProvider()
    root = next(s for s in provider.get_sessions(REPO) if s.id == ROOT)
    provider.delete_session(root)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT id FROM session").fetchall() == [("ses_other",)]
        assert conn.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM part").fetchone()[0] == 0
    finally:
        conn.close()


def test_v1_search_drops_child_session_hits(tmp_search_dirs) -> None:
    data_dir = tmp_search_dirs["opencode_data"]
    _create_task_db(data_dir / "opencode.db")
    # "rivers" appears in the root's task input and in the child's prompt.
    results = search._search_opencode_db("rivers", data_dir, None)
    assert [r.session_id for r in results] == [ROOT]
    assert search._search_opencode_db("evocative", data_dir, None) == []


def test_subagent_description_strips_the_1x_title_suffix() -> None:
    describe = opencode.subagent_description
    assert describe("Write haiku (@general subagent)") == "Write haiku"
    assert describe("Scan repo (@my-explorer subagent) ") == "Scan repo"
    assert describe("Critique essay agent 1") == "Critique essay agent 1"
    assert describe("(@general subagent)") is None
    assert describe(None) is None
