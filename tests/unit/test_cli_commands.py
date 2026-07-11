from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import pytest

from sesh import cli
from sesh.cache import _session_to_dict
from sesh.models import Message, Provider, SearchResult
from tests.helpers import make_message, make_session


def _ns(**kwargs):
    return argparse.Namespace(**kwargs)


def _session_dict(**overrides) -> dict:
    return _session_to_dict(make_session(**overrides))


def test_require_index_missing_exits(monkeypatch, capsys) -> None:
    """Missing index prints a 'run refresh' hint and exits with code 1."""
    import sesh.cache as cache_mod

    monkeypatch.setattr(cache_mod, "load_index", lambda: None)
    with pytest.raises(SystemExit) as exc:
        cli._require_index()
    assert exc.value.code == 1
    assert "Run 'sesh refresh' first" in capsys.readouterr().err


def test_cmd_projects_outputs_projects(monkeypatch, capsys) -> None:
    """'sesh projects' outputs the projects array from the index as JSON."""
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: {"projects": [{"path": "/repo"}]})
    cli.cmd_projects(_ns())
    assert json.loads(capsys.readouterr().out) == [{"path": "/repo"}]


def test_cmd_sessions_filters_and_strips_source_path(monkeypatch, capsys) -> None:
    """'sesh sessions' filters by --project/--provider and strips source_path from output."""
    index = {
        "sessions": [
            _session_dict(
                id="a",
                project_path="/p1",
                provider=Provider.CLAUDE,
                source_path="/tmp/a",
                model="m1",
            ),
            _session_dict(
                id="b",
                project_path="/p2",
                provider=Provider.CODEX,
                source_path="/tmp/b",
                model=None,
            ),
        ]
    }
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: index)

    cli.cmd_sessions(_ns(project="/p1", provider="claude"))
    out = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in out] == ["a"]
    assert "source_path" not in out[0]
    assert out[0]["provider"] == "claude"


def test_cmd_messages_not_found_exits(monkeypatch, capsys) -> None:
    """Requesting messages for a nonexistent session ID exits with code 1."""
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": []})
    args = _ns(
        session_id="missing",
        provider=None,
        limit=50,
        offset=0,
        summary=False,
        include_tools=False,
        include_thinking=False,
        full=False,
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_messages(args)
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_cmd_messages_summary_and_pagination(monkeypatch, capsys) -> None:
    """--summary filters to user text only; --offset/--limit paginate the result."""
    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    messages = [
        make_message(role="user", content="u1"),
        make_message(role="assistant", content="a1"),
        make_message(role="user", content="", content_type="tool_use", tool_name="Read"),
        make_message(role="user", content="sys", is_system=True),
        make_message(role="user", content="u2"),
    ]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (None, messages))

    args = _ns(
        session_id="s1",
        provider=None,
        limit=1,
        offset=1,
        summary=True,
        include_tools=False,
        include_thinking=False,
        full=False,
    )
    cli.cmd_messages(args)
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 2
    assert out["offset"] == 1
    assert out["limit"] == 1
    assert [m["content"] for m in out["messages"]] == ["u2"]


def test_cmd_search_outputs_json(monkeypatch, capsys) -> None:
    """'sesh search' outputs SearchResult objects as JSON with all expected fields."""
    import sesh.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q, aggregation_root=None, **_kw: [
            SearchResult(
                session_id="s1",
                provider=Provider.CODEX,
                project_path="/repo",
                matched_line="needle",
                file_path="/tmp/x.jsonl",
            )
        ],
    )
    cli.cmd_search(_ns(query="needle"))
    out = json.loads(capsys.readouterr().out)
    assert out == [
        {
            "session_id": "s1",
            "provider": "codex",
            "project_path": "/repo",
            "matched_line": "needle",
            "file_path": "/tmp/x.jsonl",
            "host": None,
            "agent_id": None,
        }
    ]


def test_cmd_search_emits_agent_id(monkeypatch, capsys) -> None:
    """A sub-agent hit surfaces its agent_id in the JSON output."""
    import sesh.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q, aggregation_root=None, **_kw: [
            SearchResult(
                session_id="parent-1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle",
                file_path="/tmp/parent-1/subagents/agent-xyz.jsonl",
                agent_id="xyz",
            )
        ],
    )
    cli.cmd_search(_ns(query="needle"))
    out = json.loads(capsys.readouterr().out)
    assert out[0]["session_id"] == "parent-1"
    assert out[0]["agent_id"] == "xyz"


def test_cmd_search_passes_aggregation_root(monkeypatch, capsys, tmp_path) -> None:
    """'sesh search --aggregation-root <p>' forwards the root and emits host."""
    import sesh.search as search_mod

    captured: dict = {}

    def _fake(q, aggregation_root=None, **_kw):
        captured["query"] = q
        captured["aggregation_root"] = aggregation_root
        return [
            SearchResult(
                session_id="s1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle",
                file_path=str(tmp_path / "laptop" / ".claude" / "a.jsonl"),
                host="laptop",
            )
        ]

    monkeypatch.setattr(search_mod, "ripgrep_search", _fake)
    cli.cmd_search(_ns(query="needle", aggregation_root=tmp_path))

    assert captured["query"] == "needle"
    assert captured["aggregation_root"] == tmp_path

    out = json.loads(capsys.readouterr().out)
    assert out == [
        {
            "session_id": "s1",
            "provider": "claude",
            "project_path": "/repo",
            "matched_line": "needle",
            "file_path": str(tmp_path / "laptop" / ".claude" / "a.jsonl"),
            "host": "laptop",
            "agent_id": None,
        }
    ]


def test_cmd_clean_empty_results(monkeypatch, capsys) -> None:
    """'sesh clean' with no matches reports zero deletions."""
    import sesh.search as search_mod

    monkeypatch.setattr(search_mod, "ripgrep_search", lambda q, **_kw: [])
    cli.cmd_clean(_ns(query="needle", dry_run=False, force=True))
    out = json.loads(capsys.readouterr().out)
    assert out == {"deleted": [], "total": 0, "dry_run": False}


def test_cmd_clean_dry_run(monkeypatch, capsys) -> None:
    """'sesh clean --dry-run' reports what would be deleted without calling delete_session."""
    import sesh.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q, **_kw: [
            SearchResult(
                session_id="s1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle",
                file_path="/tmp/claude-proj/a.jsonl",
            )
        ],
    )
    cli.cmd_clean(_ns(query="needle", dry_run=True, force=False))
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["total"] == 1
    assert out["deleted"][0]["provider"] == "claude"


def test_cmd_clean_dedup_regression(monkeypatch, capsys) -> None:
    """Same Claude session matched in two JSONL files is deleted only once.

    Regression: cmd_clean iterated raw search results without session-level
    dedup, so a session matching in multiple JSONL files triggered duplicate deletes.
    """
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.copilot as copilot_mod
    import sesh.providers.cursor as cursor_mod
    import sesh.search as search_mod

    deleted_ids: list[tuple[str, str]] = []

    class FakeClaudeProvider:
        def delete_session(self, session):
            deleted_ids.append((session.id, session.source_path or ""))

    class NoopProvider:
        def delete_session(self, session):
            deleted_ids.append((session.id, session.source_path or ""))

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q, **_kw: [
            SearchResult(
                session_id="sess-1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle one",
                file_path="/tmp/claude-proj/one.jsonl",
            ),
            SearchResult(
                session_id="sess-1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle two",
                file_path="/tmp/claude-proj/two.jsonl",
            ),
        ],
    )
    monkeypatch.setattr(claude_mod, "ClaudeProvider", FakeClaudeProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", NoopProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", NoopProvider)
    monkeypatch.setattr(copilot_mod, "CopilotProvider", NoopProvider)

    cli.cmd_clean(_ns(query="needle", dry_run=False, force=True))
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 1
    assert len(out["deleted"]) == 1
    assert deleted_ids == [("sess-1", "/tmp/claude-proj")]


