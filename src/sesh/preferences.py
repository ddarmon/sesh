"""Persistence for TUI view preferences."""

from __future__ import annotations

import json
from pathlib import Path

PREFERENCES_FILE = Path.home() / ".cache" / "sesh" / "preferences.json"

DEFAULT_PREFERENCES = {
    "provider_filter": None,
    "sort_mode": "date",
    "show_tools": False,
    "show_thinking": False,
    "fullscreen": False,
}

_VALID_PROVIDER_FILTERS = {None, "claude", "codex", "cursor"}
_VALID_SORT_MODES = {"date", "name", "messages", "timeline"}


def _normalize_preferences(data: object) -> dict:
    prefs = dict(DEFAULT_PREFERENCES)
    if not isinstance(data, dict):
        return prefs

    provider_filter = data.get("provider_filter")
    if provider_filter in _VALID_PROVIDER_FILTERS:
        prefs["provider_filter"] = provider_filter

    sort_mode = data.get("sort_mode")
    if sort_mode in _VALID_SORT_MODES:
        prefs["sort_mode"] = sort_mode

    for key in ("show_tools", "show_thinking", "fullscreen"):
        value = data.get(key)
        if isinstance(value, bool):
            prefs[key] = value

    return prefs


def load_preferences() -> dict:
    """Load preferences, returning defaults for missing/corrupt data."""
    if not PREFERENCES_FILE.is_file():
        return dict(DEFAULT_PREFERENCES)
    try:
        with open(PREFERENCES_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_PREFERENCES)
    return _normalize_preferences(data)


def save_preferences(prefs: dict) -> None:
    """Save preferences to disk, keeping only known validated keys."""
    PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = _normalize_preferences(prefs)
    try:
        with open(PREFERENCES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
