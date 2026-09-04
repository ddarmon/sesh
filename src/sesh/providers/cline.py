"""Cline (VS Code extension) session provider — ``next`` bundle only.

Cline 4.1.17 ships two bundles behind a staged rollout. This provider reads
**only** the ``next`` (SDK) bundle's store under ``~/.cline/data/``::

    ~/.cline/data/
      db/sessions.db                    # SQLite index (redundant with the JSON)
      sessions/{sessionId}/
        {sessionId}.json                # session record
        {sessionId}.messages.json       # transcript
      workspaces/chat/                  # scratch workspace for chat-mode sessions

The ``legacy`` bundle's store (VS Code ``globalStorage/…/tasks/``) is an
explicit non-goal: the two formats share nothing, and legacy discards
reasoning traces before they reach disk, so those transcripts are lossy in a
way no amount of parsing repairs.

Session ids look like ``{msEpoch}_{suffix}`` (e.g. ``1788435477952_eibi9``).

Discovery never reads ``db/sessions.db``: it is redundant with the per-session
JSON and lives in WAL mode with a live writer (VS Code holds it open), so
discovery stays plain ``stat`` + ``json.load`` like the Gemini provider. The
database is touched only by :meth:`ClineProvider.delete_session` and
:meth:`ClineProvider.move_project`, which must keep Cline's own index in sync.

Cline has no resume-by-id CLI, so sessions are not resumable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, MoveReport, Provider, SessionMeta
from sesh.providers import SessionProvider

CLINE_DIR = Path.home() / ".cline"
CLINE_DATA_DIR = CLINE_DIR / "data"

# Both files carry ``version: 1``; the format is young, so skip anything else
# rather than parsing optimistically.
SUPPORTED_VERSION = 1

# ``{msEpoch}_{suffix}`` — also the traversal-safe gate for every id-derived path.
_SESSION_ID_RE = re.compile(r"^[0-9]+_[A-Za-z0-9]+$")

_USER_INPUT_RE = re.compile(
    r"^\s*<user_input(?:\s[^>]*)?>(.*)</user_input>\s*$", re.DOTALL
)

_TASK_RESUMPTION_PREFIX = "[TASK RESUMPTION]"

# The chat-mode scratch workspace is not a real project; it is grouped under
# its own pseudo-project so those sessions stay browsable.
_CHAT_WORKSPACE_NAME = "cline:chat"
_CHAT_WORKSPACE_SUFFIX = ("data", "workspaces", "chat")


def is_chat_workspace(project_path: str) -> bool:
    """True for Cline's chat-mode scratch workspace.

    Matched on the trailing ``…/data/workspaces/chat`` segments rather than
    against this machine's own data dir: in aggregation mode the recorded
    ``workspace_root`` is the *source* host's absolute path, which never equals
    the aggregator's ``_chat_workspace``.
    """
    parts = Path(project_path).parts
    return parts[-3:] == _CHAT_WORKSPACE_SUFFIX


def resolve_data_dir() -> Path:
    """Resolve Cline's data dir the way the ``next`` bundle does.

    ``CLINE_DATA_DIR`` wins if set and non-empty, else ``${CLINE_DIR}/data``,
    else ``~/.cline/data``.
    """
    data_dir = os.environ.get("CLINE_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir)
    cline_dir = os.environ.get("CLINE_DIR", "").strip()
    if cline_dir:
        return Path(cline_dir) / "data"
    return CLINE_DATA_DIR


def is_valid_session_id(session_id) -> bool:
    """True when *session_id* is safe to interpolate into a filesystem path."""
    return isinstance(session_id, str) and bool(_SESSION_ID_RE.match(session_id))


def _parse_timestamp(value) -> datetime | None:
    """Parse a Cline timestamp (ISO-8601 string with Z, or epoch ms)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _load_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _version_ok(data: dict) -> bool:
    version = data.get("version")
    return version is None or version == SUPPORTED_VERSION


