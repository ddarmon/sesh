"""Persistent HTML view cache for ``sesh view``.

``sesh view`` renders a session to a self-contained HTML file and opens it
in the browser. Writing to a *stable* per-session path (rather than a
random ``mkstemp`` temp file) means re-running ``sesh view`` on the same
session reuses the same ``file://`` URL, so the browser refreshes the
existing tab instead of opening a new one (paired with
``webbrowser.open(url, new=0)``).

The files are pure cache -- always regenerable from the session -- so they
can be deleted at any time. That unlocks a zero-bookkeeping cleanup policy:

-   :func:`sweep_view_cache` opportunistically GCs old files on each view.
    It deletes any file that is *both* older than ``max_age_days`` *and*
    not among the ``keep_newest`` most-recently-modified files. Age ages
    out view-once sessions; the count cap bounds the burst case (scripting
    ``sesh view`` over many sessions leaves many *fresh* files that no age
    threshold would touch). Re-viewing a session rewrites its file (fresh
    mtime), so frequently viewed sessions survive automatically.
-   :func:`remove_view` drops a session's file when the session itself is
    deleted, so a stale view of a deleted session can't linger.

Security: the original ``mkstemp`` approach used ``O_EXCL`` at mode 0600 to
defend against a symlink pre-planted in the shared, world-writable temp
dir, and to keep the rendered (possibly sensitive) transcript out of a
world-readable location. We preserve both properties by *relocating*
rather than *randomizing*: files live in a user-private dir created 0700,
written 0600, opened ``O_NOFOLLOW`` so a symlink planted at the final path
component is refused. An attacker cannot pre-plant in your own private
cache dir.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from sesh.paths import VIEWS_DIR

# Opportunistic GC thresholds (see module docstring). Files are
# regenerable, so deletion is always safe.
MAX_AGE_DAYS = 7
KEEP_NEWEST = 50

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_stem(session_id: str) -> str:
    """Map a session id to a filename stem that can't escape ``VIEWS_DIR``.

    Session ids are normally UUIDs/hashes, but a provider id could in
    principle contain path separators or ``..``; replace anything outside
    a conservative allowlist and strip leading dots so the result is a
    plain, traversal-safe filename.
    """
    stem = _UNSAFE.sub("_", session_id).lstrip(".")
    return stem or "session"


def view_path(session_id: str) -> Path:
    """Return the stable HTML view path for ``session_id``."""
    return VIEWS_DIR / f"{_safe_stem(session_id)}.html"


def write_view(session_id: str, content: str) -> Path:
    """Write ``content`` to the session's view file and return its path.

    The file is written 0600 inside a 0700 dir, opened ``O_NOFOLLOW`` (a
    symlink at the path is refused) and ``O_TRUNC`` (overwrite in place,
    which bumps mtime and so keeps frequently viewed sessions alive
    through :func:`sweep_view_cache`). Raises ``OSError`` on failure.
    """
    VIEWS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = view_path(session_id)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def remove_view(session_id: str) -> None:
    """Best-effort removal of a session's view file (on session delete)."""
    try:
        view_path(session_id).unlink()
    except OSError:
        pass


def sweep_view_cache(
    *,
    max_age_days: int = MAX_AGE_DAYS,
    keep_newest: int = KEEP_NEWEST,
    now: float | None = None,
) -> None:
    """GC regenerable HTML view files. Best-effort; never raises.

    Deletes any ``*.html`` in ``VIEWS_DIR`` that is both older than
    ``max_age_days`` and not among the ``keep_newest`` most-recently
    modified files. ``now`` is injectable for deterministic tests.
    """
    if now is None:
        now = time.time()

    entries: list[tuple[Path, float]] = []
    try:
        for p in VIEWS_DIR.glob("*.html"):
            try:
                entries.append((p, p.stat().st_mtime))
            except OSError:
                continue
    except OSError:
        return

    entries.sort(key=lambda t: t[1], reverse=True)
    cutoff = now - max_age_days * 86400
    for i, (path, mtime) in enumerate(entries):
        if i < keep_newest and mtime >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            pass
