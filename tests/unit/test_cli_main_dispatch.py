from __future__ import annotations

import sys
from types import ModuleType

from sesh import cli


def test_no_subcommand_calls_tui_main(monkeypatch) -> None:
    """'sesh' with no subcommand launches the TUI."""
    calls = {"tui": 0}
    fake_app = ModuleType("sesh.app")
    fake_app.tui_main = lambda: calls.__setitem__("tui", calls["tui"] + 1)
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
