from __future__ import annotations

import json

from sesh import bookmarks


def test_save_load_roundtrip(tmp_cache_dir) -> None:
    """Bookmarks survive a save-then-load cycle."""
    data = {("claude", "s2"), ("codex", "s1")}
    bookmarks.save_bookmarks(data)
    assert bookmarks.load_bookmarks() == data


def test_load_missing_file(tmp_cache_dir) -> None:
    """Missing bookmarks file returns an empty set rather than crashing."""
    assert bookmarks.load_bookmarks() == set()


def test_load_corrupt_json(tmp_cache_dir) -> None:
    """Corrupt JSON in the bookmarks file is treated as empty, not an error."""
    bookmarks.BOOKMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    bookmarks.BOOKMARKS_FILE.write_text("{not-json")
    assert bookmarks.load_bookmarks() == set()


def test_load_empty_bookmarks_list(tmp_cache_dir) -> None:
    """A valid file with an empty bookmarks array loads as an empty set."""
    bookmarks.BOOKMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    bookmarks.BOOKMARKS_FILE.write_text('{"bookmarks": []}')
    assert bookmarks.load_bookmarks() == set()


def test_creates_cache_dir(tmp_cache_dir) -> None:
    """Saving bookmarks auto-creates the parent directory if absent."""
    assert not bookmarks.BOOKMARKS_FILE.parent.exists()
    bookmarks.save_bookmarks({("claude", "s1")})
    assert bookmarks.BOOKMARKS_FILE.parent.is_dir()


def test_sorted_output(tmp_cache_dir) -> None:
    """Saved JSON bookmarks are sorted by (provider, session_id) for stable diffs."""
    bookmarks.save_bookmarks({("codex", "b"), ("claude", "a"), ("claude", "c")})
    data = json.loads(bookmarks.BOOKMARKS_FILE.read_text())
    assert data["bookmarks"] == [
        {"provider": "claude", "session_id": "a"},
        {"provider": "claude", "session_id": "c"},
        {"provider": "codex", "session_id": "b"},
    ]