def test_cmd_clean_collects_errors(monkeypatch, capsys) -> None:
    """Provider exceptions during clean are captured in the 'errors' list, not raised."""
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.copilot as copilot_mod
    import sesh.providers.cursor as cursor_mod
    import sesh.search as search_mod

    class BoomProvider:
        def delete_session(self, session):
            raise RuntimeError("fail")

    class NoopProvider:
        def delete_session(self, session):
            return None

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q, **_kw: [
            SearchResult(
                session_id="s1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle",
                file_path="/tmp/claude-proj/a.jsonl",
            )
        ],
    )
    monkeypatch.setattr(claude_mod, "ClaudeProvider", BoomProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", NoopProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", NoopProvider)
    monkeypatch.setattr(copilot_mod, "CopilotProvider", NoopProvider)

    cli.cmd_clean(_ns(query="needle", dry_run=False, force=True))
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 0
    assert out["errors"][0]["error"] == "fail"


def test_cmd_resume_binary_missing(monkeypatch, capsys) -> None:
    """'sesh resume' exits with an error when the provider's CLI binary isn't on PATH."""
    index = {
        "sessions": [
            _session_dict(
                id="s1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                source_path="/tmp/a",
            )
        ]
    }
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: index)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_resume(_ns(session_id="s1", provider=None))
    assert exc.value.code == 1
    assert "not found on PATH" in capsys.readouterr().err


def test_cmd_resume_cursor_txt_refusal(monkeypatch, capsys) -> None:
    """Cursor .txt transcript sessions cannot be resumed (no session ID in Cursor's format)."""
    index = {
        "sessions": [
            _session_dict(
                id="s1",
                provider=Provider.CURSOR,
                project_path="/repo",
                source_path="/tmp/session.txt",
            )
        ]
    }
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: index)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_resume(_ns(session_id="s1", provider=None))
    assert exc.value.code == 1
    assert "cannot be resumed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("provider", "expected_binary", "expected_args"),
    [
        ("claude", "claude", ["claude", "--resume", "s1"]),
        ("codex", "codex", ["codex", "resume", "s1"]),
        ("cursor", "agent", ["agent", "--resume=s1"]),
        ("copilot", "copilot", ["copilot", "--resume=s1"]),
    ],
)
def test_cmd_resume_execvp_args_and_chdir(
    provider, expected_binary, expected_args, monkeypatch
) -> None:
    """Correct CLI args and chdir-to-project for each provider's resume command."""
    index = {
        "sessions": [
            _session_dict(
                id="s1",
                provider=Provider(provider),
                project_path="/repo",
                source_path="/tmp/session.db",
            )
        ]
    }
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: index)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")

    calls = {}

    def fake_chdir(path):
        calls["chdir"] = path

    def fake_execvp(binary_path, args):
        calls["execvp"] = (binary_path, args)
        raise RuntimeError("exec")

    monkeypatch.setattr(cli.os, "chdir", fake_chdir)
    monkeypatch.setattr(cli.os, "execvp", fake_execvp)

    with pytest.raises(RuntimeError, match="exec"):
        cli.cmd_resume(_ns(session_id="s1", provider=None))

    assert calls["chdir"] == "/repo"
    assert calls["execvp"] == (f"/bin/{expected_binary}", expected_args)


def test_cmd_export_json_format(monkeypatch, capsys) -> None:
    """JSON export includes session metadata and messages with tool fields."""
    session = make_session(
        id="s1",
        provider=Provider.CLAUDE,
        project_path="/repo",
        model="claude-sonnet",
        timestamp=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    messages = [
        make_message(role="user", content="hello"),
        make_message(
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="Read",
            tool_input='{"path":"x"}',
        ),
    ]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    cli.cmd_export(
        _ns(
            session_id="s1",
            provider=None,
            output_format="json",
            include_tools=True,
            include_thinking=False,
            full=False,
        )
    )
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "s1"
    assert out["provider"] == "claude"
    assert len(out["messages"]) == 2
    assert out["messages"][1]["tool_name"] == "Read"


def test_cmd_export_json_carries_subagent_workflow_id(monkeypatch, capsys) -> None:
    """JSON export includes workflow_id on each sub-agent entry."""
    from sesh.models import SubagentMeta

    session = make_session(id="s1", provider=Provider.CLAUDE, project_path="/repo")
    meta = SubagentMeta(
        agent_id="w1",
        file_path="/repo/s1/subagents/workflows/wf_a1be27ca-98b/agent-w1.jsonl",
        workflow_id="wf_a1be27ca-98b",
    )
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, []))
    monkeypatch.setattr(
        cli, "_resolve_subagents", lambda *a, **k: [(meta, [make_message(role="user", content="hi")])]
    )

    cli.cmd_export(
        _ns(
            session_id="s1",
            provider=None,
            output_format="json",
            include_tools=False,
            include_thinking=False,
            full=False,
            no_agents=False,
        )
    )
    out = json.loads(capsys.readouterr().out)
    assert out["subagents"][0]["workflow_id"] == "wf_a1be27ca-98b"


def test_cmd_export_markdown_format(monkeypatch, capsys) -> None:
    """Markdown export renders headings, blockquoted thinking, and fenced tool input."""
    session = make_session(
        id="s1",
        provider=Provider.CODEX,
        project_path="/repo",
        model="gpt-4.1",
        timestamp=datetime(2025, 1, 1, 12, 34, tzinfo=timezone.utc),
    )
    messages = [
        make_message(role="user", content="hello", timestamp=None),
        make_message(
            role="assistant",
            content="",
            content_type="thinking",
            thinking="thinking line",
            timestamp=None,
        ),
        make_message(
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="shell",
            tool_input='{"cmd":"ls"}',
            timestamp=None,
        ),
    ]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    cli.cmd_export(
        _ns(
            session_id="s1",
            provider=None,
            output_format="md",
            include_tools=True,
            include_thinking=True,
            full=False,
        )
    )
    out = capsys.readouterr().out
    assert "# Session: s1" in out
    assert "- **Provider:** codex" in out
    assert "### Thinking" in out
    assert "> thinking line" in out
    assert "```json" in out


def test_cmd_move_expands_paths_and_outputs_json(monkeypatch, capsys) -> None:
    """'sesh move' expands ~ and relative paths before passing to move_project."""
    import sesh.move as move_mod

    calls = {}

    def fake_expanduser(p):
        return p.replace("~", "/home/test")

    def fake_abspath(p):
        return f"/abs{p}" if not p.startswith("/abs") else p

    def fake_move_project(**kwargs):
        calls.update(kwargs)
        return [MoveReport(provider=Provider.CLAUDE, success=True, files_modified=1)]

    from sesh.models import MoveReport

    monkeypatch.setattr(cli.os.path, "expanduser", fake_expanduser)
    monkeypatch.setattr(cli.os.path, "abspath", fake_abspath)
    monkeypatch.setattr(move_mod, "move_project", fake_move_project)

    cli.cmd_move(_ns(old_path="~/old", new_path="./new", metadata_only=False, dry_run=True))
    out = json.loads(capsys.readouterr().out)

    assert calls == {
        "old_path": "/abs/home/test/old",
        "new_path": "/abs./new",
        "full_move": True,
        "dry_run": True,
    }
    assert out["old_path"] == "/abs/home/test/old"
    assert out["new_path"] == "/abs./new"
    assert out["dry_run"] is True
    assert out["reports"][0]["provider"] == "claude"


