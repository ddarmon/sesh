from __future__ import annotations

import json

from sesh.models import Provider
from sesh.providers import pi
from tests.helpers import write_jsonl


def _session_header(session_id: str, cwd: str, ts: str = "2026-01-01T00:00:00Z") -> dict:
    return {"type": "session", "version": 3, "id": session_id, "timestamp": ts, "cwd": cwd}


def test_rewrite_cwd_in_pi_jsonl_updates_session_header(tmp_path) -> None:
    file_path = tmp_path / "session.jsonl"
    write_jsonl(
        file_path,
        [
            _session_header("s1", "/old"),
            {"type": "message", "id": "m1", "message": {"role": "user", "content": []}},
        ],
    )

    changed = pi._rewrite_cwd_in_pi_jsonl(file_path, "/old", "/new")
    assert changed is True
    first = json.loads(file_path.read_text().splitlines()[0])
    assert first["cwd"] == "/new"


def test_rewrite_cwd_in_pi_jsonl_no_change_returns_false(tmp_path) -> None:
    file_path = tmp_path / "session.jsonl"
    write_jsonl(file_path, [_session_header("s1", "/somewhere-else")])
    assert pi._rewrite_cwd_in_pi_jsonl(file_path, "/old", "/new") is False


def test_move_project_renames_dir_and_rewrites_files(tmp_pi_dir) -> None:
    old_dir = tmp_pi_dir / pi.encode_pi_path("/old/repo")
    needs = old_dir / "2026-01-01T00-00-00Z_a.jsonl"
    no_change = old_dir / "2026-01-02T00-00-00Z_b.jsonl"
    write_jsonl(needs, [_session_header("a", "/old/repo")])
    write_jsonl(no_change, [_session_header("b", "/different/path")])

    report = pi.PiProvider().move_project("/old/repo", "/new/repo")
    assert report.success is True
    assert report.provider is Provider.PI
    assert report.dirs_renamed == 1
    assert report.files_modified == 1

    new_dir = tmp_pi_dir / pi.encode_pi_path("/new/repo")
    assert new_dir.is_dir()
    assert not old_dir.exists()
    moved = new_dir / "2026-01-01T00-00-00Z_a.jsonl"
    first = json.loads(moved.read_text().splitlines()[0])
    assert first["cwd"] == "/new/repo"


def test_move_project_target_dir_exists_fails(tmp_pi_dir) -> None:
    old_dir = tmp_pi_dir / pi.encode_pi_path("/old/repo")
    new_dir = tmp_pi_dir / pi.encode_pi_path("/new/repo")
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)

    report = pi.PiProvider().move_project("/old/repo", "/new/repo")
    assert report.success is False
    assert report.provider is Provider.PI
    assert "already exists" in report.error


def test_move_project_missing_dir_is_success(tmp_pi_dir) -> None:
    report = pi.PiProvider().move_project("/old", "/new")
    assert report.success is True
    assert report.provider is Provider.PI
    assert report.files_modified == 0
