"""Antigravity CLI (``agy``) session provider.

Verified against Antigravity CLI 1.5.12. Its store lives under
``~/.gemini/antigravity-cli/``::

    brain/{conversationId}/.system_generated/
      logs/transcript_full.jsonl        # one JSON record per step (the source)
      logs/transcript.jsonl             # same, long tool output truncated
      subagents/{childId}.json          # one sidecar per spawned sub-agent
    conversations/{conversationId}.db   # SQLite + protobuf blobs (agy's state)
    conversation_summaries.db           # SQLite index: title, workspace, parent
    jetbox_summaries_proto.pb           # protobuf copy of the summaries
    cache/last_conversations.json       # {workspace path: conversation id}
    cache/conversation_metadata.json    # stale-prone summary cache
    history.jsonl                       # prompt history (``conversationId``)

Conversation ids are UUIDs. A sub-agent is an ordinary conversation whose
index row names its ``parent_conversation_id`` (and which the parent lists in
``subagents/``); it is never listed on its own.

Reading uses only the JSONL transcript plus the summaries index. The protobuf
state is touched only by :meth:`AgyProvider.delete_session` and
:meth:`AgyProvider.move_project`, which have to keep agy's own view in sync:
agy regenerates its index row from ``trajectory_metadata_blob`` inside the
conversation database, so a move that edited only the index would silently
revert on the next resume (verified).

There is no schema for those blobs, so they are edited without one: the wire
format is walked generically and only a length-delimited field whose whole
value is a ``file://`` URI under the old project path is replaced. Every other
byte is copied through unchanged.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from sesh.models import (
    LiveProcess,
    Message,
    MoveReport,
    Provider,
    SessionMeta,
    SubagentMeta,
)
from sesh.prompt_history import HistoryReport, _rewrite_guarded, remove_jsonl_entries
from sesh.providers import SessionProvider

AGY_DATA_DIR = Path.home() / ".gemini" / "antigravity-cli"

# Conversation ids are UUIDs; this is also the traversal-safe gate for every
# id-derived filesystem path.
_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

_TRANSCRIPT_NAMES = ("transcript_full.jsonl", "transcript.jsonl")

# Sessions whose workspace cannot be recovered are grouped under a
# pseudo-project so they stay browsable.
UNKNOWN_PROJECT_NAME = "agy:unknown"

# Characters agy leaves unescaped in a workspace URI path (measured against a
# directory named with every printable ASCII punctuation character).
_URI_SAFE = "/@+,&=;:$"

_USER_REQUEST_OPEN = "<USER_REQUEST>"
_USER_REQUEST_CLOSE = "</USER_REQUEST>"

_AGENT_MESSAGE_RE = re.compile(
    r"<SYSTEM_MESSAGE>\s*\[Message\]\s+timestamp=\S+\s+sender=(\S+)"
    r"\s+priority=\S+\s+content=(.*?)\s*</SYSTEM_MESSAGE>\s*$",
    re.DOTALL,
)

_INDEX_COLUMNS = (
    "conversation_id",
    "title",
    "preview",
    "workspace_uris",
    "parent_conversation_id",
    "agent_name",
)


def is_valid_session_id(session_id) -> bool:
    """True when *session_id* is safe to interpolate into a filesystem path."""
    return isinstance(session_id, str) and bool(_ID_RE.fullmatch(session_id))


# ----------------------------------------------------------------------
# Workspace URIs
# ----------------------------------------------------------------------


def path_to_uri(path: str) -> str:
    """Encode an absolute path the way agy writes workspace URIs."""
    return "file://" + quote(path, safe=_URI_SAFE)


def uri_to_path(uri) -> str:
    """Decode a ``file://`` workspace URI; "" for anything else."""
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return ""
    return unquote(uri[len("file://"):])


def _path_matches(value: str, old_path: str) -> bool:
    return value == old_path or value.startswith(old_path.rstrip("/") + "/")


def _rewrite_path(value: str, old_path: str, new_path: str) -> str:
    if value == old_path:
        return new_path
    return new_path.rstrip("/") + value[len(old_path.rstrip("/")):]


def rewrite_uri(uri: str, old_path: str, new_path: str) -> str | None:
    """Return *uri* re-rooted from *old_path* to *new_path*, or None.

    Matching is done on the decoded path (case-sensitively, on a path
    boundary), so it does not depend on how the URI happened to be escaped.
    """
    path = uri_to_path(uri)
    if not path or not _path_matches(path, old_path):
        return None
    return path_to_uri(_rewrite_path(path, old_path, new_path))