def test_cmd_move_error_propagation(monkeypatch, capsys) -> None:
    """move_project raising ValueError causes exit(1) with the error message on stderr."""
    import sesh.move as move_mod

    monkeypatch.setattr(move_mod, "move_project", lambda **kwargs: (_ for _ in ()).throw(ValueError("bad move")))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_move(_ns(old_path="/old", new_path="/new", metadata_only=False, dry_run=False))
    assert exc.value.code == 1
    assert "bad move" in capsys.readouterr().err


# --- cmd_delete tests ---


def test_cmd_delete_not_found(monkeypatch, capsys) -> None:
    """Missing session ID exits with code 1."""
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": []})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(_ns(session_id="missing", provider=None, force=True, dry_run=False))
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_cmd_delete_ambiguous(monkeypatch, capsys) -> None:
    """Same ID in multiple providers without --provider exits with code 1."""
    index = {
        "sessions": [
            _session_dict(id="dup", provider=Provider.CLAUDE),
            _session_dict(id="dup", provider=Provider.CODEX),
        ]
    }
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(_ns(session_id="dup", provider=None, force=True, dry_run=False))
    assert exc.value.code == 1
    assert "multiple providers" in capsys.readouterr().err


def test_cmd_delete_ambiguous_resolved_by_provider(monkeypatch, capsys) -> None:
    """--provider disambiguates when same ID exists in multiple providers."""
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.copilot as copilot_mod
    import sesh.providers.cursor as cursor_mod

    deleted_ids = []

    class FakeProvider:
        def delete_session(self, session):
            deleted_ids.append(session.id)

    index = {
        "sessions": [
            _session_dict(id="dup", provider=Provider.CLAUDE),
            _session_dict(id="dup", provider=Provider.CODEX),
        ]
    }
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(claude_mod, "ClaudeProvider", FakeProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", FakeProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", FakeProvider)
    monkeypatch.setattr(copilot_mod, "CopilotProvider", FakeProvider)

    cli.cmd_delete(_ns(session_id="dup", provider="claude", force=True, dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"]["session_id"] == "dup"
    assert out["deleted"]["provider"] == "claude"
    assert deleted_ids == ["dup"]


def test_cmd_delete_dry_run(monkeypatch, capsys) -> None:
    """--dry-run reports what would be deleted without calling delete_session."""
    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)

    cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=True))
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["would_delete"]["session_id"] == "s1"


def test_cmd_delete_non_tty_no_force(monkeypatch, capsys) -> None:
    """Non-interactive mode without --force refuses to delete."""
    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))
    assert exc.value.code == 1
    assert "non-interactive" in capsys.readouterr().err


def test_cmd_delete_non_tty_with_force(monkeypatch, capsys) -> None:
    """Non-interactive mode with --force deletes successfully."""
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.copilot as copilot_mod
    import sesh.providers.cursor as cursor_mod

    deleted_ids = []

    class FakeProvider:
        def delete_session(self, session):
            deleted_ids.append(session.id)

    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(claude_mod, "ClaudeProvider", FakeProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", FakeProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", FakeProvider)
    monkeypatch.setattr(copilot_mod, "CopilotProvider", FakeProvider)

    cli.cmd_delete(_ns(session_id="s1", provider=None, force=True, dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"]["session_id"] == "s1"
    assert deleted_ids == ["s1"]


def test_cmd_delete_tty_confirms(monkeypatch, capsys) -> None:
    """Interactive mode with 'y' confirmation deletes successfully."""
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.copilot as copilot_mod
    import sesh.providers.cursor as cursor_mod

    deleted_ids = []

    class FakeProvider:
        def delete_session(self, session):
            deleted_ids.append(session.id)

    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(claude_mod, "ClaudeProvider", FakeProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", FakeProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", FakeProvider)
    monkeypatch.setattr(copilot_mod, "CopilotProvider", FakeProvider)

    cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"]["session_id"] == "s1"
    assert deleted_ids == ["s1"]


def test_cmd_delete_tty_declines(monkeypatch, capsys) -> None:
    """Interactive mode with 'n' aborts."""
    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))
    assert exc.value.code == 1
    assert "Aborted" in capsys.readouterr().err


def test_cmd_delete_provider_error(monkeypatch, capsys) -> None:
    """Provider exception during delete exits with code 1."""
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.copilot as copilot_mod
    import sesh.providers.cursor as cursor_mod

    class BoomProvider:
        def delete_session(self, session):
            raise RuntimeError("disk error")

    class NoopProvider:
        def delete_session(self, session):
            return None

    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(claude_mod, "ClaudeProvider", BoomProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", NoopProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", NoopProvider)
    monkeypatch.setattr(copilot_mod, "CopilotProvider", NoopProvider)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(_ns(session_id="s1", provider=None, force=True, dry_run=False))
    assert exc.value.code == 1
    assert "disk error" in capsys.readouterr().err


def test_cmd_delete_eof_aborts(monkeypatch, capsys) -> None:
    """EOFError during confirmation prompt aborts."""
    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))
    assert exc.value.code == 1
    assert "Aborted" in capsys.readouterr().err


# --- cmd_delete 'last' tests ---


