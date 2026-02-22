from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sesh import move
from sesh.models import MoveReport, Provider
from tests.helpers import create_store_db, write_jsonl


def test_validate_paths_rejects_same_path(tmp_path: Path) -> None:
    """Old and new paths being identical is rejected."""
    path = str(tmp_path / "repo")
    with pytest.raises(ValueError, match="must be different"):
        move._validate_paths(path, path, full_move=True)


def test_validate_paths_full_move_checks_old_and_new(tmp_path: Path) -> None:
    """Full move requires old to exist and new to not exist."""
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"

    with pytest.raises(ValueError, match="Old path does not exist"):
        move._validate_paths(str(old_path), str(new_path), full_move=True)

    old_path.mkdir()
    new_path.mkdir()
    with pytest.raises(ValueError, match="New path already exists"):
        move._validate_paths(str(old_path), str(new_path), full_move=True)


def test_validate_paths_metadata_only_requires_new_exists(tmp_path: Path) -> None:
    """Metadata-only move requires the new path to exist (files already moved)."""
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.mkdir()
    with pytest.raises(ValueError, match="metadata-only move"):
        move._validate_paths(str(old_path), str(new_path), full_move=False)

    new_path.mkdir()
    move._validate_paths(str(old_path), str(new_path), full_move=False)


