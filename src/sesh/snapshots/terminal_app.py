"""Darwin/Terminal.app backend for the snapshot subsystem.

Captures every Terminal tab (window/tab/tty/scrollback) via osascript,
resolves each tab's cwd via `ps` + `lsof`, and reopens tabs by handing a
list of shell commands back to osascript. Falls back to separate windows
when Accessibility is denied, since `Cmd-T` keystroke injection requires
that permission.
"""

from __future__ import annotations

import platform
import subprocess
from typing import TYPE_CHECKING

from sesh.snapshots.backend import CapturedTab, RestoreOutcome

if TYPE_CHECKING:
    from sesh.snapshots.core import RestoreItem


_SHELLS = ("zsh", "bash", "fish", "sh", "dash", "ksh")


_CAPTURE_APPLESCRIPT = r"""
set out to ""
tell application "Terminal"
    set winIdx to 0
    repeat with w in windows
        set winIdx to winIdx + 1
        set tabIdx to 0
        repeat with t in tabs of w
            set tabIdx to tabIdx + 1
            set tabTTY to tty of t
            set tabHist to history of t
            set out to out & "<<<TAB>>>" & linefeed
            set out to out & "WINDOW: " & winIdx & linefeed
            set out to out & "TAB: " & tabIdx & linefeed
            set out to out & "TTY: " & tabTTY & linefeed
            set out to out & "<<<HISTORY>>>" & linefeed
            set out to out & tabHist & linefeed
            set out to out & "<<<END>>>" & linefeed
        end repeat
    end repeat
end tell
return out
"""


_RESTORE_APPLESCRIPT = r"""
on run argv
    tell application "Terminal" to activate
    delay 0.2
    set isFirst to true
    set useTabs to true
    set fellBack to false
    repeat with c in argv
        set cmdStr to (c as string)
        if isFirst then
            tell application "Terminal"
                do script cmdStr
            end tell
            set isFirst to false
        else if useTabs then
            try
                tell application "System Events"
                    tell process "Terminal" to keystroke "t" using command down
                end tell
                delay 0.25
                tell application "Terminal"
                    do script cmdStr in front window
                end tell
            on error
                set useTabs to false
                set fellBack to true
                tell application "Terminal"
                    do script cmdStr
                end tell
            end try
        else
            tell application "Terminal"
                do script cmdStr
            end tell
        end if
        delay 0.15
    end repeat
    if fellBack then
        return "FALLBACK"
    else
        return "OK"
    end if
end run
"""


class TerminalAppBackend:
    """Snapshot backend for macOS Terminal.app."""

    name = "terminal_app"

    def is_supported(self) -> bool:
        return platform.system() == "Darwin"

    # ------------------------------------------------------------------
    # Capture

    def capture(self) -> list[CapturedTab]:
        raw = self._run_osascript(_CAPTURE_APPLESCRIPT)
        return self._parse_capture(raw)

    @staticmethod
    def _parse_capture(raw: str) -> list[CapturedTab]:
        """Parse the AppleScript dump into one CapturedTab per tab block."""
        tabs: list[CapturedTab] = []
        if not raw:
            return tabs

        blocks = raw.split("<<<TAB>>>")
        for block in blocks:
            block = block.strip()
            if not block:
                continue

            window: int | None = None
            tab_idx: int | None = None
            tty: str | None = None
            history_lines: list[str] = []
            in_history = False

            for line in block.splitlines():
                if line.startswith("WINDOW:"):
                    try:
                        window = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        window = None
                elif line.startswith("TAB:"):
                    try:
                        tab_idx = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        tab_idx = None
                elif line.startswith("TTY:"):
                    tty = line.split(":", 1)[1].strip() or None
                elif line.strip() == "<<<HISTORY>>>":
                    in_history = True
                elif line.strip() == "<<<END>>>":
                    in_history = False
                elif in_history:
                    history_lines.append(line)

            if window is None or tab_idx is None:
                continue

            scrollback = "\n".join(history_lines).rstrip()
            cwd = TerminalAppBackend._resolve_cwd(tty) if tty else None

            tabs.append(
                CapturedTab(
                    window=window,
                    tab=tab_idx,
                    tty=tty,
                    cwd=cwd,
                    scrollback_tail=_tail_lines(scrollback, 40),
                )
            )

        return tabs

    @staticmethod
    def _resolve_cwd(tty_path: str) -> str | None:
        """Resolve the cwd for a shell on the given tty (e.g. /dev/ttys001)."""
        if not tty_path:
            return None
        tty_short = tty_path[5:] if tty_path.startswith("/dev/") else tty_path

        try:
            ps_proc = subprocess.run(
                ["ps", "-t", tty_short, "-o", "pid=,comm="],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        if ps_proc.returncode != 0:
            return None

        # Pick the highest-PID shell on this tty.
        best_pid: int | None = None
        for line in ps_proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            comm = parts[1]
            base = comm.rsplit("/", 1)[-1].lstrip("-")
            if base in _SHELLS:
                if best_pid is None or pid > best_pid:
                    best_pid = pid

        if best_pid is None:
            return None

        try:
            lsof_proc = subprocess.run(
                ["lsof", "-a", "-d", "cwd", "-p", str(best_pid), "-Fn"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

        if lsof_proc.returncode != 0:
            return None

        for line in lsof_proc.stdout.splitlines():
            if line.startswith("n"):
                return line[1:].strip() or None
        return None

    # ------------------------------------------------------------------
    # Restore

    def restore(self, items: "list[RestoreItem]") -> RestoreOutcome:
        commands = [_compose_command(item) for item in items if item.cwd]
        commands = [c for c in commands if c]

        if not commands:
            return RestoreOutcome(launched=0, fellback=False, note="no tabs to reopen")

        result = self._run_osascript(_RESTORE_APPLESCRIPT, args=commands).strip()
        fellback = result == "FALLBACK"
        note: str | None = None
        if fellback:
            note = (
                "Opened sessions as separate windows because Accessibility "
                "is not granted to Terminal.app. Grant it under "
                "System Settings → Privacy & Security → Accessibility."
            )

        return RestoreOutcome(launched=len(commands), fellback=fellback, note=note)

    # ------------------------------------------------------------------
    # osascript driver

    @staticmethod
    def _run_osascript(script: str, args: list[str] | None = None) -> str:
        cmd = ["osascript", "-"]
        if args:
            cmd += args
        try:
            proc = subprocess.run(
                cmd,
                input=script,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout


def _tail_lines(text: str, max_lines: int) -> str:
    if not text:
        return text
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


def _compose_command(item: "RestoreItem") -> str | None:
    """Build the shell command to feed Terminal for one tab."""
    if not item.cwd:
        return None
    cwd_quoted = "'" + item.cwd.replace("'", "'\\''") + "'"
    if item.cmd_args:
        resume_str = " ".join(item.cmd_args)
        return f"cd {cwd_quoted} && {resume_str}"
    return f"cd {cwd_quoted}"