def test_cmd_delete_last_picks_most_recent(monkeypatch, capsys) -> None:
    """'sesh delete last' selects the session with the newest timestamp."""
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.copilot as copilot_mod
    import sesh.providers.cursor as cursor_mod

    deleted_ids = []

    class FakeProvider:
        def delete_session(self, session):
            deleted_ids.append(session.id)

    index = {
        "sessions": [
            _session_dict(
                id="old",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            _session_dict(
                id="newest",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
            ),
            _session_dict(
                id="mid",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 3, 1, tzinfo=timezone.utc),
            ),
        ]
    }
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(claude_mod, "ClaudeProvider", FakeProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", FakeProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", FakeProvider)
    monkeypatch.setattr(copilot_mod, "CopilotProvider", FakeProvider)

    cli.cmd_delete(_ns(session_id="last", provider=None, force=True, dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"]["session_id"] == "newest"
    assert deleted_ids == ["newest"]


def test_cmd_delete_last_scoped_to_provider(monkeypatch, capsys) -> None:
    """'sesh delete last --provider' picks the newest within that provider."""
    index = {
        "sessions": [
            _session_dict(
                id="claude-new",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
            ),
            _session_dict(
                id="codex-old",
                provider=Provider.CODEX,
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
        ]
    }
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)

    cli.cmd_delete(_ns(session_id="last", provider="codex", force=False, dry_run=True))
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["would_delete"]["session_id"] == "codex-old"


def test_cmd_delete_last_empty_index(monkeypatch, capsys) -> None:
    """'sesh delete last' with no sessions exits with code 1."""
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": []})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(_ns(session_id="last", provider=None, force=True, dry_run=False))
    assert exc.value.code == 1
    assert "No sessions found" in capsys.readouterr().err


def test_cmd_delete_last_empty_for_provider(monkeypatch, capsys) -> None:
    """'sesh delete last --provider' with no matching sessions names the provider."""
    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(_ns(session_id="last", provider="pi", force=True, dry_run=False))
    assert exc.value.code == 1
    assert "provider 'pi'" in capsys.readouterr().err


# --- cmd_clean TTY guard tests ---


def test_cmd_clean_non_tty_no_force_refuses(monkeypatch, capsys) -> None:
    """'sesh clean' in non-interactive mode without --force refuses."""
    import sesh.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q, **_kw: [
            SearchResult(
                session_id="s1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle",
                file_path="/tmp/claude-proj/a.jsonl",
            )
        ],
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_clean(_ns(query="needle", dry_run=False, force=False))
    assert exc.value.code == 1
    assert "non-interactive" in capsys.readouterr().err


def test_cmd_clean_non_tty_with_force_succeeds(monkeypatch, capsys) -> None:
    """'sesh clean --force' in non-interactive mode deletes successfully."""
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.copilot as copilot_mod
    import sesh.providers.cursor as cursor_mod
    import sesh.search as search_mod

    deleted_ids = []

    class FakeProvider:
        def delete_session(self, session):
            deleted_ids.append(session.id)

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q, **_kw: [
            SearchResult(
                session_id="s1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle",
                file_path="/tmp/claude-proj/a.jsonl",
            )
        ],
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(claude_mod, "ClaudeProvider", FakeProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", FakeProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", FakeProvider)

    cli.cmd_clean(_ns(query="needle", dry_run=False, force=True))
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 1
    assert deleted_ids == ["s1"]


def test_cmd_clean_dry_run_skips_confirmation(monkeypatch, capsys) -> None:
    """'sesh clean --dry-run' works without TTY or --force (no mutation)."""
    import sesh.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q, **_kw: [
            SearchResult(
                session_id="s1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle",
                file_path="/tmp/claude-proj/a.jsonl",
            )
        ],
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    cli.cmd_clean(_ns(query="needle", dry_run=True, force=False))
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["total"] == 1


# --- 'last' as session ID for messages/resume/export ---


def test_cmd_messages_last_picks_most_recent(monkeypatch, capsys) -> None:
    """'sesh messages last' loads the session with the newest timestamp."""
    index = {
        "sessions": [
            _session_dict(
                id="old",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            _session_dict(
                id="newest",
                provider=Provider.CODEX,
                timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
            ),
        ]
    }
    seen = {}

    def fake_load(session_data, args=None):
        seen["id"] = session_data["id"]
        return None, [make_message(role="user", content="hi")]

    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(cli, "_load_session_messages", fake_load)

    cli.cmd_messages(
        _ns(
            session_id="last",
            provider=None,
            limit=50,
            offset=0,
            summary=False,
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )
    out = json.loads(capsys.readouterr().out)
    assert seen["id"] == "newest"
    assert out["total"] == 1


def test_cmd_messages_last_scoped_to_provider(monkeypatch, capsys) -> None:
    """'sesh messages last --provider' picks the newest within that provider."""
    index = {
        "sessions": [
            _session_dict(
                id="claude-new",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
            ),
            _session_dict(
                id="codex-old",
                provider=Provider.CODEX,
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
        ]
    }
    seen = {}

    def fake_load(session_data, args=None):
        seen["id"] = session_data["id"]
        return None, []

    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(cli, "_load_session_messages", fake_load)

    cli.cmd_messages(
        _ns(
            session_id="last",
            provider="codex",
            limit=50,
            offset=0,
            summary=False,
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )
    capsys.readouterr()
    assert seen["id"] == "codex-old"


def test_cmd_messages_last_empty_index_exits(monkeypatch, capsys) -> None:
    """'sesh messages last' with an empty index exits with code 1."""
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": []})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_messages(
            _ns(
                session_id="last",
                provider=None,
                limit=50,
                offset=0,
                summary=False,
                include_tools=False,
                include_thinking=False,
                full=False,
            )
        )
    assert exc.value.code == 1
    assert "No sessions found" in capsys.readouterr().err


def test_cmd_messages_refreshes_index_before_resolving(monkeypatch, capsys) -> None:
    """'sesh messages' discovers fresh so a just-created session needs no refresh."""
    refreshed = {"called": False}

    def fake_refresh(*a, **k):
        refreshed["called"] = True
        return {"sessions": [_session_dict(id="brandnew", provider=Provider.CLAUDE)]}

    def fail_require(*a, **k):
        raise AssertionError("cmd_messages must not use the stale disk index")

    monkeypatch.setattr(cli, "_refresh_index", fake_refresh)
    monkeypatch.setattr(cli, "_require_index", fail_require)
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (None, []))

    cli.cmd_messages(
        _ns(
            session_id="brandnew",
            provider=None,
            limit=50,
            offset=0,
            summary=False,
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )
    assert refreshed["called"] is True


def test_cmd_resume_last_picks_most_recent(monkeypatch) -> None:
    """'sesh resume last' execs the provider CLI for the newest session."""
    index = {
        "sessions": [
            _session_dict(
                id="old",
                provider=Provider.CLAUDE,
                project_path="/repo-old",
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            _session_dict(
                id="newest",
                provider=Provider.CLAUDE,
                project_path="/repo-new",
                timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
            ),
        ]
    }
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: index)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")

    calls = {}
    monkeypatch.setattr(cli.os, "chdir", lambda path: calls.__setitem__("chdir", path))

    def fake_execvp(binary_path, args):
        calls["execvp"] = (binary_path, args)
        raise RuntimeError("exec")

    monkeypatch.setattr(cli.os, "execvp", fake_execvp)

    with pytest.raises(RuntimeError, match="exec"):
        cli.cmd_resume(_ns(session_id="last", provider=None))

    assert calls["chdir"] == "/repo-new"
    assert calls["execvp"] == ("/bin/claude", ["claude", "--resume", "newest"])


def test_cmd_export_last_picks_most_recent(monkeypatch, capsys) -> None:
    """'sesh export last' exports the session with the newest timestamp."""
    index = {
        "sessions": [
            _session_dict(
                id="old",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            _session_dict(
                id="newest",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
            ),
        ]
    }

    def fake_load(session_data, args=None):
        return make_session(id=session_data["id"]), []

    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(cli, "_load_session_messages", fake_load)

    cli.cmd_export(
        _ns(
            session_id="last",
            provider=None,
            output_format="json",
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "newest"


# --- export --output ---


def test_cmd_export_output_writes_markdown_file(monkeypatch, capsys, tmp_path) -> None:
    """'sesh export -o FILE' writes Markdown to the file and prints a JSON confirmation."""
    session = make_session(id="s1", provider=Provider.CLAUDE)
    messages = [make_message(role="user", content="hello file")]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    out_file = tmp_path / "nested" / "session.md"
    cli.cmd_export(
        _ns(
            session_id="s1",
            provider=None,
            output_format="md",
            output=str(out_file),
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )

    content = out_file.read_text(encoding="utf-8")
    assert "# Session: s1" in content
    assert "hello file" in content

    out = json.loads(capsys.readouterr().out)
    assert out["exported"]["session_id"] == "s1"
    assert out["exported"]["format"] == "md"
    assert out["exported"]["path"] == str(out_file)
    assert out["exported"]["bytes"] == len(content.encode("utf-8"))


def test_cmd_export_output_writes_json_file(monkeypatch, capsys, tmp_path) -> None:
    """'sesh export --format json -o FILE' writes parseable JSON to the file."""
    session = make_session(id="s1", provider=Provider.CODEX)
    messages = [make_message(role="user", content="hello json")]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    out_file = tmp_path / "session.json"
    cli.cmd_export(
        _ns(
            session_id="s1",
            provider=None,
            output_format="json",
            output=str(out_file),
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["session_id"] == "s1"
    assert data["messages"][0]["content"] == "hello json"

    out = json.loads(capsys.readouterr().out)
    assert out["exported"]["format"] == "json"
    assert out["exported"]["path"] == str(out_file)


def test_cmd_export_output_write_error_exits(monkeypatch, capsys, tmp_path) -> None:
    """An unwritable --output path exits with code 1 and an error on stderr."""
    session = make_session(id="s1")
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, []))

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not dir")
    out_file = blocker / "session.md"

    with pytest.raises(SystemExit) as exc:
        cli.cmd_export(
            _ns(
                session_id="s1",
                provider=None,
                output_format="md",
                output=str(out_file),
                include_tools=False,
                include_thinking=False,
                full=False,
            )
        )
    assert exc.value.code == 1
    assert "Export failed" in capsys.readouterr().err


def test_cmd_export_html_format_writes_file(monkeypatch, capsys, tmp_path) -> None:
    """'sesh export --format html -o FILE' writes a self-contained HTML doc."""
    session = make_session(id="s1", provider=Provider.CLAUDE)
    messages = [make_message(role="user", content="hello html")]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    out_file = tmp_path / "session.html"
    cli.cmd_export(
        _ns(
            session_id="s1",
            provider=None,
            output_format="html",
            output=str(out_file),
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )

    content = out_file.read_text(encoding="utf-8")
    assert "<html" in content
    assert "hello html" in content
    assert "cdn.jsdelivr.net" not in content  # assets inlined, offline

    out = json.loads(capsys.readouterr().out)
    assert out["exported"]["format"] == "html"
    assert out["exported"]["path"] == str(out_file)


def test_cmd_export_refreshes_index_before_resolving(monkeypatch, capsys) -> None:
    """'sesh export' discovers fresh so a just-created session needs no refresh."""
    session = make_session(id="brandnew", provider=Provider.CLAUDE)
    refreshed = {"called": False}

    def fake_refresh(*a, **k):
        refreshed["called"] = True
        return {"sessions": [_session_dict(id="brandnew")]}

    def fail_require(*a, **k):
        raise AssertionError("cmd_export must not use the stale disk index")

    monkeypatch.setattr(cli, "_refresh_index", fake_refresh)
    monkeypatch.setattr(cli, "_require_index", fail_require)
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, []))

    cli.cmd_export(
        _ns(
            session_id="brandnew",
            provider=None,
            output_format="md",
            output=None,
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )
    assert refreshed["called"] is True


def test_cmd_view_no_open_writes_file_and_prints_path(monkeypatch, capsys, tmp_cache_dir) -> None:
    """'sesh view --no-open' writes a stable HTML file and prints its path."""
    session = make_session(id="abcd1234ef", provider=Provider.CLAUDE)
    messages = [make_message(role="user", content="view me")]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="abcd1234ef")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: opened.append(url))

    cli.cmd_view(
        _ns(
            session_id="abcd1234ef",
            provider=None,
            include_tools=False,
            include_thinking=False,
            full=False,
            no_open=True,
        )
    )

    path = capsys.readouterr().out.strip()
    from pathlib import Path

    name = Path(path).name
    assert name == "abcd1234ef.html"  # stable per-session path, full id

    content = Path(path).read_text(encoding="utf-8")
    assert "<html" in content
    assert "view me" in content
    assert opened == []  # --no-open suppresses the browser


def test_cmd_view_reuses_stable_path_across_runs(monkeypatch, capsys, tmp_cache_dir) -> None:
    """Two views of the same session produce the same path (refresh-in-place)."""
    session = make_session(id="abcd1234ef", provider=Provider.CLAUDE)
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="abcd1234ef")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, []))
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: None)

    ns = lambda: _ns(  # noqa: E731
        session_id="abcd1234ef",
        provider=None,
        include_tools=False,
        include_thinking=False,
        full=False,
        no_open=True,
    )

    cli.cmd_view(ns())
    first = capsys.readouterr().out.strip()
    cli.cmd_view(ns())
    second = capsys.readouterr().out.strip()

    assert first == second


def test_cmd_view_opens_browser_by_default(monkeypatch, capsys, tmp_cache_dir) -> None:
    """'sesh view' opens the rendered file in a browser via a file URI."""
    session = make_session(id="ffff0000aa", provider=Provider.CODEX)
    messages = [make_message(role="user", content="open me")]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="ffff0000aa")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    opened: list[tuple[str, int]] = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=2: opened.append((url, new)))

    cli.cmd_view(
        _ns(
            session_id="ffff0000aa",
            provider=None,
            include_tools=False,
            include_thinking=False,
            full=False,
            no_open=False,
        )
    )

    assert len(opened) == 1
    url, new = opened[0]
    assert url.startswith("file://")
    assert url.endswith("ffff0000aa.html")
    assert new == 0  # new=0 asks the browser to reuse the existing tab


def test_cmd_view_full_includes_tools_and_thinking(monkeypatch, capsys, tmp_cache_dir) -> None:
    """'sesh view --full' renders thinking and tool messages into the page."""
    session = make_session(id="s1", provider=Provider.CLAUDE)
    messages = [
        make_message(role="user", content="hi"),
        make_message(role="assistant", content="", content_type="thinking", thinking="THOUGHT-XYZ"),
        make_message(
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="BashToolName",
            tool_input='{"cmd":"ls"}',
        ),
    ]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: None)

    cli.cmd_view(
        _ns(
            session_id="s1",
            provider=None,
            include_tools=False,
            include_thinking=False,
            full=True,
            no_open=True,
        )
    )

    from pathlib import Path

    content = Path(capsys.readouterr().out.strip()).read_text(encoding="utf-8")
    assert "THOUGHT-XYZ" in content
    assert "BashToolName" in content


