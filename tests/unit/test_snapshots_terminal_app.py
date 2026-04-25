from __future__ import annotations

import platform

import pytest

from sesh.snapshots import core as snapshots_core
from sesh.snapshots import terminal_app as ta_mod
from sesh.snapshots.backend import RestoreOutcome
from sesh.snapshots.core import RestoreItem
from sesh.snapshots.terminal_app import TerminalAppBackend


SAMPLE_RAW = """\
<<<TAB>>>
WINDOW: 1
TAB: 1
TTY: /dev/ttys001
<<<HISTORY>>>
$ claude --resume abc-123
some output line one
some output line two
<<<END>>>
<<<TAB>>>
WINDOW: 1
TAB: 2
TTY: /dev/ttys002
<<<HISTORY>>>
$ ls
README.md  src
<<<END>>>
"""


def test_is_supported_matches_platform() -> None:
    backend = TerminalAppBackend()
    assert backend.is_supported() is (platform.system() == "Darwin")


def test_parse_capture_extracts_window_tab_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub _resolve_cwd so this runs anywhere.
    monkeypatch.setattr(TerminalAppBackend, "_resolve_cwd", staticmethod(lambda tty: f"/cwd/{tty}"))

    tabs = TerminalAppBackend._parse_capture(SAMPLE_RAW)
    assert len(tabs) == 2
    assert tabs[0].window == 1
    assert tabs[0].tab == 1
    assert tabs[0].tty == "/dev/ttys001"
    assert tabs[0].cwd == "/cwd//dev/ttys001"
    assert "claude --resume abc-123" in tabs[0].scrollback_tail
    assert tabs[1].tab == 2


def test_parse_capture_handles_empty_input() -> None:
    assert TerminalAppBackend._parse_capture("") == []


def test_capture_uses_run_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TerminalAppBackend, "_run_osascript", staticmethod(lambda script, args=None: SAMPLE_RAW))
    monkeypatch.setattr(TerminalAppBackend, "_resolve_cwd", staticmethod(lambda tty: "/tmp/proj"))

    tabs = TerminalAppBackend().capture()
    assert len(tabs) == 2
    assert all(t.cwd == "/tmp/proj" for t in tabs)


def test_compose_command_with_resume() -> None:
    item = RestoreItem(
        window=1,
        tab=1,
        cwd="/tmp/proj",
        cmd_args=["claude", "--resume", "abc-123"],
        label="x",
    )
    cmd = ta_mod._compose_command(item)
    assert cmd == "cd '/tmp/proj' && claude --resume abc-123"


def test_compose_command_plain_shell() -> None:
    item = RestoreItem(window=1, tab=1, cwd="/tmp/proj", cmd_args=None, label="x")
    cmd = ta_mod._compose_command(item)
    assert cmd == "cd '/tmp/proj'"


def test_compose_command_quotes_cwd_with_apostrophe() -> None:
    item = RestoreItem(window=1, tab=1, cwd="/tmp/joe's stuff", cmd_args=None, label="x")
    cmd = ta_mod._compose_command(item)
    assert "'/tmp/joe'\\''s stuff'" in cmd


def test_compose_command_returns_none_without_cwd() -> None:
    item = RestoreItem(window=1, tab=1, cwd=None, cmd_args=None, label="x")
    assert ta_mod._compose_command(item) is None


def test_compose_command_shell_quotes_resume_args() -> None:
    item = RestoreItem(
        window=1,
        tab=1,
        cwd="/tmp/proj",
        cmd_args=["claude", "--resume", "named session"],
        label="x",
    )
    cmd = ta_mod._compose_command(item)
    assert cmd == "cd '/tmp/proj' && claude --resume 'named session'"


def test_restore_passes_commands_to_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def _fake_run_osascript(script, args=None):
        captured["script"] = script
        captured["args"] = list(args or [])
        return "OK\n"

    monkeypatch.setattr(TerminalAppBackend, "_run_osascript", staticmethod(_fake_run_osascript))

    items = [
        RestoreItem(window=1, tab=1, cwd="/p1", cmd_args=["claude", "--resume", "a"], label="x"),
        RestoreItem(window=1, tab=2, cwd="/p2", cmd_args=None, label="x"),
    ]
    outcome = TerminalAppBackend().restore(items)

    assert outcome.launched == 2
    assert outcome.fellback is False
    assert captured["args"] == [
        "cd '/p1' && claude --resume a",
        "cd '/p2'",
    ]


def test_restore_detects_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        TerminalAppBackend,
        "_run_osascript",
        staticmethod(lambda script, args=None: "FALLBACK\n"),
    )
    item = RestoreItem(window=1, tab=1, cwd="/p", cmd_args=None, label="x")
    outcome = TerminalAppBackend().restore([item])
    assert outcome.fellback is True
    assert outcome.note is not None and "Accessibility" in outcome.note


def test_restore_skips_items_without_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    def _fake(script, args=None):
        called["n"] += 1
        return "OK"

    monkeypatch.setattr(TerminalAppBackend, "_run_osascript", staticmethod(_fake))

    item = RestoreItem(window=1, tab=1, cwd=None, cmd_args=None, label="x")
    outcome = TerminalAppBackend().restore([item])
    assert outcome.launched == 0
    assert called["n"] == 0
    assert outcome.note == "no tabs to reopen"
