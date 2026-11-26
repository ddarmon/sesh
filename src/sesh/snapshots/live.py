"""Resolve the session a terminal tab's running agent process holds.

Scrollback is a poor witness: it can show a resume command printed by a
session that has since exited, and a phrase search over it can land on an
unrelated session. A tab's processes are a better one. Resolution starts
from the agent process on the tab's tty and works down a list of tiers,
recording which tier answered (``SnapshotResume.source``):

1. ``process`` -- the provider's own record of what that process has open
   (``SessionProvider.live_session_id``). Tracks in-app session switches.
2. ``argv`` -- a session id on the process's command line. Exact at launch,
   stale if the session was switched inside the app afterward.
3. ``activity`` -- the only session of that provider in the tab's working
   directory that was active after the process started. A guess.
4. ``search`` -- several such sessions; a literal scrollback phrase found in
   exactly the candidates picks one. A guess.

When no tier answers, the tab gets no resume command: an unresolved tab is
better than one that reopens the wrong session.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable

from sesh.models import LiveProcess, Provider, SessionMeta
from sesh.resume import RESUME_COMMANDS
from sesh.snapshots.backend import TtyProcess

# Launchers that run an agent as a script: the agent is then argv[1].
_INTERPRETERS = ("node", "bun", "deno", "python", "python3")

# A session written this long before the process started still counts as
# active under it (clock granularity, a resumed session's first write).
_ACTIVITY_SLACK_SECONDS = 5.0

_BINARY_TO_PROVIDER: dict[str, Provider] = {
    argv[0]: provider for provider, argv in RESUME_COMMANDS.items()
}


def _agent_provider(argv: list[str]) -> Provider | None:
    """The provider whose CLI *argv* runs, or None."""
    if not argv:
        return None
    name = os.path.basename(argv[0]).lstrip("-")
    provider = _BINARY_TO_PROVIDER.get(name)
    if provider is not None:
        return provider
    if name in _INTERPRETERS or name.startswith(("node", "python")):
        for token in argv[1:]:
            if token.startswith("-"):
                continue
            return _BINARY_TO_PROVIDER.get(os.path.basename(token))
    return None


def find_agent(processes: Iterable[TtyProcess]) -> tuple[Provider, TtyProcess] | None:
    """The agent CLI running on a tty: the earliest-started match.

    An agent's own helpers (MCP servers, a re-exec'd child) start after it,
    so the earliest match is the process the user launched.
    """
    matches = [
        (proc, provider)
        for proc in processes
        if (provider := _agent_provider(proc.argv)) is not None
    ]
    if not matches:
        return None
    proc, provider = min(
        matches,
        key=lambda m: (m[0].start if m[0].start is not None else float("inf"), m[0].pid),
    )
    return provider, proc


def descendant_pids(root: int, processes: Iterable[TtyProcess]) -> tuple[int, ...]:
    """*root* followed by every process descended from it."""
    children: dict[int, list[int]] = {}
    for proc in processes:
        children.setdefault(proc.ppid, []).append(proc.pid)
    out: list[int] = []
    todo = [root]
    while todo:
        pid = todo.pop()
        if pid in out:
            continue
        out.append(pid)
        todo.extend(sorted(children.get(pid, []), reverse=True))
    return tuple(out)


def list_open_files(pid: int) -> list[str]:
    """Paths *pid* holds open, via ``lsof``. Empty on any failure."""
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(pid), "-Fn"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    # lsof exits non-zero when some file descriptors cannot be described but
    # still prints the rest, so the output is used regardless of exit code.
    return [line[1:] for line in proc.stdout.splitlines() if line.startswith("n/")]


def live_process(
    agent: TtyProcess,
    processes: Iterable[TtyProcess],
    open_files: Callable[[int], list[str]] = list_open_files,
) -> LiveProcess:
    return LiveProcess(
        pid=agent.pid,
        pids=descendant_pids(agent.pid, processes),
        start=agent.start,
        argv=list(agent.argv),
        open_files=open_files(agent.pid),
    )


def active_candidates(
    provider: Provider,
    cwd: str,
    started: float | None,
    sessions: Iterable[SessionMeta],
    normalize: Callable[[str | None], str | None],
) -> list[SessionMeta]:
    """Sessions of *provider* in *cwd* that were active after *started*."""
    if started is None:
        return []
    cwd_norm = normalize(cwd)
    out: list[SessionMeta] = []
    for session in sessions:
        if session.provider != provider or session.host is not None:
            continue
        if normalize(session.project_path) != cwd_norm:
            continue
        try:
            last_active = session.timestamp.timestamp()
        except (AttributeError, OverflowError, OSError, ValueError):
            continue
        if last_active >= started - _ACTIVITY_SLACK_SECONDS:
            out.append(session)
    return out
