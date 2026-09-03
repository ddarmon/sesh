from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sesh.models import Provider
from sesh.providers import cline
from tests.helpers import make_session, write_cline_session


def _rows(data_dir: Path) -> list[str]:
    conn = sqlite3.connect(data_dir / "db" / "sessions.db")
    try:
        return [r[0] for r in conn.execute("SELECT session_id FROM sessions")]
    finally:
        conn.close()


def test_delete_removes_directory_and_db_rows(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/p")
    write_cline_session(
        tmp_cline_dir, session_id="2_b", workspace_root="/p",
        is_subagent=True, parent_session_id="1_a",
    )
    write_cline_session(tmp_cline_dir, session_id="3_c", workspace_root="/p")

    provider = cline.ClineProvider()
    (session,) = [s for s in provider.get_sessions("/p") if s.id == "1_a"]
    provider.delete_session(session)

    assert not (tmp_cline_dir / "sessions" / "1_a").exists()
    assert (tmp_cline_dir / "sessions" / "3_c").is_dir()
    # The child row goes too, matching Cline's own store.
    assert _rows(tmp_cline_dir) == ["3_c"]


def test_delete_refuses_unsafe_session_id(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/p")
    session = make_session(id="../../etc", provider=Provider.CLINE)

    with pytest.raises(ValueError):
        cline.ClineProvider().delete_session(session)
    assert (tmp_cline_dir / "sessions" / "1_a").is_dir()


def test_delete_without_db_still_removes_files(tmp_cline_dir: Path) -> None:
    write_cline_session(
        tmp_cline_dir, session_id="1_a", workspace_root="/p", with_db_row=False,
    )
    provider = cline.ClineProvider()
    (session,) = provider.get_sessions("/p")
    provider.delete_session(session)

    assert not (tmp_cline_dir / "sessions" / "1_a").exists()


def test_delete_reports_db_failure_after_removing_files(tmp_cline_dir: Path) -> None:
    """A locked or corrupt index is a clear error, not a silent partial move."""
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/p")
    db_path = tmp_cline_dir / "db" / "sessions.db"
    db_path.write_bytes(b"not a database")

    provider = cline.ClineProvider()
    (session,) = provider.get_sessions("/p")
    with pytest.raises(RuntimeError):
        provider.delete_session(session)

    assert not (tmp_cline_dir / "sessions" / "1_a").exists()


def test_delete_drops_cached_records(tmp_cline_dir: Path) -> None:
    write_cline_session(tmp_cline_dir, session_id="1_a", workspace_root="/p")
    write_cline_session(tmp_cline_dir, session_id="2_b", workspace_root="/p")

    provider = cline.ClineProvider()
    sessions = provider.get_sessions("/p")
    provider.delete_session(next(s for s in sessions if s.id == "1_a"))

    assert [s.id for s in provider.get_sessions("/p")] == ["2_b"]
