from __future__ import annotations

import argparse
import json

import pytest

from sesh import cli
from sesh.cache import _session_to_dict
from sesh.models import Provider
from sesh.prompt_history import HistoryReport, HistoryWriteError
from tests.helpers import make_session


def _ns(**kwargs):
    return argparse.Namespace(**kwargs)


def _patch_claude(monkeypatch, history_fn):
    import sesh.providers.claude as claude_mod

    calls: list[tuple[str, str, bool]] = []

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            pass

        def delete_session(self, session):
            calls.append(("delete", session.id, False))

        def delete_history(self, session, dry_run=False):
            calls.append(("history", session.id, dry_run))
            return history_fn(session, dry_run)

    monkeypatch.setattr(claude_mod, "ClaudeProvider", FakeProvider)
    index = {"sessions": [_session_to_dict(make_session(id="s1", provider=Provider.CLAUDE))]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    return calls


def _report(session, dry_run) -> HistoryReport:
    return HistoryReport(removed={"/h/history.jsonl": 3})


def test_delete_without_flag_leaves_history(monkeypatch, capsys) -> None:
    calls = _patch_claude(monkeypatch, _report)
    cli.cmd_delete(_ns(session_id="s1", provider=None, force=True, dry_run=False))
    out = json.loads(capsys.readouterr().out)
    assert "history" not in out
    assert calls == [("delete", "s1", False)]


def test_delete_with_history(monkeypatch, capsys) -> None:
    calls = _patch_claude(monkeypatch, _report)
    cli.cmd_delete(
        _ns(session_id="s1", provider=None, force=True, dry_run=False, history=True)
    )
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"]["session_id"] == "s1"
    assert out["history"] == {
        "supported": True,
        "removed": {"/h/history.jsonl": 3},
        "files_removed": [],
    }
    # History is only touched after the session itself is gone.
    assert calls == [("delete", "s1", False), ("history", "s1", False)]


def test_delete_dry_run_with_history(monkeypatch, capsys) -> None:
    calls = _patch_claude(monkeypatch, _report)
    cli.cmd_delete(
        _ns(session_id="s1", provider=None, force=False, dry_run=True, history=True)
    )
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["history"]["removed"] == {"/h/history.jsonl": 3}
    assert calls == [("history", "s1", True)]


def test_delete_history_failure_exits_nonzero(monkeypatch, capsys) -> None:
    def fail(session, dry_run):
        raise HistoryWriteError("history.jsonl kept changing")

    _patch_claude(monkeypatch, fail)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(
            _ns(session_id="s1", provider=None, force=True, dry_run=False, history=True)
        )
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"]["session_id"] == "s1"
    assert "kept changing" in out["history_error"]


def test_clean_empty_with_history(monkeypatch, capsys) -> None:
    import sesh.viewcache as viewcache

    calls = _patch_claude(monkeypatch, _report)
    index = {"sessions": [
        _session_to_dict(make_session(id="s1", provider=Provider.CLAUDE, is_empty=True))
    ]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(viewcache, "remove_view", lambda session_id: None)

    cli.cmd_clean(
        _ns(query=None, empty=True, dry_run=False, force=True, history=True)
    )
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"][0]["history"]["removed"] == {"/h/history.jsonl": 3}
    assert calls == [("delete", "s1", False), ("history", "s1", False)]


def test_clean_query_dry_run_with_history(monkeypatch, capsys) -> None:
    from sesh.models import SearchResult

    calls = _patch_claude(monkeypatch, _report)
    result = SearchResult(
        session_id="s1",
        project_path="/repo",
        provider=Provider.CLAUDE,
        matched_line="needle",
        file_path="/x/projects/repo/s1.jsonl",
    )
    monkeypatch.setattr("sesh.search.ripgrep_search", lambda *a, **k: [result])
    monkeypatch.setattr(cli, "_build_cwd_lookup", lambda: {})

    cli.cmd_clean(_ns(query="needle", dry_run=True, force=False, history=True))
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"][0]["history"]["removed"] == {"/h/history.jsonl": 3}
    assert calls == [("history", "s1", True)]


@pytest.mark.parametrize(
    ("argv", "command", "expected"),
    [
        (["delete", "s1"], "cmd_delete", False),
        (["delete", "s1", "--history"], "cmd_delete", True),
        (["clean", "--empty"], "cmd_clean", False),
        (["clean", "--empty", "--history"], "cmd_clean", True),
    ],
)
def test_history_flag_defaults_off(monkeypatch, argv, command, expected) -> None:
    import sys

    seen = {}
    monkeypatch.setattr(cli, command, lambda args: seen.setdefault("history", args.history))
    monkeypatch.setattr(sys, "argv", ["sesh", *argv])
    cli.main()
    assert seen["history"] is expected


def _interactive(monkeypatch, answers: list[str]) -> list[str]:
    import sys

    prompts: list[str] = []
    replies = iter(answers)

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def test_interactive_delete_prompts_for_history_yes(monkeypatch, capsys) -> None:
    calls = _patch_claude(monkeypatch, _report)
    prompts = _interactive(monkeypatch, ["y", "y"])

    cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))

    assert len(prompts) == 2
    assert prompts[1] == "Also delete 3 prompt-history entries from /h/history.jsonl? [y/N] "
    out = json.loads(capsys.readouterr().out)
    assert out["history"]["removed"] == {"/h/history.jsonl": 3}
    # Preview before anything is deleted, then delete, then history.
    assert calls == [
        ("history", "s1", True),
        ("delete", "s1", False),
        ("history", "s1", False),
    ]


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_interactive_delete_history_defaults_to_no(monkeypatch, capsys, answer) -> None:
    calls = _patch_claude(monkeypatch, _report)
    _interactive(monkeypatch, ["y", answer])

    cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))

    out = json.loads(capsys.readouterr().out)
    assert "history" not in out
    assert calls == [("history", "s1", True), ("delete", "s1", False)]