# ----------------------------------------------------------------------
# Schema-less protobuf wire format
# ----------------------------------------------------------------------


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError("truncated or oversized varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, pos


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def pb_fields(buf: bytes) -> list[tuple[int, int, int, int, bytes | int]]:
    """Split *buf* into ``(field, wire_type, start, end, value)`` tuples.

    ``start``/``end`` delimit the whole field (tag included). Raises
    ``ValueError`` when *buf* is not a well-formed message, which is how a
    plain string or binary payload is told apart from a nested message.
    """
    out: list[tuple[int, int, int, int, bytes | int]] = []
    pos = 0
    while pos < len(buf):
        start = pos
        tag, pos = _read_varint(buf, pos)
        field, wire_type = tag >> 3, tag & 7
        if field == 0:
            raise ValueError("field number 0")
        value: bytes | int
        if wire_type == 0:
            value, pos = _read_varint(buf, pos)
        elif wire_type == 1:
            value = buf[pos:pos + 8]
            pos += 8
        elif wire_type == 5:
            value = buf[pos:pos + 4]
            pos += 4
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            value = buf[pos:pos + length]
            pos += length
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        if pos > len(buf):
            raise ValueError("field overruns buffer")
        out.append((field, wire_type, start, pos, value))
    return out


def pb_rewrite(
    buf: bytes, replace: Callable[[bytes], bytes | None]
) -> tuple[bytes, int]:
    """Replace length-delimited values anywhere in a nested message.

    ``replace`` sees each length-delimited value and returns its replacement
    or None. A value it declines is tried as a nested message. Only a field
    that changed (or contains one) is re-encoded, so untouched bytes — and any
    non-canonical encoding within them — survive verbatim. Returns
    ``(new_buf, replacements)``; a buffer that does not parse is returned
    unchanged.
    """
    try:
        fields = pb_fields(buf)
    except ValueError:
        return buf, 0

    parts: list[bytes] = []
    copied_to = 0
    total = 0
    for field, wire_type, start, end, value in fields:
        if wire_type != 2 or not isinstance(value, bytes):
            continue
        new_value = replace(value)
        if new_value is not None:
            count = 1
        elif value:
            new_value, count = pb_rewrite(value, replace)
        else:
            count = 0
        if not count or new_value is None:
            continue
        total += count
        parts.append(buf[copied_to:start])
        parts.append(_encode_varint((field << 3) | 2))
        parts.append(_encode_varint(len(new_value)))
        parts.append(new_value)
        copied_to = end

    if not total:
        return buf, 0
    parts.append(buf[copied_to:])
    return b"".join(parts), total


def _uri_replacer(old_path: str, new_path: str) -> Callable[[bytes], bytes | None]:
    def replace(value: bytes) -> bytes | None:
        if not value.startswith(b"file://"):
            return None
        try:
            uri = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
        rewritten = rewrite_uri(uri, old_path, new_path)
        return rewritten.encode("utf-8") if rewritten is not None else None

    return replace


def _blob_workspace(blob) -> str:
    """Read the workspace URI from a ``trajectory_metadata_blob`` payload."""
    if not isinstance(blob, bytes):
        return ""
    try:
        for field, wire_type, _s, _e, value in pb_fields(blob):
            if field != 1 or wire_type != 2 or not isinstance(value, bytes):
                continue
            for inner, inner_type, _is, _ie, inner_value in pb_fields(value):
                if inner == 1 and inner_type == 2 and isinstance(inner_value, bytes):
                    return uri_to_path(inner_value.decode("utf-8", "replace"))
    except ValueError:
        return ""
    return ""


# ----------------------------------------------------------------------
# Small I/O helpers
# ----------------------------------------------------------------------


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite file without ever writing beside it.

    agy's databases are in WAL mode. While agy runs, ``mode=ro`` attaches to
    its ``-shm``; once agy has closed cleanly the ``-wal``/``-shm`` pair is
    gone and a read-only open fails outright because it may not create one
    (``SQLITE_CANTOPEN``, verified). ``immutable=1`` is the fallback for that
    case: it skips WAL handling, which is exactly right when there is no WAL,
    and merely yields the last checkpoint for a mirrored copy.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return conn
    except sqlite3.Error:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=2
        )
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return conn


