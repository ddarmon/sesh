from __future__ import annotations

import platform

import pytest

from sesh.snapshots import core as snapshots_core
from sesh.snapshots import terminal_app as ta_mod
from sesh.snapshots.backend import RestoreOutcome, TerminalBackendError
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


# ----- osascript failures are surfaced, not saved as an empty capture -------


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_osascript_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = (
        "157:696: execution error: Terminal got an error: "
        "Can’t get every tab of item 10 of every window. (-1728)\n"
    )
    monkeypatch.setattr(
        ta_mod.subprocess, "run", lambda *a, **k: _FakeProc(1, "", stderr)
    )
    with pytest.raises(TerminalBackendError) as excinfo:
        TerminalAppBackend._run_osascript("script")
    assert "-1728" in str(excinfo.value)


def test_run_osascript_reports_exit_code_without_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ta_mod.subprocess, "run", lambda *a, **k: _FakeProc(3))
    with pytest.raises(TerminalBackendError, match="exit code 3"):
        TerminalAppBackend._run_osascript("script")


def test_run_osascript_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*a, **k):
        raise ta_mod.subprocess.TimeoutExpired(cmd="osascript", timeout=30)

    monkeypatch.setattr(ta_mod.subprocess, "run", _timeout)
    with pytest.raises(TerminalBackendError, match="timed out"):
        TerminalAppBackend._run_osascript("script")


def test_run_osascript_raises_when_osascript_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(*a, **k):
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(ta_mod.subprocess, "run", _missing)
    with pytest.raises(TerminalBackendError, match="could not run osascript"):
        TerminalAppBackend._run_osascript("script")


def test_run_osascript_returns_stdout_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ta_mod.subprocess, "run", lambda *a, **k: _FakeProc(0, "OK\n"))
    assert TerminalAppBackend._run_osascript("script") == "OK\n"


def test_capture_script_guards_unscriptable_windows() -> None:
    # Terminal can list windows with no tabs property (error -1728); the tab
    # lookup must sit inside a try so one such window cannot abort the capture.
    script = ta_mod._CAPTURE_APPLESCRIPT
    guard = script.index("try")
    assert guard < script.index("set winTabs to tabs of w") < script.index("on error")
    assert "repeat with t in winTabs" in script


def test_core_capture_wraps_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Failing:
        name = "failing"

        def capture(self):
            raise TerminalBackendError("osascript failed: boom")

    monkeypatch.setattr(snapshots_core, "get_backend", lambda: _Failing())
    with pytest.raises(snapshots_core.SnapshotsBackendError, match="boom"):
        snapshots_core.capture()
