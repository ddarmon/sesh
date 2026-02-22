from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sesh import cache
from sesh.models import Project, Provider
from tests.helpers import make_session


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_session_serialization_roundtrip(tmp_cache_dir) -> None:
    """All SessionMeta fields survive a dict round-trip through the cache layer."""
    session = make_session(
        id="abc",
        project_path="/repo",
        provider=Provider.CODEX,
        summary="hello",
        start_timestamp=datetime(2025, 1, 2, 3, 0, 0, tzinfo=timezone.utc),
        message_count=5,
        model="gpt-4.1",
        source_path="/tmp/session.jsonl",
        timestamp=datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    payload = cache._session_to_dict(session)
    rebuilt = cache._dict_to_session(payload)
    assert rebuilt == session


def test_dict_to_session_z_suffix(tmp_cache_dir) -> None:
    """ISO timestamps ending in 'Z' (common in Claude JSONL) parse as UTC."""
    session = cache._dict_to_session(
        {
            "id": "s1",
            "project_path": "/p",
            "provider": "claude",
            "summary": "x",
            "timestamp": "2025-01-01T12:00:00Z",
        }
    )
    assert session.timestamp == datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_dict_to_session_missing_optional_fields(tmp_cache_dir) -> None:
    """Missing optional fields (model, source_path, message_count) get safe defaults."""
    session = cache._dict_to_session(
        {
            "id": "s1",
            "project_path": "/p",
            "provider": "claude",
            "summary": "x",
            "timestamp": "2025-01-01T00:00:00+00:00",
        }
    )
    assert session.start_timestamp is None
    assert session.model is None
    assert session.source_path is None
    assert session.message_count == 0


def test_dict_to_session_parses_optional_start_timestamp(tmp_cache_dir) -> None:
    """Optional start_timestamp field is parsed like timestamp when present."""
    session = cache._dict_to_session(
        {
            "id": "s1",
            "project_path": "/p",
            "provider": "claude",
            "summary": "x",
            "timestamp": "2025-01-01T12:00:00Z",
            "start_timestamp": "2025-01-01T11:30:00Z",
        }
    )
    assert session.start_timestamp == datetime(2025, 1, 1, 11, 30, tzinfo=timezone.utc)


def test_dict_to_session_invalid_timestamp(tmp_cache_dir) -> None:
    """Non-string timestamp (e.g. integer) falls back to utcnow rather than crashing."""
    before = datetime.now(tz=timezone.utc)
    session = cache._dict_to_session(
        {
            "id": "s1",
            "project_path": "/p",
            "provider": "claude",
            "summary": "x",
            "timestamp": 12345,
        }
    )
    after = datetime.now(tz=timezone.utc)
    assert before <= session.timestamp <= after


def test_put_get_roundtrip(tmp_cache_dir, tmp_path: Path) -> None:
    """Per-file cache: put then get returns the same sessions."""
    file_path = tmp_path / "session.jsonl"
    _write(file_path, "line1\n")
    sc = cache.SessionCache()
    sessions = [make_session(id="s1", source_path=str(file_path))]
    sc.put_sessions(str(file_path), sessions)
    assert sc.get_sessions(str(file_path)) == sessions


def test_cache_miss_no_entry(tmp_cache_dir, tmp_path: Path) -> None:
    """Per-file cache: querying an uncached path returns None."""
    file_path = tmp_path / "session.jsonl"
    _write(file_path, "line1\n")
    sc = cache.SessionCache()
    assert sc.get_sessions(str(file_path)) is None


def test_cache_invalidation_mtime(tmp_cache_dir, tmp_path: Path) -> None:
    """Per-file cache: changing the file's mtime invalidates the entry."""
    file_path = tmp_path / "session.jsonl"
    _write(file_path, "line1\n")
    sc = cache.SessionCache()
    sc.put_sessions(str(file_path), [make_session(source_path=str(file_path))])
    stat = file_path.stat()
    file_path.touch()
    if file_path.stat().st_mtime == stat.st_mtime:
        file_path.touch()
        stat2 = file_path.stat()
        file_path.touch()
        if file_path.stat().st_mtime == stat2.st_mtime:
            # Force a deterministic mtime shift if filesystem resolution is coarse.
            import os

            os.utime(file_path, (stat.st_atime, stat.st_mtime + 10))
    assert sc.get_sessions(str(file_path)) is None


def test_cache_invalidation_size(tmp_cache_dir, tmp_path: Path) -> None:
    """Per-file cache: changing the file's size invalidates the entry."""
    file_path = tmp_path / "session.jsonl"
    _write(file_path, "line1\n")
    sc = cache.SessionCache()
    sc.put_sessions(str(file_path), [make_session(source_path=str(file_path))])
    _write(file_path, "line1\nline2\n")
    assert sc.get_sessions(str(file_path)) is None


def test_cache_miss_file_deleted(tmp_cache_dir, tmp_path: Path) -> None:
    """Per-file cache: deleting the source file invalidates the entry."""
    file_path = tmp_path / "session.jsonl"
    _write(file_path, "line1\n")
    sc = cache.SessionCache()
    sc.put_sessions(str(file_path), [make_session(source_path=str(file_path))])
    file_path.unlink()
    assert sc.get_sessions(str(file_path)) is None


def test_dir_put_get_roundtrip(tmp_cache_dir, tmp_path: Path) -> None:
    """Per-directory cache (used by Claude provider): put then get returns same sessions."""
    dir_path = tmp_path / "claude-project"
    _write(dir_path / "a.jsonl", "{}\n")
    sc = cache.SessionCache()
    sessions = [make_session(source_path=str(dir_path))]
    sc.put_sessions_for_dir(str(dir_path), sessions)
    assert sc.get_sessions_for_dir(str(dir_path)) == sessions


def test_dir_invalidation_on_new_file(tmp_cache_dir, tmp_path: Path) -> None:
    """Per-directory cache: adding a new JSONL file invalidates the entry."""
    dir_path = tmp_path / "claude-project"
    _write(dir_path / "a.jsonl", "{}\n")
    sc = cache.SessionCache()
    sc.put_sessions_for_dir(str(dir_path), [make_session(source_path=str(dir_path))])
    _write(dir_path / "b.jsonl", "{}\n")
    assert sc.get_sessions_for_dir(str(dir_path)) is None


def test_dir_ignores_agent_files(tmp_cache_dir, tmp_path: Path) -> None:
    """agent-*.jsonl files are excluded from the directory fingerprint (Claude sub-agent noise)."""
    dir_path = tmp_path / "claude-project"
    _write(dir_path / "a.jsonl", "{}\n")
    sc = cache.SessionCache()
    sessions = [make_session(source_path=str(dir_path))]
    sc.put_sessions_for_dir(str(dir_path), sessions)
    _write(dir_path / "agent-foo.jsonl", "{}\n")
    assert sc.get_sessions_for_dir(str(dir_path)) == sessions


def test_dir_fingerprint_none_missing_dir(tmp_cache_dir, tmp_path: Path) -> None:
    """Fingerprinting a nonexistent directory returns None."""
    assert cache.SessionCache._dir_fingerprint(str(tmp_path / "missing")) is None


def test_save_load_roundtrip(tmp_cache_dir, tmp_path: Path) -> None:
    """Cache persists to disk: save() then a fresh SessionCache() sees the same data."""
    file_path = tmp_path / "session.jsonl"
    _write(file_path, "line1\n")
    sc = cache.SessionCache()
    sessions = [make_session(id="persist", source_path=str(file_path))]
    sc.put_sessions(str(file_path), sessions)
    sc.save()

    fresh = cache.SessionCache()
    assert fresh.get_sessions(str(file_path)) == sessions


def test_load_corrupt_file(tmp_cache_dir) -> None:
    """Corrupt cache file on disk is silently ignored (starts with empty cache)."""
    cache.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache.CACHE_FILE.write_text("{bad")
    sc = cache.SessionCache()
    assert sc._cache == {}


def test_load_missing_file(tmp_cache_dir) -> None:
    """No cache file on disk starts with an empty cache."""
    sc = cache.SessionCache()
    assert sc._cache == {}


def test_save_load_index_roundtrip(tmp_cache_dir) -> None:
    """Projects and sessions in the index survive a save/load round-trip."""
    project = Project(
        path="/repo",
        display_name="repo",
        providers={Provider.CLAUDE, Provider.CODEX},
        session_count=2,
        latest_activity=datetime(2025, 1, 3, tzinfo=timezone.utc),
    )
    sessions = [
        make_session(id="s1", project_path="/repo", provider=Provider.CLAUDE),
        make_session(id="s2", project_path="/repo", provider=Provider.CODEX),
    ]
    cache.save_index({project.path: project}, {project.path: sessions})
    data = cache.load_index()
    assert data is not None
    assert len(data["projects"]) == 1
    assert data["projects"][0]["path"] == "/repo"
    assert sorted(data["projects"][0]["providers"]) == ["claude", "codex"]
    assert len(data["sessions"]) == 2
    assert {s["id"] for s in data["sessions"]} == {"s1", "s2"}


def test_load_index_missing(tmp_cache_dir) -> None:
    """Missing index file returns None."""
    assert cache.load_index() is None


def test_load_index_corrupt(tmp_cache_dir) -> None:
    """Corrupt index file returns None rather than crashing."""
    cache.INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache.INDEX_FILE.write_text("{bad")
    assert cache.load_index() is None


def test_project_paths_roundtrip(tmp_cache_dir) -> None:
    """Project path cache (encoded-name -> resolved path) survives a save/load cycle."""
    mapping = {"Users-me-proj": {"path": "/Users/me/proj", "mtime": 123.0}}
    cache.save_project_paths(mapping)
    assert cache.load_project_paths() == mapping


def test_project_paths_missing(tmp_cache_dir) -> None:
    """Missing project paths file returns an empty dict."""
    assert cache.load_project_paths() == {}
