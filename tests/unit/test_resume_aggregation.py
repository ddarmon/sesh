"""Resume should be disabled for any session carrying a host tag."""

from __future__ import annotations

from sesh.models import Provider
from sesh.resume import is_resumable
from tests.helpers import make_session


def test_resume_disabled_for_aggregated_session() -> None:
    """A SessionMeta with host set is never resumable, even Claude/Codex/Pi."""
    for provider in (Provider.CLAUDE, Provider.CODEX, Provider.COPILOT, Provider.PI):
        s = make_session(provider=provider, host="laptop")
        assert not is_resumable(s), f"{provider} with host should not be resumable"


def test_resume_still_works_for_local_claude() -> None:
    """Without a host tag, the existing resume rules still apply (Claude allowed)."""
    s = make_session(provider=Provider.CLAUDE, host=None)
    assert is_resumable(s)


def test_cursor_txt_still_refused_locally() -> None:
    """The existing Cursor .txt guard still applies in local mode."""
    s = make_session(
        provider=Provider.CURSOR, host=None, source_path="/x/transcript.txt"
    )
    assert not is_resumable(s)