def test_cmd_view_write_error_exits(monkeypatch, capsys, tmp_cache_dir) -> None:
    """A failing view-file write exits with code 1 and an error on stderr."""
    from sesh import viewcache

    session = make_session(id="s1", provider=Provider.CLAUDE)
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, []))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(viewcache, "write_view", boom)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_view(
            _ns(
                session_id="s1",
                provider=None,
                include_tools=False,
                include_thinking=False,
                full=False,
                no_open=True,
            )
        )
    assert exc.value.code == 1
    assert "View failed" in capsys.readouterr().err


def test_cmd_view_refreshes_index_before_resolving(monkeypatch, capsys, tmp_cache_dir) -> None:
    """'sesh view' discovers fresh so a just-created session needs no refresh."""
    session = make_session(id="brandnew", provider=Provider.CLAUDE)
    refreshed = {"called": False}

    def fake_refresh(*a, **k):
        refreshed["called"] = True
        return {"sessions": [_session_dict(id="brandnew")]}

    # _require_index (disk-only) would NOT see the new session; assert it's
    # never consulted and that fresh discovery is what resolves the session.
    def fail_require(*a, **k):
        raise AssertionError("cmd_view must not use the stale disk index")

    monkeypatch.setattr(cli, "_refresh_index", fake_refresh)
    monkeypatch.setattr(cli, "_require_index", fail_require)
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, []))
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: None)

    cli.cmd_view(
        _ns(
            session_id="brandnew",
            provider=None,
            include_tools=False,
            include_thinking=False,
            full=False,
            no_open=True,
        )
    )

    assert refreshed["called"] is True


