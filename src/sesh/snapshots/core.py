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
from sesh.snapshots import live
from sesh.snapshots.backend import (
    CapturedTab,
    RestoreOutcome,
    TerminalBackendError,
    get_backend,
)


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


class SnapshotsBackendError(SnapshotsError):
    """Raised when the terminal backend failed to capture or reopen tabs."""


class SnapshotsNotFoundError(SnapshotsError):
    """Raised when a snapshot id does not match any stored file."""


class SnapshotsSchemaError(SnapshotsError):
    """Raised when a stored snapshot file has an unsupported schema version."""


# ----- dataclasses ---------------------------------------------------------


ResumeSource = Literal["process", "argv", "explicit", "activity", "search"]
RESUME_SOURCES: tuple[str, ...] = ("process", "argv", "explicit", "activity", "search")
GUESS_SOURCES: tuple[str, ...] = ("activity", "search")


@dataclass
class SnapshotResume:
    provider: Provider
    session_id: str
    cmd_args: list[str]
    # Which evidence named the session, strongest first:
    #   process   the provider's own record for the running process
    #   argv      a session id on the running process's command line
    #   explicit  a resume command in the scrollback of a tab with no agent
    #   activity  the only session active in the cwd since the process started
    #   search    a scrollback phrase found in a transcript
    source: ResumeSource
    matched_phrase: str | None = None

    @property
    def is_guess(self) -> bool:
        """True when the session was inferred rather than read off the process."""
        return self.source in GUESS_SOURCES

    def to_dict(self) -> dict:
        return {
            "provider": self.provider.value,
            "session_id": self.session_id,
            "cmd_args": list(self.cmd_args),
            "source": self.source,
            "guess": self.is_guess,
            "matched_phrase": self.matched_phrase,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotResume":
        source = d.get("source", "explicit")
        if source not in RESUME_SOURCES:
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
    # The resume command names a session that was inferred, not confirmed.
    guess: bool = False

    def to_dict(self) -> dict:
        return {
            "window": self.window,
            "tab": self.tab,
            "cwd": self.cwd,
            "cmd_args": list(self.cmd_args) if self.cmd_args else None,
            "label": self.label,
            "reason_skipped": self.reason_skipped,
            "guess": self.guess,
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
    (
        Provider.GEMINI,
        # gemini --resume <uuid>. Match full UUIDs only: "--resume latest"
        # and "--resume 3" (index) are not session ids.
        re.compile(
            r"\bgemini\s+--resume[\s=]+\"?"
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\"?"
        ),
    ),
    (
        Provider.PI,
        re.compile(r"\bpi\s+--session\s+([A-Za-z0-9_-]+)"),
    ),
    (
        Provider.OPENCODE,
        re.compile(r"\bopencode\s+--session\s+([A-Za-z0-9_-]+)"),
    ),
    (
        Provider.AGY,
        # agy --conversation <uuid>. Conversation ids are always UUIDs.
        re.compile(
            r"\bagy\s+--conversation[\s=]+\"?"
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\"?"
        ),
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


def _longest_literal_run(words: list[str]) -> list[str]:
    """The longest run of consecutive words that search treats as literal.

    A phrase is searched as a regex as soon as it holds one regex
    metacharacter, so "the departing teammate. (turn" is an invalid pattern
    and "what now?" matches "what no". Only metacharacter-free runs are used.
    """
    from sesh.search import _is_literal

    best: list[str] = []
    current: list[str] = []
    for word in words:
        if _is_literal(word) and not any(c in word for c in '"`'):
            current.append(word)
            if len(current) > len(best):
                best = list(current)
        else:
            current = []
    return best


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
        run = _longest_literal_run(words)
        if len(run) < 5:
            continue
        phrase = " ".join(run[:7])
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
    provider: Provider | None = None,
    candidate_ids: set[str] | None = None,
) -> SnapshotResume | None:
    """Recover a session via ripgrep search over scrollback phrases.

    *provider* limits hits to one provider (the agent running in the tab);
    *candidate_ids* limits them to known candidate sessions.
    """
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
            if r.session_id
            and r.provider in RESUME_COMMANDS  # skip providers with no resume-by-id CLI
            and (provider is None or r.provider == provider)
            and (candidate_ids is None or r.session_id in candidate_ids)
            and _normalize_path(r.project_path) == cwd_norm
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


# ----- process-based resolution ---------------------------------------------


def _discover_sessions() -> list:
    """Every local session, discovered fresh (the on-disk index can be stale)."""
    from sesh.cache import SessionCache
    from sesh.discovery import discover_all

    cache = SessionCache()
    _projects, sessions = discover_all(cache=cache)
    cache.save()
    return [s for group in sessions.values() for s in group]


class _ResolveContext:
    """Per-capture state shared across tabs; everything is loaded on demand."""

    def __init__(self, index_mtimes: dict[str, float] | None) -> None:
        self.index_mtimes = index_mtimes
        self._sessions: list | None = None
        self._providers: dict[Provider, object | None] = {}

    def sessions(self) -> list:
        if self._sessions is None:
            try:
                self._sessions = _discover_sessions()
            except Exception:
                self._sessions = []
        return self._sessions

    def provider(self, provider: Provider):
        if provider not in self._providers:
            from sesh.discovery import construct_provider

            try:
                self._providers[provider] = construct_provider(provider.value)
            except Exception:
                self._providers[provider] = None
        return self._providers[provider]


def _resume(provider: Provider, session_id: str, source: str, phrase: str | None = None):
    return SnapshotResume(
        provider=provider,
        session_id=session_id,
        cmd_args=resume_argv(provider, session_id),
        source=source,  # type: ignore[arg-type]
        matched_phrase=phrase,
    )


def _argv_resume(provider: Provider, argv: list[str]) -> SnapshotResume | None:
    """A session id on the agent's own command line.

    A fork (``--fork-session``, ``codex fork``, ``--fork``) names the session
    it copied from, not the one it is in, so it yields nothing.
    """
    if any(a == "fork" or a.startswith("--fork") for a in argv[1:]):
        return None
    command = " ".join(argv)
    for pattern_provider, pattern in _RESUME_PATTERNS:
        if pattern_provider != provider:
            continue
        m = pattern.search(command)
        if m:
            session_id = next((g for g in m.groups() if g), "")
            if session_id:
                return _resume(provider, session_id, "argv")
    return None


def _resolve_resume(
    raw: CapturedTab,
    scrollback: str,
    cwd: str | None,
    ctx: _ResolveContext,
) -> SnapshotResume | None:
    """Name the session a tab should resume. See ``sesh.snapshots.live``."""
    agent = live.find_agent(raw.processes)
    if agent is None:
        # No agent is running in the tab (or its processes are unknown):
        # the scrollback is the only evidence there is.
        resume = _parse_explicit_resume(scrollback)
        if resume is None and cwd:
            resume = _search_recover(scrollback, cwd, index_mtimes=ctx.index_mtimes)
        return resume

    provider, process = agent
    instance = ctx.provider(provider)
    if instance is not None:
        try:
            session_id = instance.live_session_id(
                live.live_process(process, raw.processes, live.list_open_files)
            )
        except Exception:
            session_id = None
        if session_id:
            return _resume(provider, session_id, "process")

    resume = _argv_resume(provider, process.argv)
    if resume is not None:
        return resume
    if not cwd:
        return None

    candidates = live.active_candidates(
        provider, cwd, process.start, ctx.sessions(), _normalize_path
    )
    if len(candidates) == 1:
        return _resume(provider, candidates[0].id, "activity")
    # Several sessions were active under this process's lifetime (other tabs
    # in the same directory), or none was (a session reopened in-app and not
    # yet continued): let the scrollback choose, within this provider only.
    return _search_recover(
        scrollback,
        cwd,
        index_mtimes=ctx.index_mtimes,
        provider=provider,
        candidate_ids={s.id for s in candidates} if candidates else None,
    )


# ----- public API ----------------------------------------------------------


def capture() -> Snapshot:
    """Capture the current terminal state, resolving resume metadata."""
    backend = get_backend()
    if backend is None:
        raise SnapshotsUnsupportedError(
            "Terminal.app snapshots are macOS-only — no supported "
            "terminal backend on this platform"
        )

    try:
        captured = backend.capture()
    except TerminalBackendError as exc:
        raise SnapshotsBackendError(f"Could not capture terminal tabs: {exc}") from exc
    ctx = _ResolveContext(_index_mtime_lookup())

    tabs: list[SnapshotTab] = []
    for raw in captured:
        scrollback = _clamp_scrollback(raw.scrollback_tail or "")
        cwd = _normalize_path(raw.cwd)
        resume = _resolve_resume(raw, scrollback, cwd, ctx)
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
    guess: bool = False,
) -> str:
    cwd_str = cwd or "(no cwd)"
    if cmd_args:
        cmd_str = shlex.join(cmd_args)
        mark = "  (guessed session)" if guess else ""
        return f"Window {window}, Tab {tab}  {cwd_str}  [{cmd_str}]{mark}"
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
        guess = tab.resume is not None and tab.resume.is_guess

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
                    guess=guess,
                ),
                reason_skipped=reason,
                guess=guess,
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

    try:
        outcome: RestoreOutcome = backend.restore(runnable)
    except TerminalBackendError as exc:
        raise SnapshotsBackendError(f"Could not reopen terminal tabs: {exc}") from exc

    return RestoreReport(
        snapshot_id=snapshot.id,
        plan=plan,
        launched=outcome.launched,
        fellback=outcome.fellback,
        host_mismatch=host_mismatch,
        note=outcome.note,
    )
