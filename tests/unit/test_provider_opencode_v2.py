"""opencode 2.0 (``session_v2`` / ``session_message``) database support.

Fixture payloads are trimmed copies of rows written by opencode 2.0.31.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sesh import diagnostics
from sesh.providers import opencode
from sesh.providers.opencode import OpencodeProvider
from tests.helpers import create_opencode_db, create_opencode_v2_db

REPO = "/Users/me/repo"


def _user(mid: str, sid: str, seq: int, text: str, created: int = 1) -> dict:
    return {
        "id": mid, "session_id": sid, "type": "user", "seq": seq,
        "data": {"time": {"created": created}, "text": text, "files": []},
    }


def _assistant(
    mid: str, sid: str, seq: int, content: list[dict], *,
    model: str = "glm-5.3:cloud", tokens: dict | None = None, created: int = 2,
) -> dict:
    data = {
        "time": {"created": created},
        "agent": "build",
        "model": {"id": model, "providerID": "lollama", "variant": "default"},
        "content": content,
        "finish": "stop",
    }
    if tokens is not None:
        data["tokens"] = tokens
    return {"id": mid, "session_id": sid, "type": "assistant", "seq": seq, "data": data}


def _idle(mid: str, sid: str, seq: int) -> dict:
    return {
        "id": mid, "session_id": sid, "type": "idle", "seq": seq,
        "data": {"time": {"created": 9}, "outcome": "succeeded"},
    }


def _tokens(inp: int, out: int, read: int = 0, write: int = 0) -> dict:
    return {"input": inp, "output": out, "reasoning": 0,
            "cache": {"read": read, "write": write}}


def _read_tool(output: str) -> dict:
    return {
        "type": "tool", "id": "call_1", "name": "read",
        "state": {
            "status": "completed",
            "input": {"path": "/repo/hello.py"},
            "content": [{"type": "text", "text": output}],
            "metadata": {"truncated": False},
        },
    }


def _session(sid: str, **extra) -> dict:
    return {"id": sid, "directory": REPO, "title": f"title {sid}",
            "time_created": 1000, "time_updated": 2000, **extra}


def test_v2_session_metadata_and_tokens(tmp_opencode_dir: Path) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_v2_db(
        db,
        sessions=[_session(
            "ses_a", title="Checking hello.py output",
            model={"id": "glm-5.3:cloud", "providerID": "lollama"},
            tokens_input=8467, tokens_output=230, tokens_cache_read=11584,
        )],
        messages=[
            _user("msg_1", "ses_a", 4, "What does hello.py print?"),
            _assistant("msg_2", "ses_a", 5, [_read_tool("1: print('hi')")],
                       tokens=_tokens(7819, 97, read=1856)),
            _assistant("msg_3", "ses_a", 14, [{"type": "text", "text": "hi"}],
                       tokens=_tokens(123, 31, read=9728)),
            _idle("msg_4", "ses_a", 19),
        ],
    )

    provider = OpencodeProvider()
    assert list(provider.discover_projects()) == [(REPO, "repo")]
    [s] = provider.get_sessions(REPO)
    assert s.id == "ses_a"
    assert s.summary == "Checking hello.py output"
    assert s.model == "glm-5.3:cloud"
    # user + 2 assistant rows; the idle marker is not a message
    assert s.message_count == 3
    assert s.source_path == str(db)
    assert s.input_tokens == 123 + 9728
    assert s.output_tokens == 230
    assert s.cumulative_input_tokens == 8467 + 11584


def test_v2_child_sessions_are_counted_not_listed(tmp_opencode_dir: Path) -> None:
    create_opencode_v2_db(
        tmp_opencode_dir / "opencode.db",
        sessions=[
            _session("ses_root"),
            _session("ses_child1", parent_id="ses_root"),
            _session("ses_child2", parent_id="ses_root"),
        ],
    )
    sessions = OpencodeProvider().get_sessions(REPO)
    assert [s.id for s in sessions] == ["ses_root"]
    assert sessions[0].subagent_count == 2


def test_v2_nested_descendants_count_toward_root(tmp_opencode_dir: Path) -> None:
    create_opencode_v2_db(
        tmp_opencode_dir / "opencode.db",
        sessions=[
            _session("ses_root"),
            _session("ses_kid", parent_id="ses_root"),
            _session("ses_grandkid", parent_id="ses_kid"),
        ],
    )
    [root] = OpencodeProvider().get_sessions(REPO)
    assert root.subagent_count == 2


def test_v2_load_subagents_returns_child_threads(tmp_opencode_dir: Path) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_v2_db(
        db,
        sessions=[
            _session("ses_root"),
            # Created out of order: results are sorted by time_created.
            _session("ses_b", parent_id="ses_root", title="Second critic",
                     time_created=3000, tokens_output=40),
            _session("ses_a", parent_id="ses_root", title="First critic",
                     time_created=2000),
            _session("ses_a_kid", parent_id="ses_a", title="Nested helper",
                     time_created=2500),
            _session("ses_other", title="Unrelated root"),
            _session("ses_other_kid", parent_id="ses_other"),
        ],
        messages=[
            _user("msg_r1", "ses_root", 1, "Ask two critics."),
            _user("msg_a1", "ses_a", 1, "Critique this.", created=2001),
            _assistant("msg_a2", "ses_a", 2, [
                {"type": "reasoning", "text": "Thinking it over."},
                {"type": "text", "text": "Looks fine."},
            ], created=2002),
            _idle("msg_a3", "ses_a", 3),
            _user("msg_b1", "ses_b", 1, "Critique again.", created=3001),
        ],
    )
    root = next(s for s in OpencodeProvider().get_sessions(REPO) if s.id == "ses_root")
    loaded = OpencodeProvider().load_subagents(root)

    assert [m.agent_id for m, _ in loaded] == ["ses_a", "ses_a_kid", "ses_b"]
    meta_a, msgs_a = loaded[0]
    assert meta_a.description == "First critic"
    assert meta_a.agent_type == "build"
    assert meta_a.file_path == str(db)
    assert meta_a.first_timestamp == opencode._parse_timestamp(2000)
    # Reasoning is loaded (shown under the thinking toggle) but not counted.
    assert [m.content_type for m in msgs_a] == ["text", "thinking", "text"]
    assert meta_a.message_count == 2
    assert meta_a.output_tokens is None

    meta_b, msgs_b = loaded[2]
    assert meta_b.agent_type is None  # no assistant reply yet
    assert meta_b.output_tokens == 40
    assert [m.content for m in msgs_b] == ["Critique again."]

    assert OpencodeProvider().discover_subagents(root) == [m for m, _ in loaded]


def test_v2_load_subagents_survives_parent_cycle(tmp_opencode_dir: Path) -> None:
    create_opencode_v2_db(
        tmp_opencode_dir / "opencode.db",
        sessions=[
            _session("ses_root"),
            _session("ses_x", parent_id="ses_y"),
            _session("ses_y", parent_id="ses_x"),
            _session("ses_kid", parent_id="ses_root"),
        ],
    )
    root = next(s for s in OpencodeProvider().get_sessions(REPO) if s.id == "ses_root")
    assert [m.agent_id for m, _ in OpencodeProvider().load_subagents(root)] == [
        "ses_kid"
    ]


def test_load_subagents_needs_a_parent_id_column_and_a_db(
    tmp_opencode_dir: Path,
) -> None:
    create_opencode_db(
        tmp_opencode_dir / "opencode.db",
        sessions=[{"id": "ses_v1", "directory": REPO, "title": "t",
                   "time_created": 1, "time_updated": 1}],
    )
    [session] = OpencodeProvider().get_sessions(REPO)
    assert OpencodeProvider().load_subagents(session) == []
    session.source_path = str(tmp_opencode_dir / "storage" / "session" / "p" / "s.json")
    assert OpencodeProvider().load_subagents(session) == []


def test_v2_get_messages_maps_message_and_content_types(
    tmp_opencode_dir: Path,
) -> None:
    sid = "ses_m"
    create_opencode_v2_db(
        tmp_opencode_dir / "opencode.db",
        sessions=[_session(sid)],
        messages=[
            _user("msg_01", sid, 1, "What does hello.py print?"),
            _assistant("msg_02", sid, 2, [
                {"type": "reasoning", "text": "Let me read the file."},
                _read_tool("1: print('hi')"),
                {"type": "tool", "id": "call_2", "name": "bash",
                 "state": {"status": "error", "input": {"command": "false"},
                           "error": {"type": "exit", "message": "exit 1"}}},
                {"type": "tool", "id": "call_3", "name": "glob",
                 "state": {"status": "running", "input": {"pattern": "*"},
                           "metadata": {}}},
            ]),
            _assistant("msg_03", sid, 3, [{"type": "text", "text": "It prints hi."}]),
            {"id": "msg_04", "session_id": sid, "type": "synthetic", "seq": 4,
             "data": {"time": {"created": 4}, "text": "The server restarted.",
                      "description": "Continuing after restart"}},
            {"id": "msg_05", "session_id": sid, "type": "shell", "seq": 5,
             "data": {"time": {"created": 5}, "shellID": "sh_1", "command": "ls",
                      "status": "exited",
                      "output": {"output": "hello.py\n", "cursor": 0,
                                 "size": 9, "truncated": False}}},
            {"id": "msg_06", "session_id": sid, "type": "compaction", "seq": 6,
             "data": {"time": {"created": 6}, "status": "completed",
                      "reason": "auto", "summary": "Earlier: read hello.py",
                      "recent": ""}},
            {"id": "msg_07", "session_id": sid, "type": "model-switched", "seq": 7,
             "data": {"time": {"created": 7}, "model": {"id": "x"}}},
            _idle("msg_08", sid, 8),
        ],
    )
    provider = OpencodeProvider()
    [session] = provider.get_sessions(REPO)
    msgs = provider.get_messages(session)

    summary = [(m.role, m.content_type, m.tool_name, m.is_system) for m in msgs]
    assert summary == [
        ("user", "text", None, False),
        ("assistant", "thinking", None, False),
        ("assistant", "tool_use", "read", False),
        ("tool", "tool_result", "read", False),
        ("assistant", "tool_use", "bash", False),
        ("tool", "tool_result", "bash", False),
        ("assistant", "tool_use", "glob", False),  # running: no result yet
        ("assistant", "text", None, False),
        ("user", "text", None, True),  # synthetic -> hidden record
        ("user", "tool_use", "shell", False),
        ("tool", "tool_result", "shell", False),
        ("assistant", "text", None, True),  # compaction summary -> hidden
    ]
    assert msgs[0].content == "What does hello.py print?"
    assert msgs[1].thinking == "Let me read the file."
    assert '"path": "/repo/hello.py"' in msgs[2].tool_input
    assert msgs[3].tool_output == "1: print('hi')"
    assert msgs[5].tool_output == "exit 1"
    assert msgs[7].content == "It prints hi."
    assert msgs[9].tool_input == "ls"
    assert msgs[10].tool_output == "hello.py\n"
    assert msgs[11].content == "Earlier: read hello.py"


def test_v2_staged_revert_hides_target_and_later_messages(
    tmp_opencode_dir: Path,
) -> None:
    sid = "ses_r"
    create_opencode_v2_db(
        tmp_opencode_dir / "opencode.db",
        sessions=[_session(sid, revert={"messageID": "msg_3", "partID": "ignored"},
                           tokens_input=999, tokens_output=99)],
        messages=[
            _user("msg_1", sid, 1, "keep user"),
            _assistant("msg_2", sid, 2, [{"type": "text", "text": "keep answer"}],
                       model="kept-model", tokens=_tokens(100, 10, read=5)),
            _user("msg_3", sid, 3, "undo user"),
            _assistant("msg_4", sid, 4, [{"type": "text", "text": "undo answer"}],
                       model="undone-model", tokens=_tokens(500, 50)),
        ],
    )
    provider = OpencodeProvider()
    [s] = provider.get_sessions(REPO)
    assert s.message_count == 2
    assert s.model == "kept-model"
    assert s.input_tokens == 105
    # Incurred usage still includes the undone turn.
    assert s.cumulative_input_tokens == 999
    assert [m.content for m in provider.get_messages(s)] == [
        "keep user", "keep answer",
    ]


def test_v2_revert_to_unknown_message_projects_nothing(
    tmp_opencode_dir: Path,
) -> None:
    sid = "ses_u"
    create_opencode_v2_db(
        tmp_opencode_dir / "opencode.db",
        sessions=[_session(sid, revert={"messageID": "msg_gone"})],
        messages=[_user("msg_1", sid, 1, "a"), _user("msg_2", sid, 2, "b")],
    )
    provider = OpencodeProvider()
    [s] = provider.get_sessions(REPO)
    assert [m.content for m in provider.get_messages(s)] == ["a", "b"]


def test_migrated_db_prefers_v2_copy_and_keeps_v1_only_sessions(
    tmp_opencode_dir: Path,
) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_db(
        db,
        sessions=[
            {"id": "ses_both", "directory": REPO, "title": "v1 title",
             "time_created": 1, "time_updated": 2},
            {"id": "ses_v1only", "directory": REPO, "title": "v1 only",
             "time_created": 1, "time_updated": 1},
        ],
        messages=[{"id": "msg_v1", "session_id": "ses_both",
                   "data": {"role": "user", "time": {"created": 1}}}],
        parts=[{"id": "prt_v1", "message_id": "msg_v1", "session_id": "ses_both",
                "data": {"type": "text", "text": "from v1"}}],
    )
    create_opencode_v2_db(
        db,
        sessions=[_session("ses_both", title="v2 title")],
        messages=[_user("msg_v2", "ses_both", 1, "from v2")],
    )
    provider = OpencodeProvider()
    by_id = {s.id: s for s in provider.get_sessions(REPO)}
    assert set(by_id) == {"ses_both", "ses_v1only"}
    assert by_id["ses_both"].summary == "v2 title"
    assert [m.content for m in provider.get_messages(by_id["ses_both"])] == ["from v2"]


def test_default_db_wins_over_stale_next_db(tmp_opencode_dir: Path) -> None:
    create_opencode_v2_db(
        tmp_opencode_dir / "opencode-next.db",
        sessions=[_session("ses_x", title="stale")],
    )
    create_opencode_v2_db(
        tmp_opencode_dir / "opencode.db",
        sessions=[_session("ses_x", title="current")],
    )
    [s] = OpencodeProvider().get_sessions(REPO)
    assert s.summary == "current"
    assert s.source_path == str(tmp_opencode_dir / "opencode.db")


def test_db_sessions_bypass_mtime_cache(tmp_opencode_dir: Path) -> None:
    """WAL writes don't touch the main file, so a cache hit could be stale."""
    create_opencode_v2_db(
        tmp_opencode_dir / "opencode.db", sessions=[_session("ses_fresh")],
    )

    class StaleCache:
        def get_sessions(self, path):
            raise AssertionError("opencode DB results must not be cached")

        def put_sessions(self, path, sessions):
            raise AssertionError("opencode DB results must not be cached")

    sessions = OpencodeProvider(cache=StaleCache()).get_sessions(REPO)
    assert [s.id for s in sessions] == ["ses_fresh"]


