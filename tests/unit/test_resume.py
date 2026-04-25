from __future__ import annotations

import pytest

from sesh import resume
from sesh.models import Provider
from tests.helpers import make_session


def test_resume_argv_per_provider() -> None:
    assert resume.resume_argv(Provider.CLAUDE, "abc") == ["claude", "--resume", "abc"]
    assert resume.resume_argv(Provider.CODEX, "xyz") == ["codex", "resume", "xyz"]
    assert resume.resume_argv(Provider.CURSOR, "cur") == ["agent", "--resume=cur"]
    assert resume.resume_argv(Provider.COPILOT, "cop") == ["copilot", "--resume=cop"]


def test_resume_argv_handles_uuid_with_dashes() -> None:
    sid = "11111111-2222-3333-4444-555555555555"
    assert resume.resume_argv(Provider.CLAUDE, sid)[-1] == sid


def test_is_resumable_true_for_normal_sessions() -> None:
    s = make_session(provider=Provider.CLAUDE, source_path="/some/file.jsonl")
    assert resume.is_resumable(s) is True


def test_is_resumable_false_for_cursor_txt_transcripts() -> None:
    s = make_session(provider=Provider.CURSOR, source_path="/x/y/transcript.txt")
    assert resume.is_resumable(s) is False


def test_is_resumable_true_for_cursor_store_db() -> None:
    s = make_session(provider=Provider.CURSOR, source_path="/x/y/store.db")
    assert resume.is_resumable(s) is True


def test_resume_binary_available_uses_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resume.shutil, "which", lambda b: "/fake/path" if b == "claude" else None)
    assert resume.resume_binary_available(Provider.CLAUDE) is True
    assert resume.resume_binary_available(Provider.CODEX) is False


def test_resume_binary_name() -> None:
    assert resume.resume_binary_name(Provider.CLAUDE) == "claude"
    assert resume.resume_binary_name(Provider.CODEX) == "codex"
    assert resume.resume_binary_name(Provider.CURSOR) == "agent"
    assert resume.resume_binary_name(Provider.COPILOT) == "copilot"
