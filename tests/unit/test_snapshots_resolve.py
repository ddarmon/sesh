from __future__ import annotations

from pathlib import Path

import pytest

from sesh.models import Provider, SearchResult
from sesh.snapshots import core as snapshots_core


def test_parse_explicit_resume_finds_claude() -> None:
    text = "$ claude --resume abc-123\nsome output"
    r = snapshots_core._parse_explicit_resume(text)
    assert r is not None
    assert r.provider == Provider.CLAUDE
    assert r.session_id == "abc-123"
    assert r.cmd_args == ["claude", "--resume", "abc-123"]
    assert r.source == "explicit"


def test_parse_explicit_resume_handles_quoted_claude_id() -> None:
    text = 'claude --resume "my-named-session"'
    r = snapshots_core._parse_explicit_resume(text)
    assert r is not None
    assert r.session_id == "my-named-session"


def test_parse_explicit_resume_finds_codex() -> None:
    text = "codex resume xyz789"
    r = snapshots_core._parse_explicit_resume(text)
    assert r is not None
    assert r.provider == Provider.CODEX
    assert r.cmd_args == ["codex", "resume", "xyz789"]


def test_parse_explicit_resume_finds_cursor() -> None:
    text = "agent --resume=cur-1"
    r = snapshots_core._parse_explicit_resume(text)
    assert r is not None
    assert r.provider == Provider.CURSOR
    assert r.cmd_args == ["agent", "--resume=cur-1"]


def test_parse_explicit_resume_finds_copilot() -> None:
    text = "copilot --resume=cop-2"
    r = snapshots_core._parse_explicit_resume(text)
    assert r is not None
    assert r.provider == Provider.COPILOT
    assert r.cmd_args == ["copilot", "--resume=cop-2"]


def test_parse_explicit_resume_takes_last_match() -> None:
    text = "claude --resume old-id\nlater output\nclaude --resume new-id"
    r = snapshots_core._parse_explicit_resume(text)
    assert r is not None
    assert r.session_id == "new-id"


def test_parse_explicit_resume_none_when_absent() -> None:
    text = "no resume command here"
    assert snapshots_core._parse_explicit_resume(text) is None


def test_candidate_phrases_skips_short_and_decoration() -> None:
    text = "\n".join([
        "❯ short",
        "> shell prompt with arrows",
        "─" * 60,
        "$ ls",
        "Last login: Wed at terminal",
        "abc",  # too short
        "this is a long enough line of regular english to qualify probably",
    ])
    out = snapshots_core._candidate_phrases(text)
    # The keeper line ("this is a long enough line ...") should produce
    # a 7-word phrase. The decoration / short / "Last login" lines should not.
    assert any(p.startswith("this is a long enough line") for p in out)
    assert all("Last login" not in p for p in out)


def test_candidate_phrases_newest_first() -> None:
    text = "\n".join([
        "this is the first phrase that should pass the filters here easily",
        "this is the second phrase that should pass the filters here also",
    ])
    out = snapshots_core._candidate_phrases(text)
    # Bottom of scrollback (newest) comes first
    assert out[0].startswith("this is the second")
    assert out[-1].startswith("this is the first")


def test_search_recover_picks_matching_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    text = "this is a long enough line of regular english to qualify probably yeah"

    fake_results = [
        SearchResult(
            session_id="other-id",
            project_path="/other/path",
            provider=Provider.CLAUDE,
            matched_line="x",
            file_path=str(tmp_path / "other.jsonl"),
        ),
        SearchResult(
            session_id="match-id",
            project_path=str(proj),
            provider=Provider.CLAUDE,
            matched_line="x",
            file_path=str(tmp_path / "match.jsonl"),
        ),
    ]
    (tmp_path / "match.jsonl").write_text("data")

    monkeypatch.setattr("sesh.search.ripgrep_search", lambda q: fake_results)

    r = snapshots_core._search_recover(text, str(proj), index_mtimes=None)
    assert r is not None
    assert r.session_id == "match-id"
    assert r.source == "search"
    assert r.matched_phrase is not None


def test_search_recover_tiebreaks_by_index_mtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    text = "this is a long enough line of regular english to qualify probably yeah"

    older = SearchResult(
        session_id="older-id",
        project_path=str(proj),
        provider=Provider.CLAUDE,
        matched_line="x",
        file_path=str(tmp_path / "older.jsonl"),
    )
    newer = SearchResult(
        session_id="newer-id",
        project_path=str(proj),
        provider=Provider.CLAUDE,
        matched_line="x",
        file_path=str(tmp_path / "newer.jsonl"),
    )
    (tmp_path / "older.jsonl").write_text("data")
    (tmp_path / "newer.jsonl").write_text("data")

    monkeypatch.setattr("sesh.search.ripgrep_search", lambda q: [older, newer])

    r = snapshots_core._search_recover(
        text,
        str(proj),
        index_mtimes={"older-id": 100.0, "newer-id": 200.0},
    )
    assert r is not None
    assert r.session_id == "newer-id"


def test_search_recover_falls_back_to_file_mtime_when_no_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    text = "this is a long enough line of regular english to qualify probably yeah"

    older_file = tmp_path / "older.jsonl"
    older_file.write_text("data")
    newer_file = tmp_path / "newer.jsonl"
    newer_file.write_text("data")

    import os
    os.utime(older_file, (100, 100))
    os.utime(newer_file, (1_000_000, 1_000_000))

    older = SearchResult(
        session_id="older-id",
        project_path=str(proj),
        provider=Provider.CLAUDE,
        matched_line="x",
        file_path=str(older_file),
    )
    newer = SearchResult(
        session_id="newer-id",
        project_path=str(proj),
        provider=Provider.CLAUDE,
        matched_line="x",
        file_path=str(newer_file),
    )

    monkeypatch.setattr("sesh.search.ripgrep_search", lambda q: [older, newer])

    r = snapshots_core._search_recover(text, str(proj), index_mtimes=None)
    assert r is not None
    assert r.session_id == "newer-id"


def test_search_recover_normalizes_realpath(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real = tmp_path / "real_proj"
    real.mkdir()
    link = tmp_path / "link_proj"
    link.symlink_to(real)
    text = "this is a long enough line of regular english to qualify probably yeah"

    fake = SearchResult(
        session_id="match-id",
        project_path=str(real),
        provider=Provider.CLAUDE,
        matched_line="x",
        file_path=str(tmp_path / "f.jsonl"),
    )
    (tmp_path / "f.jsonl").write_text("data")

    monkeypatch.setattr("sesh.search.ripgrep_search", lambda q: [fake])

    # Pass the symlink as cwd; should still resolve thanks to realpath().
    r = snapshots_core._search_recover(text, str(link), index_mtimes=None)
    assert r is not None
    assert r.session_id == "match-id"


def test_search_recover_returns_none_when_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sesh.search.ripgrep_search", lambda q: [])
    text = "this is a long enough line of regular english to qualify probably yeah"
    assert snapshots_core._search_recover(text, "/tmp/proj") is None


def test_search_recover_no_phrases_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty / decoration-only scrollback yields no candidate phrases.
    text = "$ ls\n> short"
    monkeypatch.setattr("sesh.search.ripgrep_search", lambda q: [])
    assert snapshots_core._search_recover(text, "/tmp/proj") is None