def test_v2_delete_removes_session_children_and_v1_copy(
    tmp_opencode_dir: Path,
) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_db(
        db,
        sessions=[{"id": "ses_del", "directory": REPO, "title": "v1",
                   "time_created": 1, "time_updated": 1}],
    )
    create_opencode_v2_db(
        db,
        sessions=[
            _session("ses_del"),
            _session("ses_kid", parent_id="ses_del"),
            _session("ses_grandkid", parent_id="ses_kid"),
            _session("ses_keep"),
        ],
        messages=[
            _user("msg_1", "ses_del", 1, "bye"),
            _user("msg_2", "ses_kid", 1, "bye too"),
            _user("msg_3", "ses_keep", 1, "stay"),
        ],
    )
    provider = OpencodeProvider()
    target = next(s for s in provider.get_sessions(REPO) if s.id == "ses_del")
    provider.delete_session(target)

    conn = sqlite3.connect(db)
    try:
        assert [r[0] for r in conn.execute("SELECT id FROM session_v2")] == ["ses_keep"]
        assert [r[0] for r in conn.execute(
            "SELECT session_id FROM session_message")] == ["ses_keep"]
        assert conn.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
    finally:
        conn.close()
    assert [s.id for s in OpencodeProvider().get_sessions(REPO)] == ["ses_keep"]


