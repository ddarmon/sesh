from __future__ import annotations

from sesh import preferences


def test_load_preferences_missing_file_returns_defaults(tmp_cache_dir) -> None:
    """Missing preferences file returns the default view settings."""
    assert preferences.load_preferences() == preferences.DEFAULT_PREFERENCES


def test_save_and_load_preferences_roundtrip(tmp_cache_dir) -> None:
    """Known preference keys persist across save/load."""
    prefs = {
        "provider_filter": "claude",
        "sort_mode": "timeline",
        "show_tools": True,
        "show_thinking": True,
        "fullscreen": True,
    }

    preferences.save_preferences(prefs)

    assert preferences.load_preferences() == prefs


def test_load_preferences_corrupt_json_returns_defaults(tmp_cache_dir) -> None:
    """Corrupt JSON is ignored instead of raising."""
    preferences.PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    preferences.PREFERENCES_FILE.write_text("{bad")

    assert preferences.load_preferences() == preferences.DEFAULT_PREFERENCES


def test_load_preferences_ignores_unknown_keys(tmp_cache_dir) -> None:
    """Unknown keys are dropped and missing known keys fall back to defaults."""
    preferences.PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    preferences.PREFERENCES_FILE.write_text(
        """
        {
          "provider_filter": "cursor",
          "unknown": 123,
          "show_tools": true
        }
        """.strip()
    )

    assert preferences.load_preferences() == {
        "provider_filter": "cursor",
        "sort_mode": "date",
        "show_tools": True,
        "show_thinking": False,
        "fullscreen": False,
    }