def test_interactive_ctrl_c_at_history_prompt_deletes_nothing(monkeypatch, capsys) -> None:
    import sys

    calls = _patch_claude(monkeypatch, _report)
    replies = iter(["y"])

    def fake_input(prompt: str) -> str:
        try:
            return next(replies)
        except StopIteration:
            raise KeyboardInterrupt from None

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))
    assert exc.value.code == 1
    assert ("delete", "s1", False) not in calls


def test_interactive_no_history_prompt_when_nothing_to_remove(monkeypatch, capsys) -> None:
    _patch_claude(monkeypatch, lambda session, dry_run: HistoryReport())
    prompts = _interactive(monkeypatch, ["y"])
    cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))
    assert len(prompts) == 1


def test_interactive_no_history_prompt_when_unsupported(monkeypatch, capsys) -> None:
    _patch_claude(monkeypatch, lambda session, dry_run: HistoryReport(supported=False))
    prompts = _interactive(monkeypatch, ["y"])
    cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))
    assert len(prompts) == 1


def test_history_flag_skips_history_prompt(monkeypatch, capsys) -> None:
    _patch_claude(monkeypatch, _report)
    prompts = _interactive(monkeypatch, ["y"])
    cli.cmd_delete(
        _ns(session_id="s1", provider=None, force=False, dry_run=False, history=True)
    )
    assert len(prompts) == 1
    assert "prompt history will also be removed" in prompts[0]


def test_force_never_prompts_for_history(monkeypatch, capsys) -> None:
    calls = _patch_claude(monkeypatch, _report)
    prompts = _interactive(monkeypatch, [])
    cli.cmd_delete(_ns(session_id="s1", provider=None, force=True, dry_run=False))
    assert prompts == []
    assert calls == [("delete", "s1", False)]


def test_interactive_clean_empty_prompts_once_with_totals(monkeypatch, capsys) -> None:
    import sesh.viewcache as viewcache

    def report(session, dry_run):
        return HistoryReport(
            removed={"/h/history.jsonl": 2}, files_removed=["/h/paste-cache/a.txt"]
        )

    _patch_claude(monkeypatch, report)
    index = {"sessions": [
        _session_to_dict(make_session(id=i, provider=Provider.CLAUDE, is_empty=True))
        for i in ("s1", "s2")
    ]}
    monkeypatch.setattr(cli, "_refresh_index", lambda *a, **k: index)
    monkeypatch.setattr(viewcache, "remove_view", lambda session_id: None)
    prompts = _interactive(monkeypatch, ["y", "y"])

    cli.cmd_clean(_ns(query=None, empty=True, dry_run=False, force=False))

    assert prompts[1] == (
        "Also delete 4 prompt-history entries and 2 paste-cache files "
        "from /h/history.jsonl? [y/N] "
    )
    out = json.loads(capsys.readouterr().out)
    assert all("history" in d for d in out["deleted"])


def test_history_prompt_abbreviates_home(monkeypatch, capsys, tmp_path) -> None:
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = str(tmp_path / ".claude" / "history.jsonl")
    _patch_claude(monkeypatch, lambda session, dry_run: HistoryReport(removed={path: 1}))
    prompts = _interactive(monkeypatch, ["y", "n"])
    cli.cmd_delete(_ns(session_id="s1", provider=None, force=False, dry_run=False))
    assert prompts[1] == "Also delete 1 prompt-history entry from ~/.claude/history.jsonl? [y/N] "


def test_interactive_clean_query_prompts_for_history(monkeypatch, capsys) -> None:
    from sesh.models import SearchResult

    calls = _patch_claude(monkeypatch, _report)
    result = SearchResult(
        session_id="s1",
        project_path="/repo",
        provider=Provider.CLAUDE,
        matched_line="needle",
        file_path="/x/projects/repo/s1.jsonl",
    )
    monkeypatch.setattr("sesh.search.ripgrep_search", lambda *a, **k: [result])
    monkeypatch.setattr(cli, "_build_cwd_lookup", lambda: {})
    monkeypatch.setattr("sesh.viewcache.remove_view", lambda session_id: None)
    prompts = _interactive(monkeypatch, ["y", "y"])

    cli.cmd_clean(_ns(query="needle", dry_run=False, force=False))

    assert len(prompts) == 2
    assert calls == [
        ("history", "s1", True),
        ("delete", "s1", False),
        ("history", "s1", False),
    ]
