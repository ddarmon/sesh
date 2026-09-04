from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sesh.models import Provider
from sesh.providers import cline
from tests.helpers import write_cline_session


def _record(data_dir: Path, session_id: str) -> dict:
    with open(data_dir / "sessions" / session_id / f"{session_id}.json") as f:
        return json.load(f)


def _db_paths(data_dir: Path, session_id: str) -> tuple[str, str]:
    conn = sqlite3.connect(data_dir / "db" / "sessions.db")
    try:
        return conn.execute(
            "SELECT workspace_root, cwd FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()


def test_move_rewrites_records_and_db_rows(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/old/repo")
    write_cline_session(tmp_cline_dir, session_id="2_b", workspace_root="/other")

    report = cline.ClineProvider().move_project("/old/repo", "/new/repo")

    assert report.success is True
    assert report.provider is Provider.CLINE
    assert report.files_modified == 1

    moved = _record(tmp_cline_dir, "1_a")
    assert moved["workspace_root"] == "/new/repo"
    assert moved["cwd"] == "/new/repo"
    assert _db_paths(tmp_cline_dir, "1_a") == ("/new/repo", "/new/repo")

    untouched = _record(tmp_cline_dir, "2_b")
    assert untouched["workspace_root"] == "/other"
    assert _db_paths(tmp_cline_dir, "2_b") == ("/other", "/other")


def test_move_rewrites_nested_paths(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="1_a",
        workspace_root="/old/repo", cwd="/old/repo/sub/dir",
    )

    cline.ClineProvider().move_project("/old/repo", "/new/repo")

    record = _record(tmp_cline_dir, "1_a")
    assert record["workspace_root"] == "/new/repo"
    assert record["cwd"] == "/new/repo/sub/dir"
    assert _db_paths(tmp_cline_dir, "1_a") == ("/new/repo", "/new/repo/sub/dir")


def test_move_does_not_match_sibling_prefixes(tmp_cline_dir: Path) -> None:
    """``/old/repo`` must not swallow ``/old/repo-backup``."""
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/old/repo-backup")

    report = cline.ClineProvider().move_project("/old/repo", "/new/repo")

    assert report.files_modified == 0
    assert _record(tmp_cline_dir, "1_a")["workspace_root"] == "/old/repo-backup"
    assert _db_paths(tmp_cline_dir, "1_a") == ("/old/repo-backup", "/old/repo-backup")


def test_move_leaves_transcript_and_system_prompt_alone(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/old/repo",
        system_prompt="Working Directory: /old/repo",
    )
    cline.ClineProvider().move_project("/old/repo", "/new/repo")

    with open(tmp_cline_dir / "sessions" / "1_a" / "1_a.messages.json") as f:
        transcript = json.load(f)
    assert transcript["system_prompt"] == "Working Directory: /old/repo"


def test_move_strips_no_internal_fields_into_the_record(tmp_cline_dir: Path) -> None:
    """The provider's private ``_messages_path`` never hits disk."""
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/old/repo")
    provider = cline.ClineProvider()
    provider.get_sessions("/old/repo")  # populates the private fields
    provider.move_project("/old/repo", "/new/repo")

    record = _record(tmp_cline_dir, "1_a")
    assert not [key for key in record if key.startswith("_")]
    assert record["version"] == 1


def test_move_with_no_sessions_root_succeeds(tmp_cline_dir: Path) -> None:
    report = cline.ClineProvider().move_project("/old", "/new")
    assert report.success is True
    assert report.files_modified == 0


def test_move_reports_db_failure(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/old/repo")
    (tmp_cline_dir / "db" / "sessions.db").write_bytes(b"not a database")

    report = cline.ClineProvider().move_project("/old/repo", "/new/repo")

    assert report.success is False
    assert "index" in (report.error or "")
    # The JSON records were still rewritten before the index failed.
    assert report.files_modified == 1


def test_move_project_orchestration_includes_cline(tmp_move_dirs: dict, tmp_path: Path) -> None:
    from sesh import move

    old = tmp_path / "old-project"
    old.mkdir()
    new = tmp_path / "new-project"

    write_cline_session(
        tmp_move_dirs["cline_data"], session_id="1_a", workspace_root=str(old),
    )

    dry = {r.provider: r for r in move.move_project(str(old), str(new), dry_run=True)}
    assert dry[Provider.CLINE].files_modified == 1

    reports = {r.provider: r for r in move.move_project(str(old), str(new))}
    assert reports[Provider.CLINE].success is True
    assert reports[Provider.CLINE].files_modified == 1
    assert _record(tmp_move_dirs["cline_data"], "1_a")["workspace_root"] == str(new)


def test_move_into_a_subdirectory_of_itself(tmp_cline_dir: Path) -> None:
    """A nested move (/a -> /a/b) must not rewrite its own output twice."""
    write_cline_session(
        tmp_cline_dir, session_id="1_a",
        workspace_root="/a", cwd="/a/sub",
    )

    cline.ClineProvider().move_project("/a", "/a/b")

    record = _record(tmp_cline_dir, "1_a")
    assert record["workspace_root"] == "/a/b"
    assert record["cwd"] == "/a/b/sub"
    assert _db_paths(tmp_cline_dir, "1_a") == ("/a/b", "/a/b/sub")


def test_move_db_pass_is_case_sensitive(tmp_cline_dir: Path) -> None:
    """SQL LIKE is case-insensitive for ASCII; the DB pass must not be.

    Otherwise moving `/Users/me/repo` rewrites a record stored as
    `/Users/me/Repo` in Cline's index while the JSON pass correctly skips it —
    corrupting the index and reporting zero files changed.
    """
    write_cline_session(
        tmp_cline_dir, session_id="1_a",
        workspace_root="/Users/me/Repo", cwd="/Users/me/Repo/sub",
    )

    report = cline.ClineProvider().move_project("/Users/me/repo", "/Users/me/moved")

    assert report.files_modified == 0
    record = _record(tmp_cline_dir, "1_a")
    assert record["workspace_root"] == "/Users/me/Repo"
    assert record["cwd"] == "/Users/me/Repo/sub"
    assert _db_paths(tmp_cline_dir, "1_a") == ("/Users/me/Repo", "/Users/me/Repo/sub")


def test_move_db_and_json_passes_agree(tmp_cline_dir: Path) -> None:
    """Every row the JSON pass rewrites is rewritten identically in the DB."""
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/old", cwd="/old/deep/sub",
    )
    write_cline_session(tmp_cline_dir, session_id="2_b", workspace_root="/old-ish")
    write_cline_session(tmp_cline_dir, session_id="3_c", workspace_root="/unrelated")

    cline.ClineProvider().move_project("/old", "/new")

    for session_id in ("1_a", "2_b", "3_c"):
        record = _record(tmp_cline_dir, session_id)
        assert _db_paths(tmp_cline_dir, session_id) == (
            record["workspace_root"], record["cwd"],
        )
    assert _db_paths(tmp_cline_dir, "1_a") == ("/new", "/new/deep/sub")
    assert _db_paths(tmp_cline_dir, "2_b") == ("/old-ish", "/old-ish")


def test_move_error_paths_drop_the_record_cache(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/old")
    provider = cline.ClineProvider()
    provider.get_sessions("/old")
    (tmp_cline_dir / "db" / "sessions.db").write_bytes(b"not a database")

    assert provider.move_project("/old", "/new").success is False
    # The JSON records were rewritten, so a reused instance must not serve
    # pre-move paths from its cache.
    assert provider.get_sessions("/old") == []
    assert [s.id for s in provider.get_sessions("/new")] == ["1_a"]