def test_invalidate_caches_removes_files(tmp_move_dirs) -> None:
    """Cache invalidation deletes the index, project paths, and session cache files."""
    for path in (move.CACHE_FILE, move.INDEX_FILE, move.PROJECT_PATHS_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        assert path.exists()

    move._invalidate_caches()

    assert not move.CACHE_FILE.exists()
    assert not move.INDEX_FILE.exists()
    assert not move.PROJECT_PATHS_FILE.exists()


def test_claude_file_needs_cwd_rewrite(tmp_path: Path) -> None:
    """Detects whether a Claude JSONL file contains a cwd field matching the old path."""
    jsonl_file = tmp_path / "a.jsonl"
    write_jsonl(jsonl_file, [{"cwd": "/old"}, {"cwd": "/other"}])
    assert move._claude_file_needs_cwd_rewrite(jsonl_file, "/old") is True
    assert move._claude_file_needs_cwd_rewrite(jsonl_file, "/missing") is False


def test_codex_file_needs_rewrite_for_session_meta_and_legacy(tmp_path: Path) -> None:
    """Detects both new-format (payload.cwd) and legacy (<cwd> tag) Codex references."""
    new_format = tmp_path / "new.jsonl"
    write_jsonl(
        new_format,
        [
            {
                "type": "session_meta",
                "payload": {"cwd": "/old"},
            }
        ],
    )
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text('{"payload":{"content":[{"text":"<cwd>/old</cwd>"}]}}\n')

    assert move._codex_file_needs_rewrite(new_format, "/old") is True
    assert move._codex_file_needs_rewrite(legacy, "/old") is True
    assert move._codex_file_needs_rewrite(legacy, "/missing") is False


def test_cursor_store_db_needs_rewrite(tmp_path: Path) -> None:
    """Detects whether a Cursor store.db contains blobs referencing the old path."""
    store_db = tmp_path / "store.db"
    create_store_db(store_db, blobs=[{"content": "path /old/repo"}])
    assert move._cursor_store_db_needs_rewrite(store_db, "/old/repo") is True
    assert move._cursor_store_db_needs_rewrite(store_db, "/missing") is False


def test_dry_run_claude_counts_files_and_renames(tmp_move_dirs) -> None:
    """Claude dry run counts JSONL files needing rewrite and dir renames (agent-* excluded)."""
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"
    old_dir = move.PROJECTS_DIR / move.encode_claude_path(old_path)
    write_jsonl(old_dir / "one.jsonl", [{"cwd": old_path}])
    write_jsonl(old_dir / "agent-skip.jsonl", [{"cwd": old_path}])

    report = move._dry_run_claude(old_path, new_path)
    assert report == MoveReport(
        provider=Provider.CLAUDE,
        success=True,
        files_modified=1,
        dirs_renamed=1,
        error=None,
    )


def test_dry_run_claude_conflict(tmp_move_dirs) -> None:
    """Claude dry run reports an error when the target directory already exists."""
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"
    (move.PROJECTS_DIR / move.encode_claude_path(old_path)).mkdir(parents=True)
    (move.PROJECTS_DIR / move.encode_claude_path(new_path)).mkdir(parents=True)

    report = move._dry_run_claude(old_path, new_path)
    assert report.success is False
    assert report.provider is Provider.CLAUDE
    assert report.error is not None


def test_dry_run_codex_counts_matching_files(tmp_move_dirs) -> None:
    """Codex dry run counts only JSONL files containing the old path."""
    write_jsonl(
        move.CODEX_DIR / "a.jsonl",
        [{"type": "session_meta", "payload": {"cwd": "/old"}}],
    )
    write_jsonl(move.CODEX_DIR / "b.jsonl", [{"type": "event_msg"}])

    report = move._dry_run_codex("/old")
    assert report.success is True
    assert report.provider is Provider.CODEX
    assert report.files_modified == 1


def test_dry_run_cursor_counts_dirs_and_files(tmp_move_dirs) -> None:
    """Cursor dry run counts chats/projects dir renames plus workspace.json and store.db rewrites."""
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"

    old_md5 = hashlib.md5(old_path.encode()).hexdigest()
    old_chats = move.CURSOR_CHATS_DIR / old_md5
    create_store_db(
        old_chats / "sess1" / "store.db",
        blobs=[{"content": f"open {old_path}/main.py"}],
    )

    old_projects = move.CURSOR_PROJECTS_DIR / move.encode_cursor_path(old_path)
    (old_projects / "agent-transcripts").mkdir(parents=True)

    ws = move.WORKSPACE_STORAGE / "ws1"
    ws.mkdir(parents=True)
    (ws / "workspace.json").write_text(json.dumps({"folder": move.workspace_uri(old_path)}))

    report = move._dry_run_cursor(old_path, new_path)
    assert report.provider is Provider.CURSOR
    assert report.success is True
    assert report.dirs_renamed == 2
    assert report.files_modified == 2


def test_dry_run_cursor_conflict(tmp_move_dirs) -> None:
    """Cursor dry run reports an error when the target chats directory already exists."""
    old_path = "/Users/me/old"
    new_path = "/Users/me/new"
    (move.CURSOR_CHATS_DIR / hashlib.md5(old_path.encode()).hexdigest()).mkdir(parents=True)
    (move.CURSOR_CHATS_DIR / hashlib.md5(new_path.encode()).hexdigest()).mkdir(parents=True)

    report = move._dry_run_cursor(old_path, new_path)
    assert report.success is False
    assert report.provider is Provider.CURSOR
    assert report.error is not None
    assert "target chats directory exists" in report.error


def test_move_project_dry_run_does_not_move_files(tmp_path: Path, monkeypatch) -> None:
    """Dry run produces reports without calling shutil.move or modifying the filesystem."""
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.mkdir()

    monkeypatch.setattr(
        move.shutil,
        "move",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run should not move")),
    )

    reports = move.move_project(str(old_path), str(new_path), dry_run=True)
    assert [r.provider for r in reports] == [Provider.CLAUDE, Provider.CODEX, Provider.CURSOR]
    assert all(isinstance(r, MoveReport) for r in reports)
    assert old_path.exists()
    assert not new_path.exists()


def test_move_project_orchestrates_providers_and_invalidates_caches(
    tmp_path: Path, tmp_move_dirs, monkeypatch
) -> None:
    """Full move: relocates directory on disk, calls each provider, then invalidates caches."""
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.mkdir()

    for path in (move.CACHE_FILE, move.INDEX_FILE, move.PROJECT_PATHS_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")

    moved: list[tuple[str, str]] = []
    monkeypatch.setattr(move.shutil, "move", lambda src, dst: moved.append((src, dst)))

    class FakeClaude:
        def move_project(self, old: str, new: str) -> MoveReport:
            return MoveReport(provider=Provider.CLAUDE, success=True, files_modified=1)

    class FakeCodex:
        def move_project(self, old: str, new: str) -> MoveReport:
            return MoveReport(provider=Provider.CODEX, success=True)

    class FakeCursor:
        def move_project(self, old: str, new: str) -> MoveReport:
            return MoveReport(provider=Provider.CURSOR, success=True, dirs_renamed=2)

    monkeypatch.setattr(move, "ClaudeProvider", FakeClaude)
    monkeypatch.setattr(move, "CodexProvider", FakeCodex)
    monkeypatch.setattr(move, "CursorProvider", FakeCursor)

    reports = move.move_project(str(old_path), str(new_path), full_move=True, dry_run=False)

    assert moved == [(str(old_path), str(new_path))]
    assert [r.provider for r in reports] == [Provider.CLAUDE, Provider.CODEX, Provider.CURSOR]
    assert all(r.success for r in reports)
    assert not move.CACHE_FILE.exists()
    assert not move.INDEX_FILE.exists()
    assert not move.PROJECT_PATHS_FILE.exists()


def test_move_project_captures_provider_exception(tmp_path: Path, monkeypatch) -> None:
    """A provider exception is captured in the MoveReport rather than aborting the whole move."""
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.mkdir()
    monkeypatch.setattr(move.shutil, "move", lambda src, dst: None)

    class OkProvider:
        def move_project(self, old: str, new: str) -> MoveReport:
            return MoveReport(provider=Provider.CLAUDE, success=True)

    class BoomProvider:
        def move_project(self, old: str, new: str) -> MoveReport:
            raise RuntimeError("boom")

    monkeypatch.setattr(move, "ClaudeProvider", OkProvider)
    monkeypatch.setattr(move, "CodexProvider", BoomProvider)
    monkeypatch.setattr(move, "CursorProvider", OkProvider)

    reports = move.move_project(str(old_path), str(new_path))
    by_provider = {r.provider: r for r in reports}
    assert by_provider[Provider.CODEX].success is False
    assert by_provider[Provider.CODEX].error == "boom"


def test_move_project_wraps_shutil_errors(tmp_path: Path, monkeypatch) -> None:
    """Filesystem errors during shutil.move are wrapped as ValueError."""
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.mkdir()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(move.shutil, "move", boom)
    with pytest.raises(ValueError, match="Failed moving project files"):
        move.move_project(str(old_path), str(new_path))
