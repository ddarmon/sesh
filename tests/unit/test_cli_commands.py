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
    import sesh.cache as cache_mod

    monkeypatch.setattr(cache_mod, "load_index", lambda: None)
    with pytest.raises(SystemExit) as exc:
        cli._require_index()
    assert exc.value.code == 1
    assert "Run 'sesh refresh' first" in capsys.readouterr().err


def test_cmd_projects_outputs_projects(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_require_index", lambda: {"projects": [{"path": "/repo"}]})
    cli.cmd_projects(_ns())
    assert json.loads(capsys.readouterr().out) == [{"path": "/repo"}]


def test_cmd_sessions_filters_and_strips_source_path(monkeypatch, capsys) -> None:
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
    monkeypatch.setattr(cli, "_require_index", lambda: index)

    cli.cmd_sessions(_ns(project="/p1", provider="claude"))
    out = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in out] == ["a"]
    assert "source_path" not in out[0]
    assert out[0]["provider"] == "claude"


def test_cmd_messages_not_found_exits(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_require_index", lambda: {"sessions": []})
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
    index = {"sessions": [_session_dict(id="s1", provider=Provider.CLAUDE)]}
    messages = [
        make_message(role="user", content="u1"),
        make_message(role="assistant", content="a1"),
        make_message(role="user", content="", content_type="tool_use", tool_name="Read"),
        make_message(role="user", content="sys", is_system=True),
        make_message(role="user", content="u2"),
    ]
    monkeypatch.setattr(cli, "_require_index", lambda: index)
    monkeypatch.setattr(cli, "_load_session_messages", lambda s: (None, messages))

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
    import sesh.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q: [
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
        }
    ]


def test_cmd_clean_empty_results(monkeypatch, capsys) -> None:
    import sesh.search as search_mod

    monkeypatch.setattr(search_mod, "ripgrep_search", lambda q: [])
    cli.cmd_clean(_ns(query="needle", dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert out == {"deleted": [], "total": 0, "dry_run": False}


def test_cmd_clean_dry_run(monkeypatch, capsys) -> None:
    import sesh.search as search_mod

    monkeypatch.setattr(
        search_mod,
        "ripgrep_search",
        lambda q: [
            SearchResult(
                session_id="s1",
                provider=Provider.CLAUDE,
                project_path="/repo",
                matched_line="needle",
                file_path="/tmp/claude-proj/a.jsonl",
            )
        ],
    )
    cli.cmd_clean(_ns(query="needle", dry_run=True))
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["total"] == 1
    assert out["deleted"][0]["provider"] == "claude"


def test_cmd_clean_dedup_regression(monkeypatch, capsys) -> None:
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
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
        lambda q: [
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

    cli.cmd_clean(_ns(query="needle", dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 1
    assert len(out["deleted"]) == 1
    assert deleted_ids == [("sess-1", "/tmp/claude-proj")]


def test_cmd_clean_collects_errors(monkeypatch, capsys) -> None:
    import sesh.providers.claude as claude_mod
    import sesh.providers.codex as codex_mod
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
        lambda q: [
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

    cli.cmd_clean(_ns(query="needle", dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 0
    assert out["errors"][0]["error"] == "fail"


def test_cmd_resume_binary_missing(monkeypatch, capsys) -> None:
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
    monkeypatch.setattr(cli, "_require_index", lambda: index)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_resume(_ns(session_id="s1", provider=None))
    assert exc.value.code == 1
    assert "not found on PATH" in capsys.readouterr().err


def test_cmd_resume_cursor_txt_refusal(monkeypatch, capsys) -> None:
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
    monkeypatch.setattr(cli, "_require_index", lambda: index)
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
    ],
)
def test_cmd_resume_execvp_args_and_chdir(
    provider, expected_binary, expected_args, monkeypatch
) -> None:
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
    monkeypatch.setattr(cli, "_require_index", lambda: index)
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
    monkeypatch.setattr(cli, "_require_index", lambda: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda _s: (session, messages))

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


def test_cmd_export_markdown_format(monkeypatch, capsys) -> None:
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
    monkeypatch.setattr(cli, "_require_index", lambda: {"sessions": [_session_dict(id="s1")]})
    monkeypatch.setattr(cli, "_load_session_messages", lambda _s: (session, messages))

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
    import sesh.move as move_mod

    monkeypatch.setattr(move_mod, "move_project", lambda **kwargs: (_ for _ in ()).throw(ValueError("bad move")))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_move(_ns(old_path="/old", new_path="/new", metadata_only=False, dry_run=False))
    assert exc.value.code == 1
    assert "bad move" in capsys.readouterr().err

