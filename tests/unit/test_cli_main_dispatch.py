from __future__ import annotations

import sys
from types import ModuleType

from sesh import cli


def test_no_subcommand_calls_tui_main(monkeypatch) -> None:
    """'sesh' with no subcommand launches the TUI."""
    calls = {"tui": 0}
    fake_app = ModuleType("sesh.app")
    fake_app.tui_main = lambda *a, **k: calls.__setitem__("tui", calls["tui"] + 1)
    monkeypatch.setitem(sys.modules, "sesh.app", fake_app)
    monkeypatch.setattr(sys, "argv", ["sesh"])

    cli.main()
    assert calls["tui"] == 1


def test_refresh_dispatches(monkeypatch) -> None:
    """'sesh refresh' dispatches to cmd_refresh."""
    calls = {"refresh": 0}
    monkeypatch.setattr(cli, "cmd_refresh", lambda args: calls.__setitem__("refresh", calls["refresh"] + 1))
    monkeypatch.setattr(sys, "argv", ["sesh", "refresh"])

    cli.main()
    assert calls["refresh"] == 1


def test_parser_wiring_smoke(monkeypatch) -> None:
    """'sesh projects' dispatches to cmd_projects (smoke test for subcommand wiring)."""
    called = {"cmd": None}
    monkeypatch.setattr(cli, "cmd_projects", lambda args: called.__setitem__("cmd", "projects"))
    monkeypatch.setattr(sys, "argv", ["sesh", "projects"])
    cli.main()
    assert called["cmd"] == "projects"


def test_sessions_args_project_provider(monkeypatch) -> None:
    """--project and --provider arguments are parsed and passed through to cmd_sessions."""
    seen = {}

    def fake_cmd_sessions(args):
        seen["project"] = args.project
        seen["provider"] = args.provider

    monkeypatch.setattr(cli, "cmd_sessions", fake_cmd_sessions)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sesh", "sessions", "--project", "/repo", "--provider", "cursor"],
    )

    cli.main()
    assert seen == {"project": "/repo", "provider": "cursor"}


def test_sessions_filter_flags_parsed(monkeypatch) -> None:
    """--since/--until/--limit/--bookmarked are parsed and passed to cmd_sessions."""
    seen = {}

    def fake_cmd_sessions(args):
        seen["since"] = args.since
        seen["until"] = args.until
        seen["limit"] = args.limit
        seen["bookmarked"] = args.bookmarked

    monkeypatch.setattr(cli, "cmd_sessions", fake_cmd_sessions)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sesh", "sessions",
            "--since", "2026-06-01",
            "--until", "2026-06-10",
            "--limit", "5",
            "--bookmarked",
        ],
    )

    cli.main()
    assert seen == {
        "since": "2026-06-01",
        "until": "2026-06-10",
        "limit": 5,
        "bookmarked": True,
    }


def test_search_filter_flags_parsed(monkeypatch) -> None:
    """--provider and --project are parsed and passed to cmd_search."""
    seen = {}

    def fake_cmd_search(args):
        seen["query"] = args.query
        seen["provider"] = args.provider
        seen["project"] = args.project

    monkeypatch.setattr(cli, "cmd_search", fake_cmd_search)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sesh", "search", "needle", "--provider", "claude", "--project", "/repo"],
    )

    cli.main()
    assert seen == {"query": "needle", "provider": "claude", "project": "/repo"}


def test_export_output_flag_and_last_parsed(monkeypatch) -> None:
    """'sesh export last -o FILE' parses the output path and literal 'last'."""
    seen = {}

    def fake_cmd_export(args):
        seen["session_id"] = args.session_id
        seen["output"] = args.output

    monkeypatch.setattr(cli, "cmd_export", fake_cmd_export)
    monkeypatch.setattr(sys, "argv", ["sesh", "export", "last", "-o", "/tmp/out.md"])

    cli.main()
    assert seen == {"session_id": "last", "output": "/tmp/out.md"}


def test_bookmarks_dispatches(monkeypatch) -> None:
    """'sesh bookmarks' dispatches to cmd_bookmarks."""
    called = {"cmd": None}
    monkeypatch.setattr(cli, "cmd_bookmarks", lambda args: called.__setitem__("cmd", "bookmarks"))
    monkeypatch.setattr(sys, "argv", ["sesh", "bookmarks"])

    cli.main()
    assert called["cmd"] == "bookmarks"


def test_stats_args_project_provider(monkeypatch) -> None:
    """'sesh stats' dispatches to cmd_stats with --project/--provider parsed."""
    seen = {}

    def fake_cmd_stats(args):
        seen["project"] = args.project
        seen["provider"] = args.provider

    monkeypatch.setattr(cli, "cmd_stats", fake_cmd_stats)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sesh", "stats", "--project", "/repo", "--provider", "claude"],
    )

    cli.main()
    assert seen == {"project": "/repo", "provider": "claude"}
