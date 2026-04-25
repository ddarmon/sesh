"""Shared resume-command helpers.

Maps each provider to the argv tokens needed to resume a session via its
CLI, plus a couple of small predicates used by the CLI, the TUI, and the
snapshot subsystem.

PATH-presence is intentionally decoupled from resumability so that the
snapshot capture path can record `cmd_args` even when the CLI binary
isn't installed at capture time. Use `resume_binary_available()` only at
the moment a resume actually needs to launch.
"""

from __future__ import annotations

import shutil

from sesh.models import Provider, SessionMeta


# Pure mapping: no shutil.which here.
RESUME_COMMANDS: dict[Provider, list[str]] = {
    Provider.CLAUDE: ["claude", "--resume", "{id}"],
    Provider.CODEX: ["codex", "resume", "{id}"],
    Provider.CURSOR: ["agent", "--resume={id}"],
    Provider.COPILOT: ["copilot", "--resume={id}"],
}


def is_resumable(session: SessionMeta) -> bool:
    """True if the session can in principle be resumed (no PATH check)."""
    if (
        session.provider == Provider.CURSOR
        and session.source_path
        and session.source_path.endswith(".txt")
    ):
        return False
    return True


def resume_argv(provider: Provider, session_id: str) -> list[str]:
    """Build the argv for a resume command. No PATH check."""
    return [tok.replace("{id}", session_id) for tok in RESUME_COMMANDS[provider]]


def resume_binary_available(provider: Provider) -> bool:
    """Standalone PATH check, used at launch time only."""
    return shutil.which(RESUME_COMMANDS[provider][0]) is not None


def resume_binary_name(provider: Provider) -> str:
    """Return the binary name for a provider's resume command."""
    return RESUME_COMMANDS[provider][0]