def test_cmd_view_sweeps_stale_files(monkeypatch, capsys, tmp_cache_dir) -> None:
    """'sesh view' GCs an old cached view as a side effect of rendering."""
    import os

    from sesh import viewcache

    session = make_session(id="s1", provider=Provider.CLAUDE)
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, []))
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: None)

    # A stale view from another session, well past the 7-day TTL.
    stale = viewcache.write_view("ancient", "<html></html>")
    old = __import__("time").time() - 30 * 86400
    os.utime(stale, (old, old))

    cli.cmd_view(
        _ns(
            session_id="s1",
            provider=None,
            include_tools=False,
            include_thinking=False,
            full=False,
            no_open=True,
        )
    )

    assert not stale.exists()  # swept during the view


def test_cmd_delete_removes_view_file(monkeypatch, capsys, tmp_cache_dir) -> None:
    """Deleting a session also drops its cached HTML view."""
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
    import sesh.providers.copilot as copilot_mod
    import sesh.providers.cursor as cursor_mod
    from sesh import viewcache

    class FakeProvider:
        def delete_session(self, session):
            pass

    view = viewcache.write_view("s1", "<html></html>")
    assert view.exists()

    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(claude_mod, "ClaudeProvider", FakeProvider)
    monkeypatch.setattr(codex_mod, "CodexProvider", FakeProvider)
    monkeypatch.setattr(cursor_mod, "CursorProvider", FakeProvider)
    monkeypatch.setattr(copilot_mod, "CopilotProvider", FakeProvider)

    cli.cmd_delete(_ns(session_id="s1", provider=None, force=True, dry_run=False))

    assert not view.exists()


# --- sessions --since/--until/--limit ---


def _dated_sessions_index() -> dict:
    return {
        "sessions": [
            _session_dict(
                id="jan",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 1, 15, tzinfo=timezone.utc),
            ),
            _session_dict(
                id="mar",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 3, 15, tzinfo=timezone.utc),
            ),
            _session_dict(
                id="jun",
                provider=Provider.CLAUDE,
                timestamp=datetime(2025, 6, 15, tzinfo=timezone.utc),
            ),
        ]
    }


def test_cmd_sessions_since_until_window(monkeypatch, capsys) -> None:
    """--since/--until keep only sessions inside the (inclusive) window."""
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: _dated_sessions_index())
    cli.cmd_sessions(
        _ns(project=None, provider=None, since="2025-02-01", until="2025-04-01")
    )
    out = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in out] == ["mar"]


def test_cmd_sessions_since_accepts_tz_naive_timestamps(monkeypatch, capsys) -> None:
    """Naive index timestamps compare cleanly against --since (treated as UTC)."""
    index = {
        "sessions": [
            _session_dict(id="naive", provider=Provider.CLAUDE, timestamp=datetime(2025, 6, 15)),
        ]
    }
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: index)
    cli.cmd_sessions(_ns(project=None, provider=None, since="2025-06-01"))
    out = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in out] == ["naive"]


def test_cmd_sessions_limit_sorts_newest_first(monkeypatch, capsys) -> None:
    """--limit sorts by timestamp descending before slicing."""
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: _dated_sessions_index())
    cli.cmd_sessions(_ns(project=None, provider=None, limit=2))
    out = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in out] == ["jun", "mar"]


def test_cmd_sessions_invalid_since_exits(monkeypatch, capsys) -> None:
    """A malformed --since value exits with code 1 and a helpful message."""
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: _dated_sessions_index())
    with pytest.raises(SystemExit) as exc:
        cli.cmd_sessions(_ns(project=None, provider=None, since="not-a-date"))
    assert exc.value.code == 1
    assert "Invalid --since" in capsys.readouterr().err


# --- search --provider/--project filters ---


def test_cmd_search_provider_and_project_filters(monkeypatch, capsys) -> None:
    """'sesh search --provider --project' post-filters the ripgrep results."""
    import sesh.search as search_mod

    results = [
        SearchResult(
            session_id="s1",
            provider=Provider.CLAUDE,
            project_path="/repo-a",
            matched_line="needle a",
            file_path="/tmp/a.jsonl",
        ),
        SearchResult(
            session_id="s2",
            provider=Provider.CODEX,
            project_path="/repo-a",
            matched_line="needle b",
            file_path="/tmp/b.jsonl",
        ),
        SearchResult(
            session_id="s3",
            provider=Provider.CLAUDE,
            project_path="/repo-b",
            matched_line="needle c",
            file_path="/tmp/c.jsonl",
        ),
    ]
    monkeypatch.setattr(search_mod, "ripgrep_search", lambda q, **_kw: results)

    cli.cmd_search(_ns(query="needle", provider="claude", project="/repo-a"))
    out = json.loads(capsys.readouterr().out)
    assert [r["session_id"] for r in out] == ["s1"]

    cli.cmd_search(_ns(query="needle", provider="claude", project=None))
    out = json.loads(capsys.readouterr().out)
    assert [r["session_id"] for r in out] == ["s1", "s3"]


# --- bookmarks ---


def test_cmd_sessions_bookmarked_filters(monkeypatch, capsys) -> None:
    """'sesh sessions --bookmarked' keeps only bookmarked (provider, id) pairs."""
    import sesh.bookmarks as bookmarks_mod

    monkeypatch.delenv("SESH_AGGREGATION_ROOT", raising=False)
    index = {
        "sessions": [
            _session_dict(id="a", provider=Provider.CLAUDE),
            _session_dict(id="b", provider=Provider.CODEX),
            _session_dict(id="a", provider=Provider.CODEX),
        ]
    }
    monkeypatch.setattr(cli, "_require_index", lambda *a, **k: index)
    monkeypatch.setattr(bookmarks_mod, "load_bookmarks", lambda: {("claude", "a")})

    cli.cmd_sessions(_ns(project=None, provider=None, bookmarked=True))
    out = json.loads(capsys.readouterr().out)
    assert [(s["provider"], s["id"]) for s in out] == [("claude", "a")]


def test_cmd_sessions_bookmarked_refused_in_aggregation(monkeypatch, capsys) -> None:
    """'sesh sessions --bookmarked' is refused in aggregation mode."""
    with pytest.raises(SystemExit) as exc:
        cli.cmd_sessions(
            _ns(project=None, provider=None, bookmarked=True, aggregation_root="/agg")
        )
    assert exc.value.code == 1
    assert "aggregation mode" in capsys.readouterr().err


def test_cmd_bookmarks_joins_index(monkeypatch, capsys) -> None:
    """'sesh bookmarks' joins index metadata and flags stale bookmarks."""
    import sesh.bookmarks as bookmarks_mod
    import sesh.cache as cache_mod

    monkeypatch.delenv("SESH_AGGREGATION_ROOT", raising=False)
    monkeypatch.setattr(
        bookmarks_mod,
        "load_bookmarks",
        lambda: {("claude", "s1"), ("codex", "gone")},
    )
    index = {
        "sessions": [
            _session_dict(
                id="s1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                summary="bookmarked session",
            )
        ]
    }
    monkeypatch.setattr(cache_mod, "load_index", lambda: index)

    cli.cmd_bookmarks(_ns())
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 2

    by_id = {(e["provider"], e["session_id"]): e for e in out}
    live = by_id[("claude", "s1")]
    assert live["in_index"] is True
    assert live["project_path"] == "/repo"
    assert live["summary"] == "bookmarked session"

    stale = by_id[("codex", "gone")]
    assert stale == {"session_id": "gone", "provider": "codex", "in_index": False}


