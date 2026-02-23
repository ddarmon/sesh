"""Bookmark persistence for sesh sessions."""

from __future__ import annotations

import json

from sesh.paths import CONFIG_DIR

BOOKMARKS_FILE = CONFIG_DIR / "bookmarks.json"


def load_bookmarks() -> set[tuple[str, str]]:
    """Load bookmarks as a set of (provider_value, session_id) tuples."""
    if not BOOKMARKS_FILE.is_file():
        return set()
    try:
        with open(BOOKMARKS_FILE) as f:
            data = json.load(f)
        return {
            (b["provider"], b["session_id"])
            for b in data.get("bookmarks", [])
        }
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return set()


def save_bookmarks(bookmarks: set[tuple[str, str]]) -> None:
    """Save bookmarks to disk."""
    BOOKMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "bookmarks": [
            {"provider": p, "session_id": s}
            for p, s in sorted(bookmarks)
        ]
    }
    try:
        with open(BOOKMARKS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