def _parse_timestamp(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _int_or_zero(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace *path* with *data*, keeping its permission bits."""
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _dump_json(data) -> bytes:
    # Two-space indentation without ASCII escaping, as agy writes these files.
    return json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")


# ----------------------------------------------------------------------
# Transcript parsing
# ----------------------------------------------------------------------


def split_user_input(content: str) -> tuple[str, str]:
    """Split a ``USER_INPUT`` record into (prompt, injected remainder).

    agy wraps what the user typed in ``<USER_REQUEST>`` and appends its own
    ``<ADDITIONAL_METADATA>`` / ``<USER_SETTINGS_CHANGE>`` blocks. The LAST
    closing tag is used so a prompt that itself quotes the tag stays whole.
    """
    stripped = content.lstrip()
    if not stripped.startswith(_USER_REQUEST_OPEN):
        return content.strip(), ""
    end = stripped.rfind(_USER_REQUEST_CLOSE)
    if end == -1:
        return content.strip(), ""
    prompt = stripped[len(_USER_REQUEST_OPEN):end].strip("\n")
    rest = stripped[end + len(_USER_REQUEST_CLOSE):].strip()
    return prompt, rest


def _iter_records(path: Path) -> Iterator[dict]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def read_transcript(path: Path) -> tuple[list[dict], dict]:
    """Return (active records, totals over every record) for a transcript.

    The file is append-only. ``/rewind`` does not remove the abandoned steps;
    the conversation simply continues from an earlier ``step_index``, so a
    record whose index is not past the previous one supersedes everything
    from that index on (verified with a real rewind). Token totals keep
    counting the abandoned steps because that work was actually incurred.
    """
    active: list[tuple[int, dict]] = []
    output_tokens = 0
    cumulative_input = 0
    for record in _iter_records(path):
        if record.get("type") == "PLANNER_RESPONSE":
            output_tokens += _int_or_zero(record.get("output_tokens"))
            cumulative_input += (
                _int_or_zero(record.get("input_tokens"))
                + _int_or_zero(record.get("cache_read_tokens"))
            )
        index = record.get("step_index")
        if isinstance(index, bool) or not isinstance(index, int):
            index = active[-1][0] if active else 0
        else:
            while active and active[-1][0] >= index:
                active.pop()
        active.append((index, record))
    totals = {
        "output_tokens": output_tokens,
        "cumulative_input_tokens": cumulative_input,
    }
    return [record for _index, record in active], totals


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def records_to_messages(
    records: list[dict], parent_id: str | None = None
) -> list[Message]:
    """Convert active transcript records into sesh Messages.

    Record vocabulary (``type``): ``USER_INPUT``, ``PLANNER_RESPONSE`` (text,
    ``thinking``, ``tool_calls``), ``GENERIC`` (a tool result) and
    ``SYSTEM_MESSAGE``. A tool result carries neither a tool name nor a call
    id, so it is paired with the pending call in order — results are written
    in call order (verified with parallel calls). Any other type is kept as a
    hidden record rather than dropped.

    *parent_id* marks a sub-agent transcript: the parent's message is that
    thread's prompt, so it renders as the user turn instead of a hidden
    record.
    """
    out: list[Message] = []
    pending_tools: list[str] = []
    for record in records:
        rtype = record.get("type")
        ts = _parse_timestamp(record.get("created_at"))
        content = record.get("content")
        text = content if isinstance(content, str) else ""

        if rtype == "USER_INPUT":
            pending_tools = []
            prompt, rest = split_user_input(text)
            if prompt.strip():
                out.append(Message(role="user", content=prompt, timestamp=ts))
            if rest:
                out.append(Message(
                    role="user", content=rest, timestamp=ts, is_system=True,
                ))

        elif rtype == "PLANNER_RESPONSE":
            pending_tools = []
            thinking = record.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                out.append(Message(
                    role="assistant", content="", timestamp=ts,
                    thinking=thinking.strip(), content_type="thinking",
                ))
            if text.strip():
                out.append(Message(role="assistant", content=text, timestamp=ts))
            calls = record.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict):
                    continue
                name = call.get("name")
                name = name if isinstance(name, str) else ""
                pending_tools.append(name)
                out.append(Message(
                    role="assistant", content="", timestamp=ts,
                    tool_name=name,
                    tool_input=_stringify(call.get("args", {})),
                    content_type="tool_use",
                ))

        elif rtype == "GENERIC":
            name = pending_tools.pop(0) if pending_tools else ""
            out.append(Message(
                role="tool", content="", timestamp=ts,
                tool_name=name, tool_output=text, content_type="tool_result",
            ))

        elif rtype == "SYSTEM_MESSAGE":
            match = _AGENT_MESSAGE_RE.search(text)
            if match and parent_id and match.group(1) == parent_id:
                out.append(Message(
                    role="user", content=match.group(2), timestamp=ts,
                ))
            elif text.strip():
                out.append(Message(
                    role="system", content=text, timestamp=ts, is_system=True,
                ))

        elif text.strip():
            out.append(Message(
                role="system", content=text, timestamp=ts, is_system=True,
            ))

    return out


def _dialogue_count(messages: list[Message]) -> int:
    return len([
        m for m in messages
        if m.content_type == "text" and not m.is_system
        and m.role in ("user", "assistant")
    ])


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------


class AgyProvider(SessionProvider):
    """Provider for Antigravity CLI (``agy``) conversations."""

    def __init__(
        self,
        cache=None,
        base_dir: Path | None = None,
        host: str | None = None,
    ) -> None:
        self._cache = cache
        self._base_dir = base_dir
        self.host = host
        # Built lazily, dropped after a delete or move.
        self._scan: dict | None = None
        self._index_error: str | None = None

    @property
    def _data_dir(self) -> Path:
        if self._base_dir is None:
            return AGY_DATA_DIR
        return self._base_dir / ".gemini" / "antigravity-cli"

    @property
    def _brain_dir(self) -> Path:
        return self._data_dir / "brain"

    @property
    def _conversations_dir(self) -> Path:
        return self._data_dir / "conversations"

    @property
    def _index_path(self) -> Path:
        return self._data_dir / "conversation_summaries.db"

    @property
    def _history_path(self) -> Path:
        return self._data_dir / "history.jsonl"

    def diagnostic_paths(self) -> list[tuple[str, Path]]:
        return [("data", self._data_dir), ("brain", self._brain_dir)]

    def diagnostic_issues(self) -> list[str]:
        self._scanned()
        return [self._index_error] if self._index_error else []

    # ------------------------------------------------------------------
    # SessionProvider interface
    # ------------------------------------------------------------------

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each agy workspace."""
        scan = self._scanned()
        seen: set[str] = set()
        for conv_id in scan["transcripts"]:
            if conv_id in scan["parents"]:
                continue
            project_path = self._project_path(conv_id)
            if project_path in seen:
                continue
            seen.add(project_path)
            yield project_path, self._display_name(project_path)

    def get_sessions(self, project_path: str, cache=None) -> list[SessionMeta]:
        """Return the root conversations recorded for one workspace."""
        active_cache = cache if cache is not None else self._cache
        scan = self._scanned()
        result: list[SessionMeta] = []
        for conv_id, transcript in scan["transcripts"].items():
            if conv_id in scan["parents"]:
                continue
            if self._project_path(conv_id) != project_path:
                continue
            result.append(
                self._build_session(conv_id, transcript, project_path, active_cache)
            )
        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load the active transcript of a conversation."""
        transcript = self._transcript_for(session)
        if transcript is None:
            return []
        records, _totals = read_transcript(transcript)
        return records_to_messages(records)

    def load_subagents(
        self, session: SessionMeta
    ) -> list[tuple[SubagentMeta, list[Message]]]:
        """Load the conversation's sub-agent threads (nested ones included).

        Descendants are attached to the root and returned as one flat list
        ordered by first activity, the timestamp used for splicing.
        """
        if not is_valid_session_id(session.id):
            return []
        scan = self._scanned()
        loaded: list[tuple[SubagentMeta, list[Message]]] = []
        for child_id in self._descendants(session.id):
            transcript = scan["transcripts"].get(child_id)
            if transcript is None:
                continue
            parent_id = scan["parents"].get(child_id)
            records, totals = read_transcript(transcript)
            messages = records_to_messages(records, parent_id=parent_id)
            sidecar = self._sidecar(parent_id, child_id)
            descriptor = sidecar.get("subagentDescriptor")
            if not isinstance(descriptor, dict):
                descriptor = {}
            row = scan["index"].get(child_id, {})
            agent_type = descriptor.get("typeName") or row.get("agent_name")
            role = descriptor.get("role")
            first_ts = next(
                (m.timestamp for m in messages if m.timestamp is not None), None
            )
            loaded.append((
                SubagentMeta(
                    agent_id=child_id,
                    file_path=str(transcript),
                    description=role if isinstance(role, str) and role else None,
                    agent_type=(
                        agent_type if isinstance(agent_type, str) and agent_type
                        else None
                    ),
                    first_timestamp=first_ts,
                    message_count=_dialogue_count(messages),
                    output_tokens=totals["output_tokens"] or None,
                ),
                messages,
            ))
        loaded.sort(key=lambda item: (
            item[0].first_timestamp is None,
            item[0].first_timestamp or datetime.min.replace(tzinfo=timezone.utc),
        ))
        return loaded

    def locate(self, conv_id: str) -> tuple[str, str, str | None] | None:
        """Resolve a conversation id to (root id, project path, agent id).

        Used by search: a hit inside a sub-agent transcript is attributed to
        the root conversation, with the sub-agent's id as ``agent id``.
        """
        scan = self._scanned()
        if conv_id not in scan["transcripts"]:
            return None
        root = self._root_of(conv_id)
        if root not in scan["transcripts"]:
            return None
        return root, self._project_path(root), conv_id if root != conv_id else None

    def transcript_path(self, conv_id: str) -> str | None:
        """Path of the transcript sesh reads for *conv_id*, if any."""
        path = self._scanned()["transcripts"].get(conv_id)
        return str(path) if path is not None else None

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def live_session_id(self, process: LiveProcess) -> str | None:
        """Find the conversation a running ``agy`` process holds open.

        agy keeps ``presence/{id}.lock`` open for the conversation it is
        in. The files themselves are empty and are left behind when agy
        exits, so only a lock in the process's open files counts. A process
        running sub-agents holds their locks too; those map to the same
        root conversation.
        """
        presence = str(self._data_dir / "presence") + os.sep
        roots: set[str] = set()
        for path in process.open_files:
            if not path.startswith(presence) or not path.endswith(".lock"):
                continue
            conv_id = path[len(presence):-len(".lock")]
            if is_valid_session_id(conv_id):
                roots.add(self._root_of(conv_id))
        return roots.pop() if len(roots) == 1 else None

    def delete_session(self, session: SessionMeta) -> None:
        """Remove a conversation and its sub-agents the way agy's delete does.

        agy's own delete (F4 in ``/resume``) removes the ``brain`` directory,
        the conversation database, the index row with its search entries, and
        the entry in ``jetbox_summaries_proto.pb`` (verified by diffing the
        store around a real delete). This also clears the per-conversation
        files agy leaves behind — the ``presence`` lock, the ``annotations``
        file, and the entries in ``cache/`` — so nothing keeps pointing at a
        conversation that no longer exists.
        """
        if not is_valid_session_id(session.id):
            raise ValueError(f"Refusing to delete unsafe session id: {session.id}")

        targets = [session.id, *self._descendants(session.id)]
        self._scan = None

        # Removal is deliberately allowed to raise: a brain directory that
        # cannot be removed must abort before agy's index is edited, rather
        # than leaving a transcript sesh re-lists and agy no longer knows.
        for target in targets:
            brain = self._brain_dir / target
            if brain.is_dir():
                shutil.rmtree(brain)
        for target in targets:
            for suffix in (".db", ".db-wal", ".db-shm"):
                (self._conversations_dir / f"{target}{suffix}").unlink(missing_ok=True)
            (self._data_dir / "presence" / f"{target}.lock").unlink(missing_ok=True)
            (self._data_dir / "annotations" / f"{target}.pbtxt").unlink(missing_ok=True)

        self._delete_index_rows(targets)
        self._drop_jetbox_entries(set(targets))
        self._drop_cache_entries(set(targets))

    def _delete_index_rows(self, targets: list[str]) -> None:
        db_path = self._index_path
        if not db_path.is_file():
            return
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Removed conversation files but could not open agy's index: {exc}"
            ) from exc
        try:
            for target in targets:
                conn.execute(
                    "DELETE FROM conversation_summaries WHERE conversation_id = ?",
                    (target,),
                )
                try:
                    conn.execute(
                        "DELETE FROM conversations_fts WHERE conversation_id = ?",
                        (target,),
                    )
                except sqlite3.OperationalError as exc:
                    # No search table yet (or an SQLite build without FTS5):
                    # there is nothing sesh could remove from it. A lock or
                    # I/O failure is not this case and still propagates.
                    message = str(exc).lower()
                    if "no such table" not in message and "no such module" not in message:
                        raise
            conn.commit()
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Removed conversation files but could not update agy's index: {exc}"
            ) from exc
        finally:
            conn.close()

    def _drop_jetbox_entries(self, targets: set[str]) -> None:
        """Drop the targets' entries from ``jetbox_summaries_proto.pb``.

        The file is a sequence of top-level entries, each a message whose
        field 1 is the conversation id. Entries are removed by byte range;
        nothing else is re-encoded.
        """
        path = self._data_dir / "jetbox_summaries_proto.pb"
        try:
            data = path.read_bytes()
        except OSError:
            return
        try:
            entries = pb_fields(data)
        except ValueError:
            return
        keep: list[bytes] = []
        dropped = False
        for _field, wire_type, start, end, value in entries:
            if wire_type == 2 and isinstance(value, bytes) and self._entry_id(value) in targets:
                dropped = True
                continue
            keep.append(data[start:end])
        if dropped:
            _atomic_write(path, b"".join(keep))

    @staticmethod
    def _entry_id(entry: bytes) -> str | None:
        try:
            for field, wire_type, _s, _e, value in pb_fields(entry):
                if field == 1 and wire_type == 2 and isinstance(value, bytes):
                    return value.decode("utf-8", "replace")
        except ValueError:
            return None
        return None

    def _drop_cache_entries(self, targets: set[str]) -> None:
        last_path = self._data_dir / "cache" / "last_conversations.json"
        last = _load_json(last_path)
        if isinstance(last, dict):
            kept = {k: v for k, v in last.items() if v not in targets}
            if len(kept) != len(last):
                _atomic_write(last_path, _dump_json(kept))

        meta_path = self._data_dir / "cache" / "conversation_metadata.json"
        meta = _load_json(meta_path)
        if isinstance(meta, dict) and isinstance(meta.get("conversations"), dict):
            conversations = meta["conversations"]
            kept = {k: v for k, v in conversations.items() if k not in targets}
            if len(kept) != len(conversations):
                meta["conversations"] = kept
                _atomic_write(meta_path, _dump_json(meta))

    def delete_history(
        self, session: SessionMeta, dry_run: bool = False
    ) -> HistoryReport:
        """Remove the conversation's prompts from agy's ``history.jsonl``.

        Entries carry ``conversationId`` once the conversation exists; the
        prompt that *started* a conversation (and any ``-p`` run) is written
        without one and cannot be attributed, so it is kept. Only the root id
        is matched: sub-agents are prompted by the parent agent, not the user.
        """
        path = self._history_path
        removed = remove_jsonl_entries(
            path,
            lambda entry: entry.get("conversationId") == session.id,
            dry_run=dry_run,
        )
        report = HistoryReport()
        if removed:
            report.removed[str(path)] = removed
        return report

    # ------------------------------------------------------------------
    # Move
    # ------------------------------------------------------------------

    def move_candidates(self, old_path: str) -> int:
        """Count conversations recorded under *old_path* (dry-run preview)."""
        scan = self._scanned()
        ids = set(scan["index"]) | set(scan["transcripts"])
        return len([
            conv_id for conv_id in ids
            if _path_matches(self._own_workspace(conv_id), old_path)
        ])

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Re-root every conversation recorded under *old_path*.

        Only the conversation's *workspace* is rewritten: the protobuf
        metadata blob agy rebuilds its index from, the index row, the
        ``jetbox`` copy, sub-agent sidecars, the ``cache/`` files and the
        ``workspace`` of prompt-history entries. Paths inside recorded tool
        calls, tool output and system prompts are left alone, as the Claude
        and Cline providers already tolerate embedded stale paths.
        ``settings.json``'s ``trustedWorkspaces`` is deliberately untouched:
        trusting the new location is the user's call, and agy asks.
        """
        files_modified = 0
        replace = _uri_replacer(old_path, new_path)
        self._scan = None
        try:
            files_modified += self._move_conversation_dbs(replace)
            files_modified += self._move_index(old_path, new_path, replace)
            files_modified += self._move_jetbox(replace)
            files_modified += self._move_sidecars(old_path, new_path)
            files_modified += self._move_caches(old_path, new_path)
            files_modified += self._move_history(old_path, new_path)
        except (OSError, sqlite3.Error) as exc:
            return MoveReport(
                provider=Provider.AGY,
                success=False,
                files_modified=files_modified,
                error=f"Failed updating agy metadata: {exc}",
            )
        return MoveReport(
            provider=Provider.AGY, success=True, files_modified=files_modified,
        )

    def _move_conversation_dbs(self, replace) -> int:
        conversations = self._conversations_dir
        if not conversations.is_dir():
            return 0
        modified = 0
        for db_path in sorted(conversations.glob("*.db")):
            if not is_valid_session_id(db_path.stem):
                continue
            conn = sqlite3.connect(str(db_path), timeout=5)
            try:
                try:
                    rows = conn.execute(
                        "SELECT id, data FROM trajectory_metadata_blob"
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue  # not a conversation database sesh understands
                changed = False
                for row_id, blob in rows:
                    if not isinstance(blob, bytes):
                        continue
                    new_blob, count = pb_rewrite(blob, replace)
                    if count:
                        conn.execute(
                            "UPDATE trajectory_metadata_blob SET data = ? WHERE id = ?",
                            (new_blob, row_id),
                        )
                        changed = True
                if changed:
                    conn.commit()
                    modified += 1
            finally:
                conn.close()
        return modified

    def _move_index(self, old_path: str, new_path: str, replace) -> int:
        db_path = self._index_path
        if not db_path.is_file():
            return 0
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            # Matched in Python, not with SQL LIKE: LIKE is case-insensitive
            # for ASCII and would re-root a workspace that only differs in
            # case from the one being moved.
            rows = conn.execute(
                "SELECT conversation_id, workspace_uris, raw_summary"
                " FROM conversation_summaries"
            ).fetchall()
            changed = 0
            for conv_id, uris_text, raw in rows:
                new_text = uris_text
                try:
                    uris = json.loads(uris_text) if isinstance(uris_text, str) else None
                except json.JSONDecodeError:
                    uris = None
                if isinstance(uris, list):
                    rewritten = [
                        (rewrite_uri(u, old_path, new_path) or u)
                        if isinstance(u, str) else u
                        for u in uris
                    ]
                    if rewritten != uris:
                        new_text = json.dumps(rewritten, separators=(",", ":"))
                new_raw, count = (
                    pb_rewrite(raw, replace) if isinstance(raw, bytes) else (raw, 0)
                )
                if new_text == uris_text and not count:
                    continue
                conn.execute(
                    "UPDATE conversation_summaries SET workspace_uris = ?,"
                    " raw_summary = ? WHERE conversation_id = ?",
                    (new_text, new_raw, conv_id),
                )
                changed += 1
            if changed:
                conn.commit()
            return 1 if changed else 0
        finally:
            conn.close()

    def _move_jetbox(self, replace) -> int:
        path = self._data_dir / "jetbox_summaries_proto.pb"
        try:
            data = path.read_bytes()
        except OSError:
            return 0
        new_data, count = pb_rewrite(data, replace)
        if not count:
            return 0
        _atomic_write(path, new_data)
        return 1

    def _move_sidecars(self, old_path: str, new_path: str) -> int:
        brain = self._brain_dir
        if not brain.is_dir():
            return 0
        modified = 0
        for sidecar in sorted(brain.glob("*/.system_generated/subagents/*.json")):
            data = _load_json(sidecar)
            if not isinstance(data, dict):
                continue
            uris = data.get("workspaceUris")
            if not isinstance(uris, list):
                continue
            rewritten = [
                (rewrite_uri(u, old_path, new_path) or u) if isinstance(u, str) else u
                for u in uris
            ]
            if rewritten == uris:
                continue
            data["workspaceUris"] = rewritten
            _atomic_write(
                sidecar, json.dumps(data, ensure_ascii=False).encode("utf-8")
            )
            modified += 1
        return modified

    def _move_caches(self, old_path: str, new_path: str) -> int:
        modified = 0

        last_path = self._data_dir / "cache" / "last_conversations.json"
        last = _load_json(last_path)
        if isinstance(last, dict):
            rekeyed: dict = {}
            changed = False
            for key, value in last.items():
                if isinstance(key, str) and _path_matches(key, old_path):
                    key = _rewrite_path(key, old_path, new_path)
                    changed = True
                    # A conversation already started at the new location is
                    # newer than the one being moved in; keep it as "last".
                    if key in last:
                        continue
                rekeyed[key] = value
            if changed:
                _atomic_write(last_path, _dump_json(rekeyed))
                modified += 1

        meta_path = self._data_dir / "cache" / "conversation_metadata.json"
        meta = _load_json(meta_path)
        if isinstance(meta, dict) and isinstance(meta.get("conversations"), dict):
            changed = False
            for entry in meta["conversations"].values():
                summary = entry.get("summary") if isinstance(entry, dict) else None
                uris = summary.get("WorkspaceURIs") if isinstance(summary, dict) else None
                if not isinstance(uris, list):
                    continue
                rewritten = [
                    (rewrite_uri(u, old_path, new_path) or u)
                    if isinstance(u, str) else u
                    for u in uris
                ]
                if rewritten != uris:
                    summary["WorkspaceURIs"] = rewritten
                    changed = True
            if changed:
                _atomic_write(meta_path, _dump_json(meta))
                modified += 1

        return modified

    def _move_history(self, old_path: str, new_path: str) -> int:
        """Re-root ``workspace`` in prompt-history entries.

        agy scopes prompt recall to the workspace, and every running agy
        appends to this file, so it goes through the guarded rewrite.
        """

        def write_rewritten(src, dst) -> int:
            changed = 0
            for line in src:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    entry = None
                workspace = entry.get("workspace") if isinstance(entry, dict) else None
                if isinstance(workspace, str) and _path_matches(workspace, old_path):
                    entry["workspace"] = _rewrite_path(workspace, old_path, new_path)
                    ending = "\n" if line.endswith("\n") else ""
                    dst.write(
                        json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                        + ending
                    )
                    changed += 1
                    continue
                dst.write(line)
            return changed

        return 1 if _rewrite_guarded(self._history_path, write_rewritten, False) else 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scanned(self) -> dict:
        """Scan the brain directory, the index, and the sub-agent sidecars."""
        if self._scan is not None:
            return self._scan

        transcripts: dict[str, Path] = {}
        sidecar_parents: dict[str, str] = {}
        brain = self._brain_dir
        try:
            entries = sorted(brain.iterdir()) if brain.is_dir() else []
        except OSError:
            entries = []
        for entry in entries:
            if not is_valid_session_id(entry.name) or not entry.is_dir():
                continue
            generated = entry / ".system_generated"
            for name in _TRANSCRIPT_NAMES:
                candidate = generated / "logs" / name
                if candidate.is_file():
                    transcripts[entry.name] = candidate
                    break
            subagents = generated / "subagents"
            if subagents.is_dir():
                try:
                    children = sorted(subagents.glob("*.json"))
                except OSError:
                    children = []
                for child in children:
                    if is_valid_session_id(child.stem):
                        sidecar_parents[child.stem] = entry.name

        index = self._read_index()
        parents = dict(sidecar_parents)
        for conv_id, row in index.items():
            parent = row.get("parent_conversation_id")
            if is_valid_session_id(parent) and parent != conv_id:
                parents[conv_id] = parent

        children_of: dict[str, list[str]] = {}
        for child, parent in parents.items():
            children_of.setdefault(parent, []).append(child)

        self._scan = {
            "transcripts": transcripts,
            "index": index,
            "parents": parents,
            "children": children_of,
            "workspaces": {},
        }
        return self._scan

    def _read_index(self) -> dict[str, dict]:
        self._index_error = None
        db_path = self._index_path
        if not db_path.is_file():
            return {}
        try:
            conn = _open_readonly(db_path)
        except sqlite3.Error as exc:
            self._index_error = f"agy index {db_path} could not be opened: {exc}"
            return {}
        try:
            rows = conn.execute(
                f"SELECT {', '.join(_INDEX_COLUMNS)} FROM conversation_summaries"
            ).fetchall()
        except sqlite3.Error as exc:
            self._index_error = (
                f"agy index {db_path} has an unrecognized schema: {exc}"
            )
            return {}
        finally:
            conn.close()

        index: dict[str, dict] = {}
        for row in rows:
            record = dict(zip(_INDEX_COLUMNS, row))
            conv_id = record.get("conversation_id")
            if is_valid_session_id(conv_id):
                index[conv_id] = record
        return index

    def _own_workspace(self, conv_id: str) -> str:
        """The workspace recorded for *conv_id* itself ("" if unknown).

        The index row is authoritative; a conversation agy has not indexed
        (or whose index is unreadable) falls back to the metadata blob in its
        own database.
        """
        scan = self._scanned()
        cached = scan["workspaces"].get(conv_id)
        if cached is not None:
            return cached

        workspace = ""
        row = scan["index"].get(conv_id)
        if row:
            try:
                uris = json.loads(row.get("workspace_uris") or "[]")
            except (TypeError, json.JSONDecodeError):
                uris = []
            for uri in uris if isinstance(uris, list) else []:
                workspace = uri_to_path(uri)
                if workspace:
                    break
        if not workspace:
            db_path = self._conversations_dir / f"{conv_id}.db"
            if db_path.is_file():
                try:
                    conn = _open_readonly(db_path)
                    try:
                        found = conn.execute(
                            "SELECT data FROM trajectory_metadata_blob LIMIT 1"
                        ).fetchone()
                    finally:
                        conn.close()
                    if found:
                        workspace = _blob_workspace(found[0])
                except sqlite3.Error:
                    workspace = ""

        scan["workspaces"][conv_id] = workspace
        return workspace

    def _project_path(self, conv_id: str) -> str:
        return self._own_workspace(self._root_of(conv_id)) or str(self._brain_dir)

    def _display_name(self, project_path: str) -> str:
        if project_path == str(self._brain_dir):
            return UNKNOWN_PROJECT_NAME
        return Path(project_path).name or project_path

    def _root_of(self, conv_id: str) -> str:
        parents = self._scanned()["parents"]
        seen = {conv_id}
        while conv_id in parents:
            conv_id = parents[conv_id]
            if conv_id in seen:
                break
            seen.add(conv_id)
        return conv_id

    def _descendants(self, conv_id: str) -> list[str]:
        children_of = self._scanned()["children"]
        out: list[str] = []
        seen = {conv_id}
        stack = [conv_id]
        while stack:
            for child in sorted(children_of.get(stack.pop(), [])):
                if child not in seen:
                    seen.add(child)
                    out.append(child)
                    stack.append(child)
        return out

    def _sidecar(self, parent_id: str | None, child_id: str) -> dict:
        if not is_valid_session_id(parent_id) or not is_valid_session_id(child_id):
            return {}
        data = _load_json(
            self._brain_dir / parent_id / ".system_generated" / "subagents"
            / f"{child_id}.json"
        )
        return data if isinstance(data, dict) else {}

    def _transcript_for(self, session: SessionMeta) -> Path | None:
        if session.source_path:
            path = Path(session.source_path)
            if path.is_file():
                return path
        if not is_valid_session_id(session.id):
            return None
        return self._scanned()["transcripts"].get(session.id)

    def _build_session(
        self, conv_id: str, transcript: Path, project_path: str, cache
    ) -> SessionMeta:
        source = str(transcript)
        session: SessionMeta | None = None
        if cache:
            cached = cache.get_sessions(source)
            if cached:
                session = cached[0]
        if session is None:
            session = self._parse_session(conv_id, transcript)
            if cache:
                cache.put_sessions(source, [session])

        # The cache is keyed on the transcript, so it may only stand in for
        # what the transcript produces. Title, workspace and sub-agent links
        # come from the index and sidecars, which change on their own (agy
        # backfills a generated title; a move re-roots the workspace).
        row = self._scanned()["index"].get(conv_id, {})
        for candidate in (row.get("title"), row.get("preview")):
            if isinstance(candidate, str) and candidate.strip():
                session.summary = candidate.strip()
                break
        session.id = conv_id
        session.project_path = project_path
        session.host = self.host
        session.subagent_count = len(self._descendants(conv_id))
        return session

    def _parse_session(self, conv_id: str, transcript: Path) -> SessionMeta:
        records, totals = read_transcript(transcript)
        messages = records_to_messages(records)

        model = None
        input_tokens = None
        for record in records:
            if record.get("type") != "PLANNER_RESPONSE":
                continue
            if isinstance(record.get("model"), str) and record["model"]:
                model = record["model"]
            context = (
                _int_or_zero(record.get("input_tokens"))
                + _int_or_zero(record.get("cache_read_tokens"))
            )
            if context:
                input_tokens = context

        timestamps = [m.timestamp for m in messages if m.timestamp is not None]
        started = timestamps[0] if timestamps else None
        updated = timestamps[-1] if timestamps else None
        if updated is None:
            try:
                updated = datetime.fromtimestamp(
                    transcript.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                updated = datetime.now(tz=timezone.utc)

        prompts = [
            m for m in messages
            if m.role == "user" and m.content_type == "text" and not m.is_system
        ]
        replies = [
            m for m in messages
            if m.role == "assistant" and m.content_type in ("text", "tool_use")
        ]
        summary = _first_line(prompts[0].content) if prompts else ""

        return SessionMeta(
            id=conv_id,
            project_path="",
            provider=Provider.AGY,
            summary=summary or "Antigravity Session",
            timestamp=updated,
            start_timestamp=started,
            message_count=_dialogue_count(messages),
            model=model,
            source_path=str(transcript),
            input_tokens=input_tokens,
            output_tokens=totals["output_tokens"] or None,
            cumulative_input_tokens=totals["cumulative_input_tokens"] or None,
            host=self.host,
            is_empty=not prompts and not replies,
        )


__all__ = [
    "AGY_DATA_DIR",
    "AgyProvider",
    "UNKNOWN_PROJECT_NAME",
    "is_valid_session_id",
    "path_to_uri",
    "pb_fields",
    "pb_rewrite",
    "read_transcript",
    "records_to_messages",
    "rewrite_uri",
    "split_user_input",
    "uri_to_path",
]
