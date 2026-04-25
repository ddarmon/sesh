"""Backend-agnostic core for the snapshot subsystem.

Captures, persists, and reopens Terminal-tab snapshots. Everything
platform-specific is reached through `sesh.snapshots.backend.get_backend`.
Resume metadata is resolved at save time (explicit-line parsing first,
then ripgrep-based search recovery) so reopens stay deterministic.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from sesh.models import Provider
from sesh.paths import SNAPSHOTS_DIR
from sesh.resume import RESUME_COMMANDS, resume_argv
from sesh.snapshots.backend import RestoreOutcome, get_backend


SCHEMA_VERSION = 1

# Hard cap on stored scrollback. The original bash scripts kept 30 lines;
# a small buffer above that gives search recovery a few extra phrases to
# work with without bloating snapshot files.
SCROLLBACK_MAX_LINES = 40

# Maximum number of distinct phrases we'll feed to ripgrep when trying to
# recover a session id from scrollback alone. Mirrors _sesh_match.py.
_SEARCH_PHRASE_LIMIT = 8


# ----- exceptions ----------------------------------------------------------


class SnapshotsError(Exception):
    """Base class for snapshot-related errors."""


class SnapshotsUnsupportedError(SnapshotsError):
    """Raised on platforms where no terminal backend is available."""


class SnapshotsNotFoundError(SnapshotsError):
    """Raised when a snapshot id does not match any stored file."""


class SnapshotsSchemaError(SnapshotsError):
    """Raised when a stored snapshot file has an unsupported schema version."""


# ----- dataclasses ---------------------------------------------------------


@dataclass
class SnapshotResume:
    provider: Provider
    session_id: str
    cmd_args: list[str]
    source: Literal["explicit", "search"]
    matched_phrase: str | None = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider.value,
            "session_id": self.session_id,
            "cmd_args": list(self.cmd_args),
            "source": self.source,
            "matched_phrase": self.matched_phrase,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotResume":
        source = d.get("source", "explicit")
        if source not in ("explicit", "search"):
            source = "explicit"
        return cls(
            provider=Provider(d["provider"]),
            session_id=d["session_id"],
            cmd_args=list(d.get("cmd_args") or []),
            source=source,
            matched_phrase=d.get("matched_phrase"),
        )


@dataclass
class SnapshotTab:
    window: int
    tab: int
    tty: str | None
    cwd: str | None
    scrollback_tail: str
    resume: SnapshotResume | None = None

    def to_dict(self) -> dict:
        return {
            "window": self.window,
            "tab": self.tab,
            "tty": self.tty,
            "cwd": self.cwd,
            "scrollback_tail": self.scrollback_tail,
            "resume": self.resume.to_dict() if self.resume else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotTab":
        resume = d.get("resume")
        return cls(
            window=int(d["window"]),
            tab=int(d["tab"]),
            tty=d.get("tty"),
            cwd=d.get("cwd"),
            scrollback_tail=d.get("scrollback_tail", ""),
            resume=SnapshotResume.from_dict(resume) if resume else None,
        )


@dataclass
class Snapshot:
    schema_version: int
    id: str
    created_at: str
    host: str
    tabs: list[SnapshotTab] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "created_at": self.created_at,
            "host": self.host,
            "tabs": [t.to_dict() for t in self.tabs],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        version = d.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SnapshotsSchemaError(
                f"Unsupported snapshot schema_version {version!r}; "
                f"expected {SCHEMA_VERSION}"
            )
        tabs = [SnapshotTab.from_dict(t) for t in d.get("tabs", [])]
        # Defense in depth: re-clamp scrollback if a backend forgot to.
        for t in tabs:
            t.scrollback_tail = _clamp_scrollback(t.scrollback_tail)
        return cls(
            schema_version=version,
            id=d["id"],
            created_at=d["created_at"],
            host=d.get("host", ""),
            tabs=tabs,
        )


@dataclass
class SnapshotSummary:
    id: str
    created_at: str
    host: str
    tab_count: int
    resumable_count: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "host": self.host,
            "tab_count": self.tab_count,
            "resumable_count": self.resumable_count,
        }


@dataclass
class RestoreItem:
    window: int
    tab: int
    cwd: str | None
    cmd_args: list[str] | None
    label: str
    reason_skipped: str | None = None

    def to_dict(self) -> dict:
        return {
            "window": self.window,
            "tab": self.tab,
            "cwd": self.cwd,
            "cmd_args": list(self.cmd_args) if self.cmd_args else None,
            "label": self.label,
            "reason_skipped": self.reason_skipped,
        }


@dataclass
class RestorePlan:
    snapshot_id: str
    items: list[RestoreItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "items": [it.to_dict() for it in self.items],
        }


@dataclass
class RestoreReport:
    snapshot_id: str
    plan: RestorePlan
    launched: int = 0
    fellback: bool = False
    host_mismatch: bool = False
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "plan": self.plan.to_dict(),
            "launched": self.launched,
            "fellback": self.fellback,
            "host_mismatch": self.host_mismatch,
            "note": self.note,
        }


@dataclass
class PreviewResult:
    """Modal result from `SnapshotPreviewScreen`."""

    confirmed: bool
    include_shells: bool


# ----- helpers -------------------------------------------------------------


def _clamp_scrollback(text: str) -> str:
    if not text:
        return text
    lines = text.splitlines()
    if len(lines) <= SCROLLBACK_MAX_LINES:
        return text
    return "\n".join(lines[-SCROLLBACK_MAX_LINES:])


def _snapshots_dir() -> Path:
    """Resolve the snapshots dir lazily so monkeypatching `SNAPSHOTS_DIR`
    on this module is honored by every entry point."""
    return SNAPSHOTS_DIR


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _generate_snapshot_id(now: datetime | None = None) -> str:
    base = (now or datetime.now()).strftime("snapshot-%Y%m%d-%H%M%S")
    candidate = base
    n = 1
    target_dir = _snapshots_dir()
    while (target_dir / f"{candidate}.json").exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _normalize_path(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return os.path.realpath(path)
    except OSError:
        return path


# ----- explicit-line parsing -----------------------------------------------


# Patterns for explicit `--resume` lines visible in scrollback. Order
# matters only for the regex itself; per-tab we always use the LAST match.
_RESUME_PATTERNS: list[tuple[Provider, re.Pattern[str]]] = [
    (
        Provider.CLAUDE,
        # claude --resume <id>  or  claude --resume "<id>"
        re.compile(r"\bclaude\s+--resume\s+(?:\"([^\"]+)\"|([A-Za-z0-9_-]+))"),
    ),
    (
        Provider.CODEX,
        re.compile(r"\bcodex\s+resume\s+([A-Za-z0-9_-]+)"),
    ),
    (
        Provider.CURSOR,
        re.compile(r"\bagent\s+--resume=([A-Za-z0-9_-]+)"),
    ),
    (
        Provider.COPILOT,
        re.compile(r"\bcopilot\s+--resume=([A-Za-z0-9_-]+)"),
    ),
]


def _parse_explicit_resume(scrollback: str) -> SnapshotResume | None:
    """Find the most recent explicit `--resume` invocation in scrollback."""
    if not scrollback:
        return None

    last_match: tuple[int, Provider, str] | None = None
    for provider, pattern in _RESUME_PATTERNS:
        for m in pattern.finditer(scrollback):
            session_id = next((g for g in m.groups() if g), "")
            if not session_id:
                continue
            if last_match is None or m.start() > last_match[0]:
                last_match = (m.start(), provider, session_id)

    if last_match is None:
        return None

    _, provider, session_id = last_match
    return SnapshotResume(
        provider=provider,
        session_id=session_id,
        cmd_args=resume_argv(provider, session_id),
        source="explicit",
        matched_phrase=None,
    )


# ----- search-based recovery ----------------------------------------------


_SKIP_PREFIXES = ("❯", ">", "─", "━", "-", "=", "╭", "╰", "│", "*", "✻", "✳", "※")
_SKIP_SUBSTR = ("Last login", "Restored session", "Resume this session", "for shortcuts")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0E-\x1F]")
_LEADING_CLAUDE_MARKER = "⏺ "  # "⏺ "


def _candidate_phrases(text: str) -> list[str]:
    """Generate distinctive scrollback phrases (newest first)."""
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) < 40:
            continue
        if s.startswith(_SKIP_PREFIXES):
            continue
        if any(sub in s for sub in _SKIP_SUBSTR):
            continue
        if _CONTROL_CHARS.search(s):
            continue
        if s.startswith(_LEADING_CLAUDE_MARKER):
            s = s[len(_LEADING_CLAUDE_MARKER):]
        words = s.split()
        if not words or any(words[0].startswith(c) for c in ("~/", "$", "#", "%", "/")):
            continue
        alpha = sum(c.isalpha() for c in s)
        if alpha < len(s) * 0.5:
            continue
        if len(words) < 5:
            continue
        phrase = " ".join(words[:7])
        if any(c in phrase for c in '"\\$`'):
            continue
        if phrase in seen:
            continue
        seen.add(phrase)
        out.append(phrase)
    return list(reversed(out))


def _index_mtime_lookup() -> dict[str, float] | None:
    """Build {session_id: mtime_seconds} from the index, if available."""
    from sesh.cache import load_index

    data = load_index()
    if not data:
        return None
    out: dict[str, float] = {}
    for sess in data.get("sessions", []):
        sid = sess.get("id")
        ts = sess.get("timestamp")
        if not sid or not ts:
            continue
        try:
            value = ts.replace("Z", "+00:00") if isinstance(ts, str) else ts
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
        out[sid] = dt.timestamp()
    return out


def _file_mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _search_recover(
    scrollback: str,
    cwd: str,
    *,
    index_mtimes: dict[str, float] | None = None,
) -> SnapshotResume | None:
    """Recover a session via ripgrep search over scrollback phrases."""
    if not scrollback or not cwd:
        return None

    from sesh.search import ripgrep_search

    cwd_norm = _normalize_path(cwd)
    phrases = _candidate_phrases(scrollback)[:_SEARCH_PHRASE_LIMIT]
    if not phrases:
        return None

    for phrase in phrases:
        try:
            results = ripgrep_search(phrase)
        except Exception:
            continue
        candidates = [
            r for r in results
            if r.session_id and _normalize_path(r.project_path) == cwd_norm
        ]
        if not candidates:
            continue

        # Tie-break: prefer the candidate with the most recent index mtime,
        # falling back to on-disk file mtime, then to result order.
        def _sort_key(r):
            sid = r.session_id
            idx_mtime = (index_mtimes or {}).get(sid, 0.0)
            file_mtime = _file_mtime(r.file_path)
            return (idx_mtime, file_mtime)

        candidates.sort(key=_sort_key, reverse=True)
        chosen = candidates[0]
        return SnapshotResume(
            provider=chosen.provider,
            session_id=chosen.session_id,
            cmd_args=resume_argv(chosen.provider, chosen.session_id),
            source="search",
            matched_phrase=phrase,
        )

    return None


# ----- public API ----------------------------------------------------------


def capture() -> Snapshot:
    """Capture the current terminal state, resolving resume metadata."""
    backend = get_backend()
    if backend is None:
        raise SnapshotsUnsupportedError(
            "Terminal.app snapshots are macOS-only — no supported "
            "terminal backend on this platform"
        )

    captured = backend.capture()
    index_mtimes = _index_mtime_lookup()

    tabs: list[SnapshotTab] = []
    for raw in captured:
        scrollback = _clamp_scrollback(raw.scrollback_tail or "")
        cwd = _normalize_path(raw.cwd)
        resume = _parse_explicit_resume(scrollback)
        if resume is None and cwd:
            resume = _search_recover(scrollback, cwd, index_mtimes=index_mtimes)
        tabs.append(
            SnapshotTab(
                window=raw.window,
                tab=raw.tab,
                tty=raw.tty,
                cwd=cwd,
                scrollback_tail=scrollback,
                resume=resume,
            )
        )

    return Snapshot(
        schema_version=SCHEMA_VERSION,
        id=_generate_snapshot_id(),
        created_at=_now_iso(),
        host=socket.gethostname(),
        tabs=tabs,
    )


def save(snapshot: Snapshot) -> Path:
    """Serialize a snapshot to disk and return its path."""
    target_dir = _snapshots_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{snapshot.id}.json"
    with open(path, "w") as f:
        json.dump(snapshot.to_dict(), f, indent=2)
    return path


def load(snapshot_id: str) -> Snapshot:
    """Load a snapshot by id, validating the schema version."""
    path = _snapshots_dir() / f"{snapshot_id}.json"
    if not path.is_file():
        raise SnapshotsNotFoundError(f"Snapshot '{snapshot_id}' not found")
    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise SnapshotsSchemaError(f"Snapshot '{snapshot_id}' is not valid JSON: {exc}") from exc
    return Snapshot.from_dict(data)


def list_snapshots() -> list[SnapshotSummary]:
    """Return snapshot summaries, newest first."""
    target_dir = _snapshots_dir()
    if not target_dir.is_dir():
        return []

    out: list[SnapshotSummary] = []
    for path in target_dir.glob("snapshot-*.json"):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        if data.get("schema_version") != SCHEMA_VERSION:
            continue

        tabs = data.get("tabs", []) or []
        resumable = sum(1 for t in tabs if t.get("resume"))
        out.append(
            SnapshotSummary(
                id=data.get("id", path.stem),
                created_at=data.get("created_at", ""),
                host=data.get("host", ""),
                tab_count=len(tabs),
                resumable_count=resumable,
            )
        )

    out.sort(key=lambda s: s.created_at, reverse=True)
    return out


def delete(snapshot_id: str) -> None:
    """Remove a snapshot file."""
    path = _snapshots_dir() / f"{snapshot_id}.json"
    if not path.is_file():
        raise SnapshotsNotFoundError(f"Snapshot '{snapshot_id}' not found")
    path.unlink()


def _build_label(
    *,
    window: int,
    tab: int,
    cwd: str | None,
    cmd_args: list[str] | None,
) -> str:
    cwd_str = cwd or "(no cwd)"
    if cmd_args:
        cmd_str = shlex.join(cmd_args)
        return f"Window {window}, Tab {tab}  {cwd_str}  [{cmd_str}]"
    return f"Window {window}, Tab {tab}  {cwd_str}  [shell]"


def build_restore_plan(
    snapshot: Snapshot,
    *,
    include_shells: bool = False,
) -> RestorePlan:
    """Build a deterministic restore plan; preserves window/tab order."""
    items: list[RestoreItem] = []
    for tab in snapshot.tabs:
        cmd_args: list[str] | None = None
        reason: str | None = None

        if tab.resume is not None:
            cmd_args = list(tab.resume.cmd_args)
            binary = cmd_args[0] if cmd_args else None
            if binary and shutil.which(binary) is None:
                reason = f"{binary} not on PATH"

        if tab.cwd is None:
            reason = reason or "cwd missing"
        elif not Path(tab.cwd).is_dir():
            reason = reason or f"cwd missing: {tab.cwd}"

        if tab.resume is None and not include_shells:
            reason = reason or "plain shell"

        items.append(
            RestoreItem(
                window=tab.window,
                tab=tab.tab,
                cwd=tab.cwd,
                cmd_args=cmd_args,
                label=_build_label(
                    window=tab.window,
                    tab=tab.tab,
                    cwd=tab.cwd,
                    cmd_args=cmd_args,
                ),
                reason_skipped=reason,
            )
        )

    return RestorePlan(snapshot_id=snapshot.id, items=items)


def restore(
    snapshot: Snapshot,
    *,
    include_shells: bool = False,
    dry_run: bool = False,
) -> RestoreReport:
    """Reopen the runnable subset of a snapshot's tabs."""
    plan = build_restore_plan(snapshot, include_shells=include_shells)
    host_mismatch = bool(snapshot.host) and snapshot.host != socket.gethostname()

    runnable = [it for it in plan.items if it.reason_skipped is None]

    if dry_run:
        return RestoreReport(
            snapshot_id=snapshot.id,
            plan=plan,
            launched=0,
            fellback=False,
            host_mismatch=host_mismatch,
            note="dry-run: no tabs spawned",
        )

    if not runnable:
        return RestoreReport(
            snapshot_id=snapshot.id,
            plan=plan,
            launched=0,
            fellback=False,
            host_mismatch=host_mismatch,
            note="no tabs to reopen",
        )

    backend = get_backend()
    if backend is None:
        raise SnapshotsUnsupportedError(
            "Terminal.app snapshots are macOS-only — no supported "
            "terminal backend on this platform"
        )

    outcome: RestoreOutcome = backend.restore(runnable)

    return RestoreReport(
        snapshot_id=snapshot.id,
        plan=plan,
        launched=outcome.launched,
        fellback=outcome.fellback,
        host_mismatch=host_mismatch,
        note=outcome.note,
    )