def _int_or_zero(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2)
    except (TypeError, ValueError):
        return str(value)


def strip_user_input_wrapper(text: str) -> str:
    """Unwrap ``<user_input mode="act">…</user_input>`` around a user turn."""
    match = _USER_INPUT_RE.match(text)
    return match.group(1).strip() if match else text


def _is_task_resumption(text: str) -> bool:
    return text.lstrip().startswith(_TASK_RESUMPTION_PREFIX)


def _blocks_to_messages(role: str, blocks, ts: datetime | None) -> list[Message]:
    """Convert one stored message's content blocks into sesh Messages.

    Block vocabulary is taken from the ``next`` bundle's codec, not guessed
    from samples: ``text``, ``thinking``, ``redacted_thinking``, ``tool_use``,
    ``tool_result``, ``image``, ``file``, ``media``.
    """
    if isinstance(blocks, str):
        blocks = [{"type": "text", "text": blocks}]
    if not isinstance(blocks, list):
        return []

    out: list[Message] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            text = block.get("text")
            if not isinstance(text, str):
                continue
            is_system = False
            if role == "user":
                text = strip_user_input_wrapper(text)
                is_system = _is_task_resumption(text)
            if not text.strip():
                continue
            out.append(Message(
                role=role,
                content=text,
                timestamp=ts,
                is_system=is_system,
                content_type="text",
            ))

        elif btype == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str) or not thinking.strip():
                continue
            out.append(Message(
                role="assistant",
                content="",
                timestamp=ts,
                thinking=thinking,
                content_type="thinking",
            ))

        elif btype == "redacted_thinking":
            out.append(Message(
                role="assistant",
                content="",
                timestamp=ts,
                thinking="[redacted thinking]",
                content_type="thinking",
            ))

        elif btype == "tool_use":
            out.append(Message(
                role="assistant",
                content="",
                timestamp=ts,
                tool_name=block.get("name") or "",
                tool_input=_stringify(block.get("input", {})),
                content_type="tool_use",
            ))

        elif btype == "tool_result":
            # ``content`` is arbitrary JSON, not necessarily a string
            # (``run_commands`` stores a list of {query, result, success}).
            output = _stringify(block.get("content") or "")
            if block.get("is_error"):
                output = f"[error] {output}"
            out.append(Message(
                role="tool",
                content="",
                timestamp=ts,
                tool_name=block.get("name") or "",
                tool_output=output,
                content_type="tool_result",
            ))

        elif btype == "image":
            media_type = block.get("mediaType") or "image"
            out.append(Message(
                role=role,
                content=f"[image: {media_type}]",
                timestamp=ts,
                content_type="text",
            ))

        elif btype == "media":
            out.append(Message(
                role=role,
                content="[media]",
                timestamp=ts,
                content_type="text",
            ))

        elif btype == "file":
            path = block.get("path") or ""
            content = block.get("content")
            body = content if isinstance(content, str) else ""
            header = f"[file: {path}]" if path else "[file]"
            out.append(Message(
                role=role,
                content=f"{header}\n{body}".rstrip(),
                timestamp=ts,
                content_type="text",
            ))

    return out


