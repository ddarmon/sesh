"""Tests for the persistent HTML view cache (sesh view)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sesh import viewcache


@pytest.fixture()
def views_dir(tmp_cache_dir: Path) -> Path:
    """The redirected VIEWS_DIR (under tmp_cache_dir via conftest)."""
    return viewcache.VIEWS_DIR


def _touch(path: Path, mtime: float) -> None:
    """Create an empty file with a specific mtime."""
    path.write_text("x", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_view_path_is_stable(views_dir: Path) -> None:
    """The same session id always maps to the same path."""
    a = viewcache.view_path("abc-123")
    b = viewcache.view_path("abc-123")
    assert a == b
    assert a == views_dir / "abc-123.html"


def test_safe_stem_sanitizes_traversal(views_dir: Path) -> None:
    """A traversal-flavored id can't escape the views dir."""
    stem = viewcache._safe_stem("../../etc/passwd")
    # No path separators survive, and leading dots are stripped, so the
    # remaining '..' chars are inert filename characters, not components.
    assert "/" not in stem
    assert not stem.startswith(".")
    # The resolved path stays strictly inside VIEWS_DIR.
    resolved = viewcache.view_path("../../etc/passwd").resolve()
    assert resolved.parent == views_dir.resolve()


def test_safe_stem_empty_falls_back() -> None:
    assert viewcache._safe_stem("") == "session"
    # "..." sanitizes to empty, so it falls back to a "session-" hash stem
    # rather than a bare "session" (keeps the mapping injective).
    dots = viewcache._safe_stem("...")
    assert dots.startswith("session-")


def test_safe_stem_distinct_ids_dont_collide() -> None:
    """Ids that sanitize to the same chars stay distinct via a hash suffix."""
    assert viewcache._safe_stem("a/b") != viewcache._safe_stem("a_b")


def test_safe_stem_bounds_overlong_ids() -> None:
    """A very long id maps to a bounded, distinct filename (no OSError)."""
    long_id = "x" * 5000
    stem = viewcache._safe_stem(long_id)
    assert len(stem) <= viewcache._MAX_STEM + 13  # base + "-" + 12-char hash
    assert viewcache._safe_stem(long_id) != viewcache._safe_stem("x" * 4999)


def test_write_view_overwrites_in_place(views_dir: Path) -> None:
    """Re-writing the same session reuses the path (stable URL)."""
    p1 = viewcache.write_view("sess", "<html>one</html>")
    p2 = viewcache.write_view("sess", "<html>two</html>")
    assert p1 == p2
    assert p2.read_text(encoding="utf-8") == "<html>two</html>"


def test_write_view_permissions(views_dir: Path) -> None:
    """File is 0600 (written via mkstemp + atomic replace)."""
    p = viewcache.write_view("sess", "<html></html>")
    assert (p.stat().st_mode & 0o777) == 0o600


def test_write_view_replaces_symlink_without_following(views_dir: Path, tmp_path: Path) -> None:
    """A symlink pre-planted at the target is replaced, never written through."""
    views_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = tmp_path / "evil.html"
    os.symlink(target, viewcache.view_path("sess"))

    p = viewcache.write_view("sess", "<html>safe</html>")

    # The write went to a real regular file at the stable path, and the
    # symlink target was never created (atomic os.replace clobbers the link).
    assert not p.is_symlink()
    assert p.read_text(encoding="utf-8") == "<html>safe</html>"
    assert not target.exists()


def test_write_view_no_temp_files_left(views_dir: Path) -> None:
    """The mkstemp scratch file is renamed away, not left behind."""
    viewcache.write_view("sess", "<html></html>")
    leftover = [p.name for p in views_dir.iterdir() if p.name.startswith(".tmp-")]
    assert leftover == []


def test_remove_view(views_dir: Path) -> None:
    p = viewcache.write_view("sess", "<html></html>")
    assert p.exists()
    viewcache.remove_view("sess")
    assert not p.exists()


def test_remove_view_missing_is_silent(views_dir: Path) -> None:
    # No file written; should not raise.
    viewcache.remove_view("never-existed")


def test_sweep_deletes_old_keeps_fresh(views_dir: Path) -> None:
    views_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = 1_000_000.0
    fresh = views_dir / "fresh.html"
    old = views_dir / "old.html"
    _touch(fresh, now - 3600)  # 1 hour old
    _touch(old, now - 30 * 86400)  # 30 days old

    viewcache.sweep_view_cache(max_age_days=7, keep_newest=50, now=now)

    assert fresh.exists()
    assert not old.exists()


def test_sweep_count_cap_bounds_fresh_burst(views_dir: Path) -> None:
    """Even all-fresh files are capped at keep_newest, newest retained."""
    views_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = 1_000_000.0
    # 5 files, all young, with increasing mtime
    for i in range(5):
        _touch(views_dir / f"s{i}.html", now - (5 - i))

    viewcache.sweep_view_cache(max_age_days=7, keep_newest=2, now=now)

    remaining = sorted(p.name for p in views_dir.glob("*.html"))
    # The two newest (highest mtime) survive: s3, s4
    assert remaining == ["s3.html", "s4.html"]


def test_sweep_missing_dir_is_silent(tmp_cache_dir: Path) -> None:
    # VIEWS_DIR does not exist yet; sweep must not raise.
    assert not viewcache.VIEWS_DIR.exists()
    viewcache.sweep_view_cache(now=1_000_000.0)


def test_sweep_ignores_non_html(views_dir: Path) -> None:
    views_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = 1_000_000.0
    other = views_dir / "notes.txt"
    _touch(other, now - 365 * 86400)
    viewcache.sweep_view_cache(max_age_days=7, keep_newest=0, now=now)
    assert other.exists()