def test_cmd_bookmarks_without_index(monkeypatch, capsys) -> None:
    """'sesh bookmarks' still lists raw entries when no index exists."""
    import sesh.bookmarks as bookmarks_mod
    import sesh.cache as cache_mod

    monkeypatch.delenv("SESH_AGGREGATION_ROOT", raising=False)
    monkeypatch.setattr(bookmarks_mod, "load_bookmarks", lambda: {("claude", "s1")})
    monkeypatch.setattr(cache_mod, "load_index", lambda: None)

    cli.cmd_bookmarks(_ns())
    out = json.loads(capsys.readouterr().out)
    assert out == [{"session_id": "s1", "provider": "claude", "in_index": False}]


def test_cmd_bookmarks_refused_in_aggregation(monkeypatch, capsys) -> None:
    """'sesh bookmarks' is refused in aggregation mode."""
    with pytest.raises(SystemExit) as exc:
        cli.cmd_bookmarks(_ns(aggregation_root="/agg"))
    assert exc.value.code == 1
    assert "aggregation mode" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# view/export --file (loose transcript, no index)
# ---------------------------------------------------------------------------
def _write_loose_transcript(path) -> str:
    """Write a minimal Claude transcript .jsonl; return its sessionId."""
    import json as _json

    sid = "loose-sid-1"
    entries = [
        {
            "sessionId": sid,
            "cwd": "/Users/me/proj",
            "timestamp": "2025-01-15T10:00:00Z",
            "message": {"role": "user", "content": "render me"},
        },
        {
            "sessionId": sid,
            "cwd": "/Users/me/proj",
            "timestamp": "2025-01-15T10:01:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(_json.dumps(e) + "\n")
    return sid


def test_cmd_export_file_md_bypasses_index(monkeypatch, capsys, tmp_path) -> None:
    """export --file renders without ever touching the index."""
    loose = tmp_path / "archived.jsonl"
    _write_loose_transcript(loose)

    def fail_refresh(*a, **k):
        raise AssertionError("export --file must not refresh the index")

    monkeypatch.setattr(cli, "_refresh_index", fail_refresh)

    cli.cmd_export(
        _ns(
            session_id=None,
            file=str(loose),
            provider=None,
            output_format="md",
            output=None,
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )
    out = capsys.readouterr().out
    assert "# Session: loose-sid-1" in out
    assert "render me" in out


def test_cmd_export_file_html_to_output(monkeypatch, capsys, tmp_path) -> None:
    loose = tmp_path / "archived.jsonl"
    _write_loose_transcript(loose)
    out_file = tmp_path / "out.html"

    cli.cmd_export(
        _ns(
            session_id=None,
            file=str(loose),
            provider=None,
            output_format="html",
            output=str(out_file),
            include_tools=False,
            include_thinking=False,
            full=False,
        )
    )
    assert "<html" in out_file.read_text(encoding="utf-8")


def test_cmd_view_file_no_open(monkeypatch, capsys, tmp_cache_dir, tmp_path) -> None:
    loose = tmp_path / "archived.jsonl"
    _write_loose_transcript(loose)
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: None)

    cli.cmd_view(
        _ns(
            session_id=None,
            file=str(loose),
            provider=None,
            include_tools=False,
            include_thinking=False,
            full=False,
            no_open=True,
        )
    )
    from pathlib import Path

    path = capsys.readouterr().out.strip()
    assert Path(path).name == "loose-sid-1.html"
    assert "render me" in Path(path).read_text(encoding="utf-8")


def test_cmd_export_file_and_session_id_conflict(capsys, tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.cmd_export(_ns(session_id="s1", file=str(tmp_path / "x.jsonl"), provider=None))
    assert exc.value.code == 1
    assert "not both" in capsys.readouterr().err


def test_cmd_export_neither_id_nor_file(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.cmd_export(_ns(session_id=None, file=None, provider=None))
    assert exc.value.code == 1
    assert "required" in capsys.readouterr().err


def test_cmd_view_file_missing_path(capsys, tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.cmd_view(_ns(session_id=None, file=str(tmp_path / "nope.jsonl"), provider=None))
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().err


def test_cmd_view_file_wrong_suffix(capsys, tmp_path) -> None:
    bad = tmp_path / "notes.md"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_view(_ns(session_id=None, file=str(bad), provider=None))
    assert exc.value.code == 1
    assert ".jsonl" in capsys.readouterr().err


def test_cmd_export_file_nonclaude_provider_rejected(capsys, tmp_path) -> None:
    loose = tmp_path / "archived.jsonl"
    _write_loose_transcript(loose)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_export(_ns(session_id=None, file=str(loose), provider="codex"))
    assert exc.value.code == 1
    assert "only Claude" in capsys.readouterr().err


# --- sub-agent (Task/Agent) view/export plumbing -------------------------

def _agent_records(session_id, agent_id, *, user_text="agent kickoff", with_tool=False):
    recs = [
        {
            "sessionId": session_id,
            "agentId": agent_id,
            "isSidechain": True,
            "timestamp": "2025-01-01T00:30:00Z",
            "uuid": "au1",
            "parentUuid": None,
            "message": {"role": "user", "content": user_text},
        },
        {
            "sessionId": session_id,
            "agentId": agent_id,
            "isSidechain": True,
            "timestamp": "2025-01-01T00:30:05Z",
            "uuid": "au2",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "nested reply text"}],
                "usage": {"input_tokens": 10, "output_tokens": 7},
            },
        },
    ]
    if with_tool:
        recs.append({
            "sessionId": session_id,
            "agentId": agent_id,
            "isSidechain": True,
            "timestamp": "2025-01-01T00:30:06Z",
            "uuid": "au3",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "InteriorTool",
                     "input": {"q": "x"}},
                ],
            },
        })
    return recs


def _make_subagent_session(tmp_path, *, session_id="sess-A", with_tool=False,
                           sidecar=True, agent_id="ag-1"):
    """Write a current-layout Claude project with one sub-agent; return session."""
    import json as _json

    project_dir = tmp_path / "proj"
    (project_dir).mkdir(parents=True, exist_ok=True)
    # Main session file.
    main = project_dir / "main.jsonl"
    with open(main, "w") as f:
        f.write(_json.dumps({
            "sessionId": session_id,
            "timestamp": "2025-01-01T00:00:00Z",
            "message": {"role": "user", "content": "please investigate"},
        }) + "\n")
    # Current-layout sub-agent.
    agent_file = project_dir / session_id / "subagents" / f"agent-{agent_id}.jsonl"
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    with open(agent_file, "w") as f:
        for rec in _agent_records(session_id, agent_id, with_tool=with_tool):
            f.write(_json.dumps(rec) + "\n")
    if sidecar:
        sc = agent_file.parent / (agent_file.stem + ".meta.json")
        sc.write_text(_json.dumps({
            "agentType": "Explore",
            "isFork": True,
            "description": "Investigate the layout",
            "toolUseId": "toolu_777",
        }))
    session = make_session(
        id=session_id, provider=Provider.CLAUDE, source_path=str(project_dir),
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    main_messages = [
        make_message(role="user", content="please investigate",
                     timestamp=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)),
    ]
    return session, main_messages


def test_cmd_view_includes_subagent_block(monkeypatch, capsys, tmp_cache_dir, tmp_path) -> None:
    """'sesh view' embeds a kind:'agent' payload entry with nested messages."""
    session, messages = _make_subagent_session(tmp_path)
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id=session.id)]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: None)

    cli.cmd_view(_ns(session_id=session.id, provider=None, include_tools=False,
                     include_thinking=False, full=False, no_open=True, no_agents=False))

    from pathlib import Path as _P
    content = _P(capsys.readouterr().out.strip()).read_text(encoding="utf-8")
    assert '"kind": "agent"' in content or '"kind":"agent"' in content
    assert "Investigate the layout" in content
    assert "nested reply text" in content


