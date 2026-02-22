from __future__ import annotations

import json

from sesh import bookmarks


def test_save_load_roundtrip(tmp_cache_dir) -> None:
    data = {("claude", "s2"), ("codex", "s1")}
    bookmarks.save_bookmarks(data)
    assert bookmarks.load_bookmarks() == data


def test_load_missing_file(tmp_cache_dir) -> None:
    assert bookmarks.load_bookmarks() == set()


def test_load_corrupt_json(tmp_cache_dir) -> None:
    bookmarks.BOOKMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    bookmarks.BOOKMARKS_FILE.write_text("{not-json")
    assert bookmarks.load_bookmarks() == set()


def test_load_empty_bookmarks_list(tmp_cache_dir) -> None:
    bookmarks.BOOKMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    bookmarks.BOOKMARKS_FILE.write_text('{"bookmarks": []}')
    assert bookmarks.load_bookmarks() == set()


def test_creates_cache_dir(tmp_cache_dir) -> None:
    assert not bookmarks.BOOKMARKS_FILE.parent.exists()
    bookmarks.save_bookmarks({("claude", "s1")})
    assert bookmarks.BOOKMARKS_FILE.parent.is_dir()


def test_sorted_output(tmp_cache_dir) -> None:
    bookmarks.save_bookmarks({("codex", "b"), ("claude", "a"), ("claude", "c")})
    data = json.loads(bookmarks.BOOKMARKS_FILE.read_text())
    assert data["bookmarks"] == [
        {"provider": "claude", "session_id": "a"},
        {"provider": "claude", "session_id": "c"},
        {"provider": "codex", "session_id": "b"},
    ]
