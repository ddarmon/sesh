"""XDG Base Directory paths for sesh app-managed data."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg_cache_home() -> Path:
    val = os.environ.get("XDG_CACHE_HOME", "")
    if val and Path(val).is_absolute():
        return Path(val)
    return Path.home() / ".cache"


def _xdg_config_home() -> Path:
    val = os.environ.get("XDG_CONFIG_HOME", "")
    if val and Path(val).is_absolute():
        return Path(val)
    return Path.home() / ".config"


CACHE_DIR = _xdg_cache_home() / "sesh"
CONFIG_DIR = _xdg_config_home() / "sesh"