def test_v2_move_rewrites_directory_and_project_worktree(
    tmp_opencode_dir: Path,
) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_v2_db(
        db,
        sessions=[_session("ses_1"), _session("ses_2", directory="/elsewhere")],
        projects=[{"id": "proj", "worktree": REPO}],
    )
    report = OpencodeProvider().move_project(REPO, "/Users/me/moved")
    assert report.success
    assert report.files_modified == 1

    conn = sqlite3.connect(db)
    try:
        rows = dict(conn.execute("SELECT id, directory FROM session_v2"))
        worktree = conn.execute("SELECT worktree FROM project").fetchone()[0]
    finally:
        conn.close()
    assert rows == {"ses_1": "/Users/me/moved", "ses_2": "/elsewhere"}
    assert worktree == "/Users/me/moved"


def test_unrecognized_schema_is_a_doctor_warning(
    tmp_opencode_dir: Path, monkeypatch,
) -> None:
    tmp_opencode_dir.mkdir(parents=True)
    conn = sqlite3.connect(tmp_opencode_dir / "opencode.db")
    conn.execute("CREATE TABLE session_v3 (id TEXT)")
    conn.close()

    assert len(OpencodeProvider().diagnostic_issues()) == 1
    result = diagnostics._probe_provider("opencode", base_dir=None, host=None)
    schema = [i for i in result["issues"] if i["stage"] == "schema"]
    assert len(schema) == 1
    assert "unrecognized opencode database schema" in schema[0]["message"]


def test_recognized_schemas_raise_no_diagnostic(tmp_opencode_dir: Path) -> None:
    create_opencode_v2_db(tmp_opencode_dir / "opencode.db", sessions=[])
    create_opencode_db(tmp_opencode_dir / "opencode-beta.db", sessions=[])
    assert OpencodeProvider().diagnostic_issues() == []


def test_db_paths_order(tmp_path: Path) -> None:
    for name in ("opencode-next.db", "opencode.db", "opencode-beta.db"):
        (tmp_path / name).touch()
    assert [p.name for p in opencode.db_paths(tmp_path)] == [
        "opencode.db", "opencode-beta.db", "opencode-next.db",
    ]
