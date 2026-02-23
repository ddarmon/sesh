from __future__ import annotations

from pathlib import Path

import pytest

from sesh import paths


@pytest.mark.parametrize(
    ("env_var", "helper", "suffix"),
    [
        ("XDG_CACHE_HOME", paths._xdg_cache_home, ".cache"),
        ("XDG_CONFIG_HOME", paths._xdg_config_home, ".config"),
    ],
)
def test_xdg_home_unset_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
    helper,
    suffix: str,
) -> None:
    monkeypatch.delenv(env_var, raising=False)
    assert helper() == Path.home() / suffix


@pytest.mark.parametrize(
    ("env_var", "helper"),
    [
        ("XDG_CACHE_HOME", paths._xdg_cache_home),
        ("XDG_CONFIG_HOME", paths._xdg_config_home),
    ],
)
def test_xdg_home_uses_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_var: str,
    helper,
) -> None:
    custom = tmp_path / "xdg"
    monkeypatch.setenv(env_var, str(custom))
    assert helper() == custom


@pytest.mark.parametrize(
    ("env_var", "helper", "suffix"),
    [
        ("XDG_CACHE_HOME", paths._xdg_cache_home, ".cache"),
        ("XDG_CONFIG_HOME", paths._xdg_config_home, ".config"),
    ],
)
def test_xdg_home_empty_string_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
    helper,
    suffix: str,
) -> None:
    monkeypatch.setenv(env_var, "")
    assert helper() == Path.home() / suffix


@pytest.mark.parametrize(
    ("env_var", "helper", "suffix"),
    [
        ("XDG_CACHE_HOME", paths._xdg_cache_home, ".cache"),
        ("XDG_CONFIG_HOME", paths._xdg_config_home, ".config"),
    ],
)
def test_xdg_home_relative_path_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
    helper,
    suffix: str,
) -> None:
    monkeypatch.setenv(env_var, "relative/path")
    assert helper() == Path.home() / suffix
