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
    assert resume.resume_argv(Provider.PI, "pp") == ["pi", "--session", "pp"]
    assert resume.resume_argv(Provider.GEMINI, "gg") == ["gemini", "--resume", "gg"]


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


def test_is_resumable_true_for_gemini_with_resolved_path() -> None:
    """Gemini CLI >= 0.46 resumes by session UUID (scoped to the project cwd)."""
    s = make_session(
        provider=Provider.GEMINI,
        project_path="/Users/me/proj",
        source_path="/x/chats/session-a.json",
    )
    assert resume.is_resumable(s) is True
    assert Provider.GEMINI in resume.RESUME_COMMANDS


def test_is_resumable_false_for_gemini_unresolved_hash_dir() -> None:
    """Unresolved-hash sessions have no real cwd to run `gemini --resume` in."""
    hash_dir = "a" * 64
    s = make_session(
        provider=Provider.GEMINI,
        project_path=f"/Users/me/.gemini/tmp/{hash_dir}",
        source_path=f"/Users/me/.gemini/tmp/{hash_dir}/chats/session-a.json",
    )
    assert resume.is_resumable(s) is False


def test_resume_binary_available_uses_shutil_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resume.shutil, "which", lambda b: "/fake/path" if b == "claude" else None)
    assert resume.resume_binary_available(Provider.CLAUDE) is True
    assert resume.resume_binary_available(Provider.CODEX) is False


def test_resume_binary_name() -> None:
    assert resume.resume_binary_name(Provider.CLAUDE) == "claude"
    assert resume.resume_binary_name(Provider.CODEX) == "codex"
    assert resume.resume_binary_name(Provider.CURSOR) == "agent"
    assert resume.resume_binary_name(Provider.COPILOT) == "copilot"
    assert resume.resume_binary_name(Provider.PI) == "pi"
    assert resume.resume_binary_name(Provider.GEMINI) == "gemini"
