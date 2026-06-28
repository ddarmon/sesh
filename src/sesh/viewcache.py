"""Persistent HTML view cache for ``sesh view``.

``sesh view`` renders a session to a self-contained HTML file and opens it
in the browser. Writing to a *stable* per-session path (rather than a
random ``mkstemp`` temp file) means re-running ``sesh view`` on the same
session reuses the same ``file://`` URL, so the browser refreshes the
existing tab instead of opening a new one (paired with
``webbrowser.open(url, new=0)``).

The files are pure cache -- always regenerable from the session -- so they
can be deleted at any time. That unlocks a zero-bookkeeping cleanup policy:

-   :func:`sweep_view_cache` opportunistically GCs files on each view. A
    file is *kept* only if it is **both** among the ``keep_newest``
    most-recently-modified files **and** newer than ``max_age_days`` --
    equivalently, it is deleted when it is old **or** beyond the newest
    ``keep_newest``. The two triggers are independent on purpose: age ages
    out view-once sessions, and the count cap *also* bounds the burst case
    (scripting ``sesh view`` over many sessions leaves many *fresh* files
    that no age threshold would touch). Re-viewing a session rewrites its
    file (fresh mtime), so frequently viewed sessions survive.
-   :func:`remove_view` drops a session's file when the session itself is
    deleted, so a stale view of a deleted session can't linger.

Security: the original ``mkstemp`` approach used ``O_EXCL`` at mode 0600 to
defend against a symlink pre-planted in the shared, world-writable temp
dir, and to keep the rendered (possibly sensitive) transcript out of a
world-readable location. We preserve both properties by *relocating*
rather than *randomizing*: files live in a user-private cache dir (created
0700 when sesh first makes it) and are written 0600 via a private
``mkstemp`` temp file that is then atomically ``os.replace``-d onto the
stable path. The atomic rename also makes concurrent ``sesh view`` runs of
the same session safe (no truncate/interleave) and means a symlink
pre-planted at the stable path is *replaced*, never written through.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

from sesh.paths import VIEWS_DIR

# Opportunistic GC thresholds (see module docstring). Files are
# regenerable, so deletion is always safe.
MAX_AGE_DAYS = 7
KEEP_NEWEST = 50

# Cap the stem length so the final ``{stem}.html`` stays well under the
# common 255-byte filename limit; longer ids get a hash suffix instead.
_MAX_STEM = 128
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_stem(session_id: str) -> str:
    """Map a session id to a filename stem that can't escape ``VIEWS_DIR``.

    Session ids are normally short UUIDs/hashes, which pass through
    unchanged. Anything outside a conservative allowlist is replaced and
    leading dots are stripped so the result is a plain, traversal-safe
    filename. When sanitizing *altered* the id, or the id is over-long, a
    short hash of the original is appended so the mapping stays injective
    (two distinct ids can't collide onto the same file -- which would let
    one session's delete/sweep clobber another's view).
    """
    cleaned = _UNSAFE.sub("_", session_id).lstrip(".")
    if cleaned == session_id and len(cleaned) <= _MAX_STEM:
        return cleaned or "session"
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:12]
    base = (cleaned or "session")[:_MAX_STEM]
    return f"{base}-{digest}"


def view_path(session_id: str) -> Path:
    """Return the stable HTML view path for ``session_id``."""
    return VIEWS_DIR / f"{_safe_stem(session_id)}.html"


def write_view(session_id: str, content: str) -> Path:
    """Write ``content`` to the session's view file and return its path.

    Written atomically: ``content`` goes to a private 0600 ``mkstemp`` file
    in ``VIEWS_DIR`` (so a symlink can't redirect the write), then
    ``os.replace`` renames it onto the stable path. The rename makes
    concurrent same-session views safe and bumps mtime (keeping frequently
    viewed sessions alive through :func:`sweep_view_cache`). Raises
    ``OSError`` on failure.
    """
    VIEWS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = view_path(session_id)
    fd, tmp = tempfile.mkstemp(dir=VIEWS_DIR, prefix=".tmp-", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
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

    Deletes any ``*.html`` in ``VIEWS_DIR`` that is older than
    ``max_age_days`` **or** beyond the ``keep_newest`` most-recently
    modified files (so a file survives only if it is both fresh and recent
    -- see module docstring). ``keep_newest=0`` keeps none. ``now`` is
    injectable for deterministic tests.
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

    # Newest first: index < keep_newest is the recency cap; mtime >= cutoff
    # is the age cap. A tie at the recency boundary falls back to glob order
    # (filesystem-dependent), which is irrelevant since files are regenerable.
    entries.sort(key=lambda t: t[1], reverse=True)
    cutoff = now - max_age_days * 86400
    for i, (path, mtime) in enumerate(entries):
        if i < keep_newest and mtime >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            pass
