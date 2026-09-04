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
        "show_agents": True,
        "fullscreen": True,
    }

    preferences.save_preferences(prefs)

    assert preferences.load_preferences() == prefs


def test_save_and_load_tokens_sort_mode_roundtrip(tmp_cache_dir) -> None:
    """The tokens sort mode persists across save/load."""
    prefs = dict(preferences.DEFAULT_PREFERENCES)
    prefs["sort_mode"] = "tokens"

    preferences.save_preferences(prefs)

    assert preferences.load_preferences()["sort_mode"] == "tokens"


def test_load_preferences_unknown_sort_mode_falls_back_to_default(tmp_cache_dir) -> None:
    """An unrecognized sort_mode value degrades to the default."""
    preferences.PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    preferences.PREFERENCES_FILE.write_text('{"sort_mode": "bogus"}')

    assert preferences.load_preferences()["sort_mode"] == "date"


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
        "show_agents": False,
        "fullscreen": False,
    }


def test_every_provider_is_a_valid_filter() -> None:
    """A new provider must not be silently dropped when preferences are saved."""
    from sesh.models import Provider
    from sesh.preferences import _normalize_preferences

    for provider in Provider:
        normalized = _normalize_preferences({"provider_filter": provider.value})
        assert normalized["provider_filter"] == provider.value


def test_unknown_provider_filter_falls_back_to_none() -> None:
    from sesh.preferences import _normalize_preferences

    assert _normalize_preferences({"provider_filter": "nope"})["provider_filter"] is None
