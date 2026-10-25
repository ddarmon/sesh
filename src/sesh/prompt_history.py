"""Guarded rewrites of provider history files shared with live CLIs.

Prompt-history files (``~/.claude/history.jsonl``, ``~/.codex/history.jsonl``,
...) are appended to by every running agent process, so a plain
read-filter-write would drop any line appended in between.  The rewrite here
streams the filtered copy into a private temp file beside the original,
re-stats the original just before ``os.replace``, and starts over when it
changed.  That narrows the race to the gap between the final stat and the
rename; it does not close it (the CLIs take no lock sesh could share).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

ATTEMPTS = 5


class HistoryWriteError(OSError):
    """A history file kept changing underneath every rewrite attempt."""


@dataclass
class HistoryReport:
    """What ``SessionProvider.delete_history`` removed (or would remove)."""

    supported: bool = True
    # File path -> number of entries removed from it.
    removed: dict[str, int] = field(default_factory=dict)
    # Whole files deleted alongside the entries (e.g. Claude paste-cache).
    files_removed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "removed": dict(self.removed),
            "files_removed": list(self.files_removed),
        }


class _Discard:
    def write(self, _text: str) -> int:
        return 0


def _rewrite_guarded(
    path: Path,
    write_filtered: Callable[[IO[str], IO[str]], int],
    dry_run: bool,
) -> int:
    """Run ``write_filtered(src, dst)`` over ``path`` and swap the result in.

    ``write_filtered`` returns how many entries it dropped and must be safe to
    call again from scratch, since a concurrent append triggers a retry.
    Returns the dropped count; the file is left untouched when it is zero, on
    a dry run, or when ``path`` does not exist.
    """
    for _ in range(ATTEMPTS):
        try:
            before = os.stat(path)
        except FileNotFoundError:
            return 0

        if dry_run:
            with open(path, encoding="utf-8", errors="surrogateescape", newline="") as src:
                return write_filtered(src, _Discard())

        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with (
                os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as dst,
                open(path, encoding="utf-8", errors="surrogateescape", newline="") as src,
            ):
                removed = write_filtered(src, dst)
            if removed == 0:
                os.unlink(tmp)
                return 0
            os.chmod(tmp, stat.S_IMODE(before.st_mode))
            after = os.stat(path)
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                os.unlink(tmp)
                continue
            os.replace(tmp, path)
            return removed
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    raise HistoryWriteError(f"{path} kept changing during rewrite; left as is")


def remove_jsonl_entries(
    path: Path, drop: Callable[[dict], bool], dry_run: bool = False
) -> int:
    """Remove JSONL lines whose object satisfies ``drop``.

    Lines that are blank, unparseable, or not objects are kept verbatim.
    """

    def write_filtered(src: IO[str], dst: IO[str]) -> int:
        removed = 0
        for line in src:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                entry = None
            if isinstance(entry, dict) and drop(entry):
                removed += 1
                continue
            dst.write(line)
        return removed

    return _rewrite_guarded(path, write_filtered, dry_run)


def remove_json_array_entries(
    path: Path, drop: Callable[[dict], bool], dry_run: bool = False
) -> int:
    """Remove elements of a top-level JSON array that satisfy ``drop``.

    A file that is not a JSON array is left untouched.  The rewrite uses
    two-space indentation without ASCII escaping, matching Gemini CLI's own
    ``JSON.stringify(..., null, 2)``.
    """

    def write_filtered(src: IO[str], dst: IO[str]) -> int:
        try:
            data = json.load(src)
        except json.JSONDecodeError:
            return 0
        if not isinstance(data, list):
            return 0
        kept = [e for e in data if not (isinstance(e, dict) and drop(e))]
        removed = len(data) - len(kept)
        if removed:
            json.dump(kept, dst, indent=2, ensure_ascii=False)
        return removed

    return _rewrite_guarded(path, write_filtered, dry_run)