class ClineProvider(SessionProvider):
    """Provider for Cline ``next``-bundle sessions."""

    def __init__(
        self,
        cache=None,
        base_dir: Path | None = None,
        host: str | None = None,
    ) -> None:
        self._cache = cache
        self._base_dir = base_dir
        self.host = host
        # session_id -> record; built lazily, dropped after a delete or move
        self._records: dict[str, dict] | None = None

    @property
    def _data_dir(self) -> Path:
        # In aggregation mode the env overrides describe *this* machine, not
        # the mirrored host, so they are deliberately not consulted.
        if self._base_dir is None:
            return resolve_data_dir()
        return self._base_dir / ".cline" / "data"

    @property
    def _sessions_dir(self) -> Path:
        # Named for the base class's diagnostic_paths() probe: ``sesh doctor``
        # then reports this root separately, so a data dir written by the
        # legacy bundle (which has no sessions/) reads as present-but-empty
        # rather than as a silent zero-session scan.
        return self._data_dir / "sessions"

    @property
    def _db_path(self) -> Path:
        return self._data_dir / "db" / "sessions.db"

    # ------------------------------------------------------------------
    # SessionProvider interface
    # ------------------------------------------------------------------

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each Cline workspace."""
        seen: set[str] = set()
        for record in self._session_records().values():
            if record.get("is_subagent"):
                continue
            project_path = self._project_path(record)
            if not project_path or project_path in seen:
                continue
            seen.add(project_path)
            yield project_path, self._display_name(project_path)

    def get_sessions(self, project_path: str, cache=None) -> list[SessionMeta]:
        """Return sessions for one Cline workspace (one session per dir)."""
        active_cache = cache if cache is not None else self._cache
        records = self._session_records()

        subagent_counts: dict[str, int] = {}
        for record in records.values():
            parent = record.get("parent_session_id")
            if record.get("is_subagent") and isinstance(parent, str) and parent:
                subagent_counts[parent] = subagent_counts.get(parent, 0) + 1

        result: list[SessionMeta] = []
        for session_id, record in records.items():
            if record.get("is_subagent"):
                continue
            if self._project_path(record) != project_path:
                continue
            session = self._build_session(
                session_id, record, project_path,
                subagent_count=subagent_counts.get(session_id, 0),
                cache=active_cache,
            )
            if session is not None:
                result.append(session)

        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages from a Cline ``{id}.messages.json`` document."""
        if not session.source_path:
            return []
        data = _load_json(Path(session.source_path))
        if not data or not _version_ok(data):
            return []

        messages: list[Message] = []
        for entry in data.get("messages", []):
            if not isinstance(entry, dict):
                continue
            role = entry.get("role")
            if not isinstance(role, str) or not role:
                continue
            ts = _parse_timestamp(entry.get("ts"))
            messages.extend(_blocks_to_messages(role, entry.get("content"), ts))
        return messages

    def delete_session(self, session: SessionMeta) -> None:
        """Remove the session directory, its sub-agent directories, and its rows.

        Cline's store keys child sessions by ``parent_session_id``, so deleting a
        parent has to take the children's directories with it — otherwise their
        records keep being parsed on every scan and keep inflating the (now
        absent) parent's ``subagent_count``.
        """
        if not is_valid_session_id(session.id):
            raise ValueError(f"Refusing to delete unsafe session id: {session.id}")

        # Collect child directories before the scan cache is dropped.
        child_ids = [
            child_id
            for child_id, record in self._session_records().items()
            if record.get("is_subagent")
            and record.get("parent_session_id") == session.id
        ]
        self._records = None

        # rmtree is deliberately allowed to raise: a removal that fails
        # (permissions, an open handle) must abort before the index is edited,
        # rather than leaving files sesh re-lists and Cline no longer knows about.
        for target_id in (session.id, *child_ids):
            target = self._sessions_dir / target_id
            if target.is_dir():
                shutil.rmtree(target)

        db_path = self._db_path
        if not db_path.is_file():
            return
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Removed session files but could not open Cline's index: {exc}"
            ) from exc
        try:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session.id,))
            conn.execute(
                "DELETE FROM sessions WHERE parent_session_id = ?", (session.id,)
            )
            conn.commit()
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Removed session files but could not update Cline's index: {exc}"
            ) from exc
        finally:
            conn.close()

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Rewrite ``workspace_root`` / ``cwd`` in the JSON records and the DB.

        The old path also appears inside ``system_prompt`` and inside tool
        output; those are left alone, as the Claude provider already tolerates
        embedded stale paths.
        """
        files_modified = 0
        sessions_root = self._sessions_dir
        if sessions_root.is_dir():
            try:
                for session_dir in sorted(sessions_root.iterdir()):
                    if not session_dir.is_dir() or not is_valid_session_id(session_dir.name):
                        continue
                    record_file = session_dir / f"{session_dir.name}.json"
                    record = _load_json(record_file)
                    if not record:
                        continue
                    changed = False
                    for field in ("workspace_root", "cwd"):
                        value = record.get(field)
                        if isinstance(value, str) and _path_matches(value, old_path):
                            record[field] = _rewrite_path(value, old_path, new_path)
                            changed = True
                    if not changed:
                        continue
                    _atomic_rewrite_json(record_file, record)
                    files_modified += 1
            except OSError as exc:
                self._records = None
                return MoveReport(
                    provider=Provider.CLINE,
                    success=False,
                    files_modified=files_modified,
                    error=f"Failed updating Cline session records: {exc}",
                )

        db_path = self._db_path
        if db_path.is_file():
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
                try:
                    # Match in Python with the same helper the JSON pass uses.
                    # SQL LIKE is case-insensitive for ASCII, so a LIKE prefix
                    # pass would rewrite rows the JSON pass correctly skipped
                    # (e.g. moving "/Users/me/repo" would hit a record stored as
                    # "/Users/me/Repo") and report zero files changed while
                    # corrupting Cline's index. Rewriting each row once also
                    # keeps a nested move (/a -> /a/b) from re-matching its own
                    # output.
                    rows = conn.execute(
                        "SELECT session_id, workspace_root, cwd FROM sessions"
                    ).fetchall()
                    for session_id, workspace_root, cwd in rows:
                        updated = []
                        for value in (workspace_root, cwd):
                            if isinstance(value, str) and _path_matches(value, old_path):
                                updated.append(_rewrite_path(value, old_path, new_path))
                            else:
                                updated.append(value)
                        if updated != [workspace_root, cwd]:
                            conn.execute(
                                "UPDATE sessions SET workspace_root = ?, cwd = ?"
                                " WHERE session_id = ?",
                                (*updated, session_id),
                            )
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._records = None
                return MoveReport(
                    provider=Provider.CLINE,
                    success=False,
                    files_modified=files_modified,
                    error=f"Failed updating Cline's session index: {exc}",
                )

        self._records = None
        return MoveReport(
            provider=Provider.CLINE,
            success=True,
            files_modified=files_modified,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _session_records(self) -> dict[str, dict]:
        """Scan ``sessions/*/{id}.json`` and return {session_id: record}."""
        if self._records is not None:
            return self._records

        sessions_root = self._sessions_dir
        records: dict[str, dict] = {}
        if not sessions_root.is_dir():
            self._records = records
            return records

        try:
            entries = sorted(sessions_root.iterdir())
        except OSError:
            entries = []

        for session_dir in entries:
            name = session_dir.name
            if not is_valid_session_id(name) or not session_dir.is_dir():
                continue
            record = _load_json(session_dir / f"{name}.json")
            if not record or not _version_ok(record):
                continue
            # Never trust the recorded absolute messages_path: in aggregation
            # mode it points at the source host's filesystem.
            record["_messages_path"] = str(session_dir / f"{name}.messages.json")
            # Identity is the (traversal-safe) directory name, not the record's
            # own session_id, so discovery, search, and delete address the same
            # session even if a record's internal id disagrees with its folder.
            records[name] = record

        self._records = records
        return records

    def _project_path(self, record: dict) -> str:
        for field in ("workspace_root", "cwd"):
            value = record.get(field)
            if isinstance(value, str) and value:
                return value
        return ""

    def _display_name(self, project_path: str) -> str:
        if is_chat_workspace(project_path):
            return _CHAT_WORKSPACE_NAME
        return Path(project_path).name or project_path

    def _build_session(
        self,
        session_id: str,
        record: dict,
        project_path: str,
        *,
        subagent_count: int,
        cache,
    ) -> SessionMeta | None:
        messages_path = record.get("_messages_path") or ""
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        # The cache is keyed on the transcript, so it may only stand in for
        # fields the transcript produces. Everything else comes from the record,
        # which is re-read on every scan anyway — Cline rewrites the record alone
        # when it backfills a generated title, and a cached summary would
        # otherwise never catch up.
        counts = None
        if cache:
            cached = cache.get_sessions(messages_path)
            if cached:
                counts = {
                    "message_count": cached[0].message_count,
                    "input_tokens": cached[0].input_tokens,
                    "updated_at": cached[0].timestamp,
                }

        parsed_transcript = counts is None
        if counts is None:
            counts = self._messages_summary(messages_path)

        started = _parse_timestamp(record.get("started_at"))
        updated = (
            _parse_timestamp(record.get("updated_at"))
            or _parse_timestamp(record.get("ended_at"))
        )

        if updated is None:
            updated = counts["updated_at"] or started
        if updated is None:
            updated = datetime.now(tz=timezone.utc)

        usage = metadata.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        cumulative_input = (
            _int_or_zero(usage.get("inputTokens"))
            + _int_or_zero(usage.get("cacheReadTokens"))
            + _int_or_zero(usage.get("cacheWriteTokens"))
        )
        output_tokens = _int_or_zero(usage.get("outputTokens"))

        summary = metadata.get("title") or record.get("prompt") or "Cline Session"
        if not isinstance(summary, str):
            summary = "Cline Session"
        summary = summary.strip() or "Cline Session"

        model = record.get("model")

        session = SessionMeta(
            id=session_id,
            project_path=project_path,
            provider=Provider.CLINE,
            summary=summary,
            timestamp=updated,
            start_timestamp=started,
            message_count=counts["message_count"],
            model=model if isinstance(model, str) else None,
            source_path=messages_path,
            input_tokens=counts["input_tokens"],
            output_tokens=output_tokens or None,
            cumulative_input_tokens=cumulative_input or None,
            host=self.host,
            subagent_count=subagent_count,
        )
        if cache and parsed_transcript:
            cache.put_sessions(messages_path, [session])
        return session

    @staticmethod
    def _messages_summary(messages_path: str) -> dict:
        """Count messages and read the last turn's context size.

        ``metadata.tokensIn`` / ``tokensOut`` skip tool-call turns (verified),
        so the last-turn context has to come from the transcript's own
        per-message ``metrics``.
        """
        empty = {"message_count": 0, "input_tokens": None, "updated_at": None}
        if not messages_path:
            return empty
        data = _load_json(Path(messages_path))
        if not data or not _version_ok(data):
            return empty

        entries = data.get("messages")
        if not isinstance(entries, list):
            entries = []

        message_count = 0
        input_tokens: int | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            message_count += 1
            if entry.get("role") != "assistant":
                continue
            metrics = entry.get("metrics")
            if isinstance(metrics, dict) and metrics:
                input_tokens = (
                    _int_or_zero(metrics.get("inputTokens"))
                    + _int_or_zero(metrics.get("cacheReadTokens"))
                    + _int_or_zero(metrics.get("cacheWriteTokens"))
                )

        return {
            "message_count": message_count,
            "input_tokens": input_tokens or None,
            "updated_at": _parse_timestamp(data.get("updated_at")),
        }


def _path_matches(value: str, old_path: str) -> bool:
    return value == old_path or value.startswith(old_path.rstrip("/") + "/")


def _rewrite_path(value: str, old_path: str, new_path: str) -> str:
    if value == old_path:
        return new_path
    return new_path.rstrip("/") + value[len(old_path.rstrip("/")):]


def _atomic_rewrite_json(path: Path, data: dict) -> None:
    scratch = {k: v for k, v in data.items() if not k.startswith("_")}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(scratch, f, indent=2)
        os.replace(tmp, str(path))
    except BaseException:
        os.unlink(tmp)
        raise


__all__ = [
    "CLINE_DIR",
    "CLINE_DATA_DIR",
    "ClineProvider",
    "is_chat_workspace",
    "is_valid_session_id",
    "resolve_data_dir",
    "strip_user_input_wrapper",
]