def test_cmd_view_no_agents_suppresses_block(monkeypatch, capsys, tmp_cache_dir, tmp_path) -> None:
    """--no-agents drops the sub-agent block from the rendered view."""
    session, messages = _make_subagent_session(tmp_path)
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id=session.id)]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: None)

    cli.cmd_view(_ns(session_id=session.id, provider=None, include_tools=False,
                     include_thinking=False, full=False, no_open=True, no_agents=True))

    from pathlib import Path as _P
    content = _P(capsys.readouterr().out.strip()).read_text(encoding="utf-8")
    assert "Investigate the layout" not in content
    assert "nested reply text" not in content


def test_cmd_export_markdown_gains_subagent_section(monkeypatch, capsys, tmp_path) -> None:
    """Markdown export appends a '## Sub-agent:' section."""
    session, messages = _make_subagent_session(tmp_path)
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id=session.id)]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    cli.cmd_export(_ns(session_id=session.id, provider=None, output_format="md",
                       output=None, include_tools=False, include_thinking=False,
                       full=False, no_agents=False))
    out = capsys.readouterr().out
    assert "## Sub-agent: Investigate the layout (ag-1)" in out
    assert "**Type:** Explore" in out
    assert "nested reply text" in out


def test_cmd_export_json_gains_subagents_array(monkeypatch, capsys, tmp_path) -> None:
    """JSON export gains a 'subagents' array with per-agent metadata + messages."""
    session, messages = _make_subagent_session(tmp_path)
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id=session.id)]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    cli.cmd_export(_ns(session_id=session.id, provider=None, output_format="json",
                       output=None, include_tools=False, include_thinking=False,
                       full=False, no_agents=False))
    out = json.loads(capsys.readouterr().out)
    assert "subagents" in out
    assert len(out["subagents"]) == 1
    ag = out["subagents"][0]
    assert ag["agent_id"] == "ag-1"
    assert ag["description"] == "Investigate the layout"
    assert ag["agent_type"] == "Explore"
    assert ag["is_fork"] is True
    assert ag["tool_use_id"] == "toolu_777"
    assert ag["message_count"] == 2
    assert any(m["content"] == "nested reply text" for m in ag["messages"])


def test_cmd_export_json_subagent_interior_tools_gated(monkeypatch, capsys, tmp_path) -> None:
    """Interior tool messages appear in the sub-agent only with --include-tools."""
    session, messages = _make_subagent_session(tmp_path, with_tool=True)
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id=session.id)]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    def _run(include_tools):
        cli.cmd_export(_ns(session_id=session.id, provider=None, output_format="json",
                           output=None, include_tools=include_tools,
                           include_thinking=False, full=False, no_agents=False))
        return json.loads(capsys.readouterr().out)

    without = _run(False)
    assert not any(
        m.get("tool_name") == "InteriorTool"
        for m in without["subagents"][0]["messages"]
    )
    withtools = _run(True)
    assert any(
        m.get("tool_name") == "InteriorTool"
        for m in withtools["subagents"][0]["messages"]
    )


def test_cmd_export_json_malformed_agent_file_does_not_crash(
    monkeypatch, capsys, tmp_path
) -> None:
    """[finding 3] A malformed-but-valid-JSON agent line never bricks export."""
    session, messages = _make_subagent_session(tmp_path, sidecar=False)
    # Corrupt the agent file: prepend a bare array (valid JSON, not an object)
    # and a record whose message is a string. Pre-fix these raised AttributeError.
    agent_file = tmp_path / "proj" / session.id / "subagents" / "agent-ag-1.jsonl"
    original = agent_file.read_text()
    agent_file.write_text(
        "[]\n"
        + json.dumps({"sessionId": session.id, "message": "not a dict"}) + "\n"
        + original
    )
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id=session.id)]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    # Must not raise; the well-formed interior still comes through.
    cli.cmd_export(_ns(session_id=session.id, provider=None, output_format="json",
                       output=None, include_tools=False, include_thinking=False,
                       full=False, no_agents=False))
    out = json.loads(capsys.readouterr().out)
    assert len(out["subagents"]) == 1
    assert any(m["content"] == "nested reply text" for m in out["subagents"][0]["messages"])


def test_resolve_subagents_skips_when_loader_raises(monkeypatch) -> None:
    """[finding 3] A load failure is swallowed, returning no sub-agents."""
    session = make_session(id="s-guard", provider=Provider.CLAUDE, source_path="/p")

    class _Boom:
        def load_subagents(self, _session):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "_provider_for_session", lambda *a, **k: _Boom())
    out = cli._resolve_subagents(
        session, _ns(no_agents=False), include_tools=False, include_thinking=False
    )
    assert out == []


def test_cmd_export_json_nonclaude_has_no_subagents(monkeypatch, capsys) -> None:
    """Non-Claude sessions never get a subagents array."""
    session = make_session(id="cx", provider=Provider.CODEX)
    messages = [make_message(role="user", content="hi")]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="cx")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    cli.cmd_export(_ns(session_id="cx", provider=None, output_format="json",
                       output=None, include_tools=False, include_thinking=False,
                       full=False, no_agents=False))
    out = json.loads(capsys.readouterr().out)
    assert "subagents" not in out


def test_cmd_export_json_claude_without_subagents_unchanged(monkeypatch, capsys) -> None:
    """A Claude session with no sub-agent files gets no subagents array."""
    session = make_session(id="c-none", provider=Provider.CLAUDE, source_path=None)
    messages = [make_message(role="user", content="hi")]
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: {"sessions": [_session_dict(id="c-none")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda *a, **k: (session, messages))

    cli.cmd_export(_ns(session_id="c-none", provider=None, output_format="json",
                       output=None, include_tools=False, include_thinking=False,
                       full=False, no_agents=False))
    out = json.loads(capsys.readouterr().out)
    assert "subagents" not in out


def test_cmd_view_file_picks_up_layout_a_subagents(monkeypatch, capsys, tmp_cache_dir, tmp_path) -> None:
    """--file loose path discovers current-layout sub-agents beside the file."""
    import json as _json

    session_id = "loose-A"
    loose = tmp_path / "archived.jsonl"
    with open(loose, "w") as f:
        f.write(_json.dumps({
            "sessionId": session_id,
            "cwd": "/Users/me/proj",
            "timestamp": "2025-01-01T00:00:00Z",
            "message": {"role": "user", "content": "please investigate"},
        }) + "\n")
    agent_file = tmp_path / session_id / "subagents" / "agent-la.jsonl"
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    with open(agent_file, "w") as f:
        for rec in _agent_records(session_id, "la"):
            f.write(_json.dumps(rec) + "\n")
    (agent_file.parent / (agent_file.stem + ".meta.json")).write_text(
        _json.dumps({"agentType": "Task", "description": "Loose sub-agent work"})
    )
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: None)

    cli.cmd_view(_ns(session_id=None, file=str(loose), provider=None,
                     include_tools=False, include_thinking=False, full=False,
                     no_open=True, no_agents=False))

    from pathlib import Path as _P
    content = _P(capsys.readouterr().out.strip()).read_text(encoding="utf-8")
    assert "Loose sub-agent work" in content
    assert "nested reply text" in content
