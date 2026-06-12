from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sesh.models import Provider
from sesh.providers.opencode import OpencodeProvider
from tests.helpers import create_opencode_db, write_opencode_storage_session


def test_move_rewrites_storage_directory(tmp_opencode_dir: Path) -> None:
    info_file = write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_mv",
        directory="/Users/me/old",
    )
    write_opencode_storage_session(
        tmp_opencode_dir,
        session_id="ses_other",
        project_id="proj_other",
        directory="/Users/me/unrelated",
    )

    provider = OpencodeProvider()
    report = provider.move_project("/Users/me/old", "/Users/me/new")
    assert report.provider is Provider.OPENCODE
    assert report.success
    assert report.files_modified == 1

    with open(info_file) as f:
        assert json.load(f)["directory"] == "/Users/me/new"

    # Re-discovery sees the new path only.
    fresh = OpencodeProvider()
    paths = [p for p, _ in fresh.discover_projects()]
    assert "/Users/me/new" in paths
    assert "/Users/me/old" not in paths
    assert "/Users/me/unrelated" in paths


def test_move_rewrites_db_directory(tmp_opencode_dir: Path) -> None:
    db = tmp_opencode_dir / "opencode.db"
    create_opencode_db(
        db,
        sessions=[
            {"id": "ses_1", "directory": "/Users/me/old", "title": "a",
             "time_created": 1, "time_updated": 2},
            {"id": "ses_2", "directory": "/Users/me/old", "title": "b",
             "time_created": 1, "time_updated": 2},
            {"id": "ses_3", "directory": "/Users/me/other", "title": "c",
             "time_created": 1, "time_updated": 2},
        ],
    )

    provider = OpencodeProvider()
    report = provider.move_project("/Users/me/old", "/Users/me/new")
    assert report.success
    assert report.files_modified == 2

    conn = sqlite3.connect(db)
    try:
        rows = dict(conn.execute("SELECT id, directory FROM session").fetchall())
    finally:
        conn.close()
    assert rows == {
        "ses_1": "/Users/me/new",
        "ses_2": "/Users/me/new",
        "ses_3": "/Users/me/other",
    }


def test_move_with_no_data_is_a_noop_success(tmp_opencode_dir: Path) -> None:
    provider = OpencodeProvider()
    report = provider.move_project("/Users/me/old", "/Users/me/new")
    assert report.success
    assert report.files_modified == 0
