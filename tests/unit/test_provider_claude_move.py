from __future__ import annotations

import json
from pathlib import Path

from sesh.models import Provider
from sesh.providers import claude
from tests.helpers import write_jsonl


def test_rewrite_cwd_in_jsonl_rewrites_exact_matches(tmp_path: Path) -> None:
    jsonl_file = tmp_path / "session.jsonl"
    jsonl_file.write_text(
        "\n".join(
            [
                json.dumps({"cwd": "/old", "x": 1}),
                json.dumps({"cwd": "/other", "x": 2}),
                "not json",
            ]
        )
        + "\n"
    )

    changed = claude._rewrite_cwd_in_jsonl(jsonl_file, "/old", "/new")
    assert changed is True

    lines = jsonl_file.read_text().splitlines()
    assert json.loads(lines[0])["cwd"] == "/new"
    assert json.loads(lines[1])["cwd"] == "/other"
    assert lines[2] == "not json"


def test_rewrite_cwd_in_jsonl_no_change_returns_false(tmp_path: Path) -> None:
    jsonl_file = tmp_path / "session.jsonl"
    write_jsonl(jsonl_file, [{"cwd": "/other"}])
    assert claude._rewrite_cwd_in_jsonl(jsonl_file, "/old", "/new") is False


def test_move_project_renames_and_rewrites(tmp_claude_dir) -> None:
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"
    old_dir = tmp_claude_dir / "projects" / claude.encode_claude_path(old_path)
    write_jsonl(
        old_dir / "session.jsonl",
        [
            {"cwd": old_path, "sessionId": "s1"},
            {"cwd": "/other", "sessionId": "s2"},
        ],
    )

    provider = claude.ClaudeProvider()
    provider._path_to_dir[old_path] = old_dir
    report = provider.move_project(old_path, new_path)

    new_dir = tmp_claude_dir / "projects" / claude.encode_claude_path(new_path)
    assert report.provider is Provider.CLAUDE
    assert report.success is True
    assert report.dirs_renamed == 1
    assert report.files_modified == 1
    assert not old_dir.exists()
    assert new_dir.is_dir()
    lines = new_dir.joinpath("session.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["cwd"] == new_path
    assert provider._path_to_dir.get(new_path) == new_dir
    assert old_path not in provider._path_to_dir


def test_move_project_conflict_when_target_exists(tmp_claude_dir) -> None:
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"
    (tmp_claude_dir / "projects" / claude.encode_claude_path(old_path)).mkdir(parents=True)
    (tmp_claude_dir / "projects" / claude.encode_claude_path(new_path)).mkdir(parents=True)

    report = claude.ClaudeProvider().move_project(old_path, new_path)
    assert report.success is False
    assert report.provider is Provider.CLAUDE
    assert report.error is not None
    assert "already exists" in report.error


def test_move_project_rewrites_metadata_in_existing_target_dir(tmp_claude_dir) -> None:
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"
    new_dir = tmp_claude_dir / "projects" / claude.encode_claude_path(new_path)
    write_jsonl(new_dir / "session.jsonl", [{"cwd": old_path, "sessionId": "s1"}])

    report = claude.ClaudeProvider().move_project(old_path, new_path)
    assert report.success is True
    assert report.dirs_renamed == 0
    assert report.files_modified == 1
    assert json.loads((new_dir / "session.jsonl").read_text().splitlines()[0])["cwd"] == new_path
