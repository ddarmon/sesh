"""opencode (sst/opencode) session provider.

opencode stores its data under ``~/.local/share/opencode``. Three on-disk
formats exist in the wild:

1. **SQLite v2** (opencode 2.0+): ``opencode.db`` (or
   ``opencode-{channel}.db``) with a ``session_v2`` table and a
   ``session_message`` table. Each ``session_message`` row is one whole
   message (``type`` = ``user`` / ``assistant`` / ``synthetic`` / ``system``
   / ``skill`` / ``shell`` / ``compaction`` / ``idle`` / ``*-switched``)
   ordered by ``seq``; assistant content blocks (text / reasoning / tool,
   with tool output inline) live in its JSON ``data``. There is no ``part``
   table. Child sessions (``parent_id`` set) are sub-agents: they are
   excluded from the session list, counted on their root session (nested
   descendants included), and loaded as sub-agent threads on demand by
   ``load_subagents``.
2. **SQLite v1** (2026, pre-2.0): the same file with ``session``,
   ``message``, and ``part`` tables. ``message.data`` / ``part.data`` are
   JSON columns holding the V1 message/part payloads. Child sessions
   (``session.parent_id``, written by the ``task`` tool) are sub-agents,
   handled exactly as in v2. opencode 2.0
   migrates these rows into the v2 tables in place without dropping them,
   so a DB can hold both; the v2 copy of a session wins.
3. **Legacy JSON storage** (2025-era): individual JSON files under
   ``storage/``::

       storage/session/{projectID}/{sessionID}.json   # session info
       storage/message/{sessionID}/{messageID}.json   # message info
       storage/part/{messageID}/{partID}.json         # content parts

All formats are read; sessions found in SQLite take precedence when
the same session ID appears in both (the SQLite migration imported the
JSON data). While ``/undo`` is staged for possible ``/redo``, opencode
retains the physical tail and records the logical cutoff in
``session.revert`` (message ID plus optional part ID); sesh projects that
cutoff in transcripts, discovery metadata, and search. In v2 the cutoff
hides the target message and everything after it in ``seq`` order (v2
messages have no parts, so ``partID`` is ignored, as opencode's UI does).

The SQLite databases run in WAL mode, so recent writes live in
``opencode.db-wal`` while the main file's mtime/size stay put. DB-backed
sessions are therefore never served from the mtime-keyed metadata cache.

The project path is resolved from the session's ``directory`` field
(both formats), never from encoded folder or project IDs.

Message parts map onto sesh content types:

- ``text``    -> ``"text"`` (``synthetic`` texts are tagged system)
- ``reasoning`` -> ``"thinking"``
- ``tool``    -> one ``"tool_use"`` plus, when the tool state is
  completed/errored, one ``"tool_result"``
- ``step-start`` / ``step-finish`` / ``snapshot`` / ``patch`` /
  ``file`` / ``agent`` / ``retry`` / ``subtask`` / ``compaction``
  are skipped.

v2 messages map the same way (see ``_v2_message_to_messages``): user text
is a visible prompt; ``synthetic`` / ``system`` / ``skill`` text and a
completed ``compaction`` summary are hidden records; a user ``shell``
command is a tool call/result pair; ``idle`` and ``*-switched`` markers
are skipped.

Token conventions follow the repo standard: ``input_tokens`` is the
LAST assistant turn's ``tokens.input + cache.read + cache.write``
(context size); ``output_tokens`` sums ``tokens.output`` across turns;
``cumulative_input_tokens`` sums per-turn input+cache across the
session.
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

from sesh.models import Message, MoveReport, Provider, SessionMeta, SubagentMeta
from sesh.providers import SessionProvider
from sesh.providers.claude import _is_system_message

OPENCODE_DATA_DIR = Path.home() / ".local" / "share" / "opencode"


def _parse_timestamp(ts) -> datetime:
    """Parse an opencode timestamp (epoch millis or ISO string)."""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(ts, str):
        s = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _load_json(path: Path) -> dict | None:
    """Load a single-object JSON file, returning None on any error."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _tokens_from_message(info: dict) -> tuple[int, int] | None:
    """Return (input+cache, output) from an assistant message's tokens."""
    tokens = info.get("tokens")
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") or {}
    if not isinstance(cache, dict):
        cache = {}
    try:
        turn_input = (
            int(tokens.get("input", 0) or 0)
            + int(cache.get("read", 0) or 0)
            + int(cache.get("write", 0) or 0)
        )
        output = int(tokens.get("output", 0) or 0)
    except (TypeError, ValueError):
        return None
    return turn_input, output


def _parse_revert(value: object) -> tuple[str, str | None] | None:
    """Return ``(message_id, part_id)`` from an opencode staged undo.

    SQLite stores ``session.revert`` as JSON text while legacy storage keeps
    the same object directly in the session-info file. Malformed state falls
    back to the physical transcript rather than speculatively hiding content.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    message_id = value.get("messageID")
    if not isinstance(message_id, str) or not message_id:
        return None
    part_id = value.get("partID")
    if part_id is not None and (not isinstance(part_id, str) or not part_id):
        return None
    return message_id, part_id


def _message_is_active(
    message_id: object, revert: tuple[str, str | None] | None
) -> bool:
    """Whether a physical message survives the currently staged undo."""
    if revert is None or not isinstance(message_id, str) or not message_id:
        return True
    target_message, target_part = revert
    return message_id < target_message or (
        message_id == target_message and target_part is not None
    )


def _part_is_active(
    message_id: object,
    part_id: object,
    revert: tuple[str, str | None] | None,
) -> bool:
    """Whether a physical part survives the currently staged undo."""
    if revert is None or not isinstance(message_id, str) or not message_id:
        return True
    target_message, target_part = revert
    if message_id < target_message:
        return True
    if message_id > target_message or target_part is None:
        return False
    # Unknown part ids cannot be ordered safely, so preserve visibility.
    return not isinstance(part_id, str) or not part_id or part_id < target_part


def _part_to_messages(role: str, ts: datetime, part: dict) -> list[Message]:
    """Convert one opencode content part into zero or more Messages."""
    ptype = part.get("type", "")

    if ptype == "text":
        text = part.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return []
        is_sys = role == "user" and (
            bool(part.get("synthetic")) or _is_system_message(text)
        )
        return [Message(
            role=role,
            content=text,
            timestamp=ts,
            is_system=is_sys,
            content_type="text",
        )]

    if ptype == "reasoning":
        text = part.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return []
        return [Message(
            role="assistant",
            content="",
            timestamp=ts,
            thinking=text,
            content_type="thinking",
        )]

    if ptype == "tool":
        name = part.get("tool", "")
        state = part.get("state") or {}
        if not isinstance(state, dict):
            state = {}
        try:
            tool_input = json.dumps(state.get("input", {}), indent=2)
        except TypeError:
            tool_input = str(state.get("input", ""))
        out = [Message(
            role="assistant",
            content="",
            timestamp=ts,
            tool_name=name,
            tool_input=tool_input,
            content_type="tool_use",
        )]
        status = state.get("status", "")
        if status in ("completed", "error"):
            output = state.get("output") if status == "completed" else state.get("error")
            out.append(Message(
                role="tool",
                content="",
                timestamp=ts,
                tool_name=name,
                tool_output=output if isinstance(output, str) else "",
                content_type="tool_result",
            ))
        return out

    # step-start, step-finish, snapshot, patch, file, agent, retry,
    # subtask, compaction: no user-facing content.
    return []


def _model_id(model_json: object) -> str | None:
    """Model ID from a session row's JSON ``model`` column."""
    if not model_json:
        return None
    try:
        model_obj = json.loads(model_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return (model_obj.get("id") or None) if isinstance(model_obj, dict) else None


def db_paths(data_dir: Path) -> list[Path]:
    """opencode SQLite databases, the default ``opencode.db`` first.

    Earlier files win session-ID dedup, so the live default DB must beat a
    stale ``opencode-next.db`` left behind by the 2.0 migration (which would
    otherwise sort first because ``-`` < ``.``).
    """
    if not data_dir.is_dir():
        return []
    return sorted(
        (p for p in data_dir.glob("opencode*.db") if p.is_file()),
        key=lambda p: (p.name != "opencode.db", p.name),
    )


def db_tables(conn: sqlite3.Connection) -> set[str]:
    """Names of the tables in an opencode database."""
    try:
        return {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.Error:
        return set()


def has_v2_schema(tables: set[str]) -> bool:
    return {"session_v2", "session_message"} <= tables


def has_v1_schema(tables: set[str]) -> bool:
    return {"session", "message"} <= tables


def v2_cutoffs(conn: sqlite3.Connection) -> dict[str, int]:
    """Map session ID -> ``seq`` of its staged-undo target message.

    Messages at or after that ``seq`` are hidden. A revert whose target
    message is missing projects nothing, matching opencode's own UI.
    """
    result: dict[str, int] = {}
    try:
        rows = conn.execute(
            "SELECT s.id, s.revert FROM session_v2 s WHERE s.revert IS NOT NULL"
        ).fetchall()
        for session_id, raw in rows:
            revert = _parse_revert(raw)
            if revert is None:
                continue
            row = conn.execute(
                "SELECT seq FROM session_message WHERE session_id = ? AND id = ?",
                (session_id, revert[0]),
            ).fetchone()
            if row is not None and isinstance(row[0], int):
                result[session_id] = row[0]
    except sqlite3.Error:
        pass
    return result


# opencode 1.x titles a task-tool child session "{description} (@{agent} subagent)".
_SUBAGENT_TITLE_SUFFIX = re.compile(r"\s*\(@[^()]*\bsubagent\)\s*$")


def subagent_description(title: object) -> str | None:
    """A child session's title without the 1.x ``(@agent subagent)`` suffix."""
    if not isinstance(title, str):
        return None
    return _SUBAGENT_TITLE_SUFFIX.sub("", title).strip() or None


def child_map(rows) -> dict[str, list[str]]:
    """Map parent session ID -> child IDs from ``(id, parent_id, ...)`` rows."""
    children: dict[str, list[str]] = {}
    for row in rows:
        if row[0] and row[1]:
            children.setdefault(row[1], []).append(row[0])
    return children


def descendants(children: dict[str, list[str]], root_id: str) -> list[str]:
    """All transitive child session IDs of *root_id* (breadth-first).

    A cycle in corrupt data cannot loop forever: each ID is visited once.
    """
    found: list[str] = []
    seen = {root_id}
    queue = [root_id]
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in seen:
                seen.add(child)
                found.append(child)
                queue.append(child)
    return found


def v2_is_active(seq: object, cutoff: int | None) -> bool:
    """Whether a v2 message survives the staged undo ``cutoff``."""
    return cutoff is None or not isinstance(seq, int) or seq < cutoff


def _v2_tool_output(state: dict) -> str:
    """Flatten a v2 tool state's inline result (text content and/or error)."""
    pieces: list[str] = []
    error = state.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            pieces.append(message)
    content = state.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    pieces.append(text)
    return "\n".join(pieces)


def _v2_message_to_messages(mtype: str, info: dict) -> list[Message]:
    """Convert one v2 ``session_message`` row into zero or more Messages."""
    time_obj = info.get("time")
    ts = _parse_timestamp(
        time_obj.get("created") if isinstance(time_obj, dict) else None
    )

    if mtype in ("user", "synthetic", "system", "skill"):
        text = info.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return []
        is_sys = mtype != "user" or _is_system_message(text)
        return [Message(
            role="user", content=text, timestamp=ts,
            is_system=is_sys, content_type="text",
        )]

    if mtype == "compaction":
        summary = info.get("summary", "")
        if info.get("status") != "completed" or not isinstance(summary, str):
            return []
        if not summary.strip():
            return []
        return [Message(
            role="assistant", content=summary, timestamp=ts,
            is_system=True, content_type="text",
        )]

    if mtype == "shell":
        command = info.get("command", "")
        out = [Message(
            role="user", content="", timestamp=ts, tool_name="shell",
            tool_input=command if isinstance(command, str) else "",
            content_type="tool_use",
        )]
        output = info.get("output")
        if isinstance(output, dict) and isinstance(output.get("output"), str):
            out.append(Message(
                role="tool", content="", timestamp=ts, tool_name="shell",
                tool_output=output["output"], content_type="tool_result",
            ))
        return out

    if mtype != "assistant":
        # idle, agent-switched, model-switched, location-switched
        return []

    messages: list[Message] = []
    content = info.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in ("text", "reasoning"):
            # Same shapes as the v1 text/reasoning parts.
            messages.extend(_part_to_messages("assistant", ts, block))
        elif btype == "tool":
            name = block.get("name", "")
            state = block.get("state")
            if not isinstance(state, dict):
                state = {}
            try:
                tool_input = json.dumps(state.get("input", {}), indent=2)
            except TypeError:
                tool_input = str(state.get("input", ""))
            messages.append(Message(
                role="assistant", content="", timestamp=ts,
                tool_name=name, tool_input=tool_input,
                content_type="tool_use",
            ))
            if state.get("status") in ("completed", "error"):
                messages.append(Message(
                    role="tool", content="", timestamp=ts,
                    tool_name=name, tool_output=_v2_tool_output(state),
                    content_type="tool_result",
                ))
    return messages


def _agent_from_data(data: object) -> str | None:
    """The ``agent`` field of a message's JSON ``data``, if any."""
    try:
        info = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    agent = info.get("agent") if isinstance(info, dict) else None
    return agent if isinstance(agent, str) and agent else None


def _atomic_rewrite_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(path))
    except BaseException:
        os.unlink(tmp)
        raise


class OpencodeProvider(SessionProvider):
    """Provider for opencode (sst/opencode) sessions."""

    def __init__(
        self,
        cache=None,
        base_dir: Path | None = None,
        host: str | None = None,
    ) -> None:
        self._index: dict[str, list[SessionMeta]] | None = None
        self._cache = cache
        self._base_dir = base_dir
        self.host = host

    @property
    def _data_dir(self) -> Path:
        if self._base_dir is None:
            return OPENCODE_DATA_DIR
        return self._base_dir / ".local" / "share" / "opencode"

    @property
    def _storage_dir(self) -> Path:
        return self._data_dir / "storage"

    def _db_paths(self) -> list[Path]:
        return db_paths(self._data_dir)

    def diagnostic_issues(self) -> list[str]:
        """Flag databases whose schema no reader recognizes.

        Without this, an unknown layout reads as "no sessions found", which is
        how the opencode 2.0 schema change went unnoticed.
        """
        issues: list[str] = []
        for db_path in self._db_paths():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            except sqlite3.Error as exc:
                issues.append(f"cannot open {db_path}: {exc}")
                continue
            try:
                tables = db_tables(conn)
            finally:
                conn.close()
            if not has_v2_schema(tables) and not has_v1_schema(tables):
                issues.append(
                    f"unrecognized opencode database schema in {db_path} "
                    "(no session_v2/session_message or session/message tables)"
                )
        return issues

    # ------------------------------------------------------------------
    # SessionProvider interface
    # ------------------------------------------------------------------

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each opencode project."""
        index = self._build_index()
        for project_path in sorted(index.keys()):
            if project_path in ("/", ""):
                continue
            display_name = Path(project_path).name or project_path
            yield project_path, display_name

    def get_sessions(self, project_path: str, cache=None) -> list[SessionMeta]:
        """Return sessions for a given project path."""
        index = self._build_index()
        result = list(index.get(project_path, []))
        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages for a session on demand."""
        if not session.source_path:
            return []
        source = Path(session.source_path)
        if source.suffix == ".db":
            return self._get_messages_db(source, session.id)
        return self._get_messages_storage(source, session.id)

    def load_subagents(
        self, session: SessionMeta
    ) -> list[tuple[SubagentMeta, list[Message]]]:
        """Load a DB session's child sessions as sub-agent threads.

        Nested descendants are attached to the root and returned as one flat
        chronological list (as Codex does). Child sessions live in the same
        table as their parent, so the schema that holds the root (v2 first,
        as in ``get_messages``) supplies them. Legacy JSON storage is not
        linked to its children.
        """
        if not session.source_path or not session.source_path.endswith(".db"):
            return []
        db_path = Path(session.source_path)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []

        loaded: list[tuple[SubagentMeta, list[Message]]] = []
        try:
            tables = db_tables(conn)
            if has_v2_schema(tables) and self._in_v2(conn, session.id):
                rows = self._session_rows(conn, "session_v2")
                get_messages = self._get_messages_db_v2
                agent_name = self._v2_agent_name
            elif has_v1_schema(tables):
                rows = self._session_rows(conn, "session")
                get_messages = self._get_messages_db_v1
                agent_name = self._v1_agent_name
            else:
                return []
            info = {row[0]: row for row in rows if row[0]}
            for child_id in descendants(child_map(rows), session.id):
                _sid, _parent, title, t_created, tok_out = info[child_id]
                messages = get_messages(conn, child_id)
                meta = SubagentMeta(
                    agent_id=child_id,
                    file_path=str(db_path),
                    description=subagent_description(title),
                    agent_type=agent_name(conn, child_id),
                    first_timestamp=_parse_timestamp(t_created),
                    message_count=len([
                        m for m in messages
                        if m.role in ("user", "assistant")
                        and m.content_type == "text"
                    ]),
                    output_tokens=int(tok_out) if tok_out else None,
                )
                loaded.append((meta, messages))
        finally:
            conn.close()

        loaded.sort(
            key=lambda pair: pair[0].first_timestamp
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return loaded

    def discover_subagents(self, session: SessionMeta) -> list[SubagentMeta]:
        return [meta for meta, _ in self.load_subagents(session)]

    @staticmethod
    def _in_v2(conn: sqlite3.Connection, session_id: str) -> bool:
        try:
            return conn.execute(
                "SELECT 1 FROM session_v2 WHERE id = ?", (session_id,)
            ).fetchone() is not None
        except sqlite3.Error:
            return False

    @staticmethod
    def _session_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
        """``(id, parent_id, title, time_created, tokens_output)`` rows.

        Empty when the table has no ``parent_id`` column (no sub-agents);
        ``tokens_output`` is NULL when that column is missing.
        """
        for tokens in ("tokens_output", "NULL"):
            try:
                return conn.execute(
                    f"SELECT id, parent_id, title, time_created, {tokens}"
                    f" FROM {table}"
                ).fetchall()
            except sqlite3.Error:
                continue
        return []

    @staticmethod
    def _v2_agent_name(conn: sqlite3.Connection, session_id: str) -> str | None:
        """The agent (e.g. ``general``) that ran a child session.

        Read from the first assistant message rather than ``session_v2.agent``,
        which not every 2.0 schema carries.
        """
        try:
            row = conn.execute(
                "SELECT data FROM session_message"
                " WHERE session_id = ? AND type = 'assistant'"
                " ORDER BY seq LIMIT 1",
                (session_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
        return _agent_from_data(row[0]) if row is not None else None

    @staticmethod
    def _v1_agent_name(conn: sqlite3.Connection, session_id: str) -> str | None:
        """The agent that ran a v1 child session, from its first assistant."""
        try:
            rows = conn.execute(
                "SELECT data FROM message WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
            for (data,) in rows:
                try:
                    info = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(info, dict) and info.get("role") == "assistant":
                    return _agent_from_data(data)
        except sqlite3.Error:
            pass
        return None

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a session from the SQLite DB or the JSON storage tree."""
        if not session.source_path:
            return
        source = Path(session.source_path)
        if source.suffix == ".db":
            self._delete_session_db(source, session.id)
        else:
            self._delete_session_storage(source, session.id)
        self._index = None

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Rewrite session ``directory`` metadata after a project move."""
        files_modified = 0

        # SQLite databases
        for db_path in self._db_paths():
            try:
                conn = sqlite3.connect(str(db_path))
                try:
                    tables = db_tables(conn)
                    # A migrated DB holds v1 and v2 copies; rewrite both so
                    # the v1 fallback never resurfaces the old path.
                    for table in ("session_v2", "session"):
                        if table not in tables:
                            continue
                        cur = conn.execute(
                            f"UPDATE {table} SET directory = ? WHERE directory = ?",
                            (new_path, old_path),
                        )
                        files_modified += cur.rowcount if cur.rowcount > 0 else 0
                    if "project" in tables:
                        conn.execute(
                            "UPDATE project SET worktree = ? WHERE worktree = ?",
                            (new_path, old_path),
                        )
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                return MoveReport(
                    provider=Provider.OPENCODE,
                    success=False,
                    files_modified=files_modified,
                    error=f"Failed updating opencode database: {exc}",
                )

        # Legacy JSON storage
        session_root = self._storage_dir / "session"
        if session_root.is_dir():
            try:
                for info_file in session_root.glob("*/*.json"):
                    info = _load_json(info_file)
                    if not info or info.get("directory") != old_path:
                        continue
                    info["directory"] = new_path
                    _atomic_rewrite_json(info_file, info)
                    files_modified += 1
            except OSError as exc:
                return MoveReport(
                    provider=Provider.OPENCODE,
                    success=False,
                    files_modified=files_modified,
                    error=f"Failed updating opencode session metadata: {exc}",
                )

        self._index = None
        return MoveReport(
            provider=Provider.OPENCODE,
            success=True,
            files_modified=files_modified,
        )

    # ------------------------------------------------------------------
    # Discovery / indexing
    # ------------------------------------------------------------------

    def _build_index(self) -> dict[str, list[SessionMeta]]:
        """Build (and memoize) project_path -> [SessionMeta]."""
        if self._index is not None:
            return self._index

        self._index = {}
        seen_ids: set[str] = set()

        for db_path in self._db_paths():
            for session in self._sessions_from_db(db_path):
                if session.id in seen_ids:
                    continue
                seen_ids.add(session.id)
                self._index.setdefault(session.project_path, []).append(session)

        for session in self._sessions_from_storage():
            if session.id in seen_ids:
                continue
            seen_ids.add(session.id)
            self._index.setdefault(session.project_path, []).append(session)

        return self._index

    def _sessions_from_db(self, db_path: Path) -> list[SessionMeta]:
        """Read all sessions from one opencode SQLite database.

        Not cached: WAL writes leave the main file's mtime/size unchanged, so
        an mtime-keyed cache entry would hide new sessions indefinitely.
        """
        db_str = str(db_path)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []

        try:
            tables = db_tables(conn)
            v2_ids: set[str] = set()
            sessions: list[SessionMeta] = []
            if has_v2_schema(tables):
                sessions.extend(self._sessions_from_db_v2(conn, db_str, v2_ids))
            if has_v1_schema(tables):
                sessions.extend(
                    s for s in self._sessions_from_db_v1(conn, db_str)
                    if s.id not in v2_ids
                )
            return sessions
        finally:
            conn.close()

    def _sessions_from_db_v2(
        self, conn: sqlite3.Connection, db_str: str, seen_ids: set[str],
    ) -> list[SessionMeta]:
        """Read root sessions from the opencode 2.0 ``session_v2`` table.

        Every session ID (children included) is added to *seen_ids* so the
        v1 copies a migration leaves behind are suppressed.
        """
        try:
            rows = conn.execute(
                "SELECT id, parent_id, directory, title, time_created,"
                " time_updated, model, tokens_input, tokens_output,"
                " tokens_cache_read, tokens_cache_write FROM session_v2"
            ).fetchall()
        except sqlite3.Error:
            return []

        cutoffs = v2_cutoffs(conn)
        children = child_map(rows)
        roots = []
        for row in rows:
            sid, parent_id = row[0], row[1]
            if not sid:
                continue
            seen_ids.add(sid)
            if not parent_id:
                roots.append(row)

        sessions: list[SessionMeta] = []
        for (
            sid, _parent, directory, title, t_created, t_updated,
            model_json, tok_in, tok_out, tok_cr, tok_cw,
        ) in roots:
            if not directory:
                continue
            cutoff = cutoffs.get(sid)
            try:
                count_row = conn.execute(
                    "SELECT COUNT(*) FROM session_message"
                    " WHERE session_id = ? AND type IN ('user', 'assistant')"
                    " AND (? IS NULL OR seq < ?)",
                    (sid, cutoff, cutoff),
                ).fetchone()
                last_row = conn.execute(
                    "SELECT data FROM session_message"
                    " WHERE session_id = ? AND type = 'assistant'"
                    " AND (? IS NULL OR seq < ?)"
                    " ORDER BY seq DESC LIMIT 1",
                    (sid, cutoff, cutoff),
                ).fetchone()
            except sqlite3.Error:
                continue

            model = _model_id(model_json)
            last_input = None
            if last_row is not None:
                try:
                    info = json.loads(last_row[0])
                except (json.JSONDecodeError, TypeError):
                    info = None
                if isinstance(info, dict):
                    model_obj = info.get("model")
                    if isinstance(model_obj, dict) and model_obj.get("id"):
                        model = model_obj["id"]
                    tokens = _tokens_from_message(info)
                    if tokens is not None:
                        last_input = tokens[0] or None

            sessions.append(self._db_session_meta(
                sid, directory, title, t_created, t_updated, model,
                count_row[0] if count_row else 0, db_str, last_input,
                tok_in, tok_out, tok_cr, tok_cw,
                # Nested descendants count too: load_subagents returns them
                # flattened under the root.
                subagent_count=len(descendants(children, sid)),
            ))
        return sessions

    def _db_session_meta(
        self, sid, directory, title, t_created, t_updated, model,
        message_count, db_str, last_input, tok_in, tok_out, tok_cr, tok_cw,
        subagent_count: int = 0,
    ) -> SessionMeta:
        """Build a SessionMeta from a ``session``/``session_v2`` row."""
        output_tokens = int(tok_out) if tok_out else None
        cumulative = None
        if any(v for v in (tok_in, tok_cr, tok_cw)):
            cumulative = int(tok_in or 0) + int(tok_cr or 0) + int(tok_cw or 0)
        return SessionMeta(
            id=sid,
            project_path=directory,
            provider=Provider.OPENCODE,
            summary=(title or "").strip() or "OpenCode Session",
            timestamp=_parse_timestamp(t_updated),
            start_timestamp=_parse_timestamp(t_created),
            message_count=message_count,
            model=model,
            source_path=db_str,
            input_tokens=last_input,
            output_tokens=output_tokens,
            cumulative_input_tokens=cumulative,
            host=self.host,
            subagent_count=subagent_count,
        )

    def _sessions_from_db_v1(
        self, conn: sqlite3.Connection, db_str: str,
    ) -> list[SessionMeta]:
        """Read sessions from the pre-2.0 ``session``/``message`` tables."""
        sessions: list[SessionMeta] = []
        try:
            cur = conn.cursor()
            try:
                rows = cur.execute(
                    "SELECT id, directory, title, time_created, time_updated,"
                    " model, tokens_input, tokens_output,"
                    " tokens_cache_read, tokens_cache_write"
                    " FROM session"
                ).fetchall()
            except sqlite3.Error:
                # Older/newer schema without token or model columns
                try:
                    rows = [
                        (*row, None, None, None, None, None)
                        for row in cur.execute(
                            "SELECT id, directory, title,"
                            " time_created, time_updated FROM session"
                        ).fetchall()
                    ]
                except sqlite3.Error:
                    return sessions

            # Child (task-tool sub-agent) sessions are counted on their root,
            # not listed, exactly as in v2.
            children = child_map(self._session_rows(conn, "session"))
            child_ids = {cid for kids in children.values() for cid in kids}

            reverts = self._db_reverts(cur)
            counts: dict[str, int] = {}
            try:
                for row_sid, message_id in cur.execute(
                    "SELECT session_id, id FROM message"
                ).fetchall():
                    if _message_is_active(message_id, reverts.get(row_sid)):
                        counts[row_sid] = counts.get(row_sid, 0) + 1
            except sqlite3.Error:
                pass

            for row in rows:
                (
                    sid, directory, title, t_created, t_updated,
                    model_json, tok_in, tok_out, tok_cr, tok_cw,
                ) = row
                if not sid or not directory or sid in child_ids:
                    continue

                model = _model_id(model_json)
                last_input, last_model = self._last_assistant_tokens_db(
                    cur, sid, reverts.get(sid)
                )
                if last_model is not None:
                    model = last_model

                sessions.append(self._db_session_meta(
                    sid, directory, title, t_created, t_updated, model,
                    counts.get(sid, 0), db_str, last_input,
                    tok_in, tok_out, tok_cr, tok_cw,
                    subagent_count=len(descendants(children, sid)),
                ))
        except sqlite3.Error:
            pass
        return sessions

    @staticmethod
    def _db_reverts(
        cur: sqlite3.Cursor,
    ) -> dict[str, tuple[str, str | None]]:
        """Load valid staged-undo cutoffs when the DB schema supports them."""
        result: dict[str, tuple[str, str | None]] = {}
        try:
            rows = cur.connection.execute(
                "SELECT id, revert FROM session WHERE revert IS NOT NULL"
            )
        except sqlite3.Error:
            return result
        for session_id, value in rows:
            revert = _parse_revert(value)
            if isinstance(session_id, str) and revert is not None:
                result[session_id] = revert
        return result

    @staticmethod
    def _last_assistant_tokens_db(
        cur: sqlite3.Cursor,
        session_id: str,
        revert: tuple[str, str | None] | None = None,
    ) -> tuple[int | None, str | None]:
        """Return active last-turn input+cache tokens and model ID."""
        try:
            rows = cur.connection.execute(
                "SELECT id, data FROM message"
                " WHERE session_id = ? ORDER BY id DESC",
                (session_id,),
            )
        except sqlite3.Error:
            return None, None
        for message_id, data in rows:
            if not _message_is_active(message_id, revert):
                continue
            try:
                info = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            tokens = _tokens_from_message(info)
            model = info.get("modelID") or None
            if tokens is not None:
                return tokens[0] or None, model
            return None, model
        return None, None

    def _sessions_from_storage(self) -> list[SessionMeta]:
        """Read all sessions from the legacy JSON storage tree."""
        session_root = self._storage_dir / "session"
        if not session_root.is_dir():
            return []

        cache = self._cache
        sessions: list[SessionMeta] = []

        for project_dir in sorted(session_root.iterdir()):
            if not project_dir.is_dir():
                continue
            for info_file in sorted(project_dir.glob("*.json")):
                file_str = str(info_file)
                if cache:
                    cached = cache.get_sessions(file_str)
                    if cached:
                        sessions.extend(cached)
                        continue
                session = self._parse_storage_session(info_file)
                if session is None:
                    continue
                sessions.append(session)
                if cache:
                    cache.put_sessions(file_str, [session])

        return sessions

    def _parse_storage_session(self, info_file: Path) -> SessionMeta | None:
        info = _load_json(info_file)
        if not info:
            return None

        session_id = info.get("id") or info_file.stem
        directory = info.get("directory")
        if not directory:
            path_obj = info.get("path")
            if isinstance(path_obj, dict):
                directory = path_obj.get("cwd") or path_obj.get("root")
        if not directory:
            return None

        time_obj = info.get("time") or {}
        if not isinstance(time_obj, dict):
            time_obj = {}

        storage = info_file.parent.parent.parent  # .../storage
        msg_dir = storage / "message" / session_id
        revert = _parse_revert(info.get("revert"))

        message_count = 0
        model: str | None = None
        last_input: int | None = None
        output_total = 0
        cumulative = 0
        saw_tokens = False

        if msg_dir.is_dir():
            for msg_file in sorted(msg_dir.glob("*.json")):
                minfo = _load_json(msg_file)
                if not minfo:
                    continue
                message_id = minfo.get("id") or msg_file.stem
                is_active = _message_is_active(message_id, revert)
                role = minfo.get("role")
                if is_active and role in ("user", "assistant"):
                    message_count += 1
                if role == "assistant":
                    if is_active:
                        model = minfo.get("modelID") or model
                    tokens = _tokens_from_message(minfo)
                    if tokens is not None and any(tokens):
                        saw_tokens = True
                        if is_active:
                            last_input = tokens[0]
                        # Cumulative usage describes incurred work, including
                        # turns hidden by a staged undo.
                        cumulative += tokens[0]
                        output_total += tokens[1]

        return SessionMeta(
            id=session_id,
            project_path=directory,
            provider=Provider.OPENCODE,
            summary=(info.get("title") or "").strip() or "OpenCode Session",
            timestamp=_parse_timestamp(time_obj.get("updated")),
            start_timestamp=_parse_timestamp(time_obj.get("created")),
            message_count=message_count,
            model=model,
            source_path=str(info_file),
            input_tokens=last_input if saw_tokens else None,
            output_tokens=output_total if saw_tokens else None,
            cumulative_input_tokens=cumulative if saw_tokens else None,
            host=self.host,
        )

    # ------------------------------------------------------------------
    # Message loading
    # ------------------------------------------------------------------

    def _get_messages_db(self, db_path: Path, session_id: str) -> list[Message]:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []

        try:
            tables = db_tables(conn)
            if has_v2_schema(tables) and self._in_v2(conn, session_id):
                return self._get_messages_db_v2(conn, session_id)
            if has_v1_schema(tables):
                return self._get_messages_db_v1(conn, session_id)
            return []
        finally:
            conn.close()

    @staticmethod
    def _get_messages_db_v2(
        conn: sqlite3.Connection, session_id: str,
    ) -> list[Message]:
        messages: list[Message] = []
        cutoff = v2_cutoffs(conn).get(session_id)
        try:
            rows = conn.execute(
                "SELECT seq, type, data FROM session_message"
                " WHERE session_id = ? ORDER BY seq",
                (session_id,),
            )
            for seq, mtype, data in rows:
                if not v2_is_active(seq, cutoff):
                    break
                try:
                    info = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(info, dict):
                    messages.extend(_v2_message_to_messages(mtype, info))
        except sqlite3.Error:
            pass
        return messages

    def _get_messages_db_v1(
        self, conn: sqlite3.Connection, session_id: str,
    ) -> list[Message]:
        messages: list[Message] = []
        try:
            revert = self._db_reverts(conn.cursor()).get(session_id)
            rows = conn.execute(
                "SELECT m.id, m.data, p.id, p.data"
                " FROM message m LEFT JOIN part p ON p.message_id = m.id"
                " WHERE m.session_id = ?"
                " ORDER BY m.id, p.id",
                (session_id,),
            )
            for message_id, mdata, part_id, pdata in rows:
                if not _part_is_active(message_id, part_id, revert):
                    continue
                try:
                    minfo = json.loads(mdata)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(minfo, dict) or pdata is None:
                    continue
                try:
                    part = json.loads(pdata)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(part, dict):
                    continue
                role = minfo.get("role", "")
                time_obj = minfo.get("time") or {}
                ts = _parse_timestamp(
                    time_obj.get("created") if isinstance(time_obj, dict) else None
                )
                messages.extend(_part_to_messages(role, ts, part))
        except sqlite3.Error:
            pass
        return messages

    def _get_messages_storage(self, info_file: Path, session_id: str) -> list[Message]:
        storage = info_file.parent.parent.parent
        msg_dir = storage / "message" / session_id
        if not msg_dir.is_dir():
            return []

        info = _load_json(info_file)
        revert = _parse_revert(info.get("revert")) if info else None
        messages: list[Message] = []
        for msg_file in sorted(msg_dir.glob("*.json")):
            minfo = _load_json(msg_file)
            if not minfo:
                continue
            role = minfo.get("role", "")
            time_obj = minfo.get("time") or {}
            ts = _parse_timestamp(
                time_obj.get("created") if isinstance(time_obj, dict) else None
            )
            message_id = minfo.get("id") or msg_file.stem
            for part_file in self._part_files(storage, session_id, message_id):
                part = _load_json(part_file)
                if not part:
                    continue
                part_id = part.get("id") or part_file.stem
                if not _part_is_active(message_id, part_id, revert):
                    continue
                messages.extend(_part_to_messages(role, ts, part))

        return messages

    @staticmethod
    def _part_files(storage: Path, session_id: str, message_id: str) -> list[Path]:
        """Part files for a message (flat layout, then older nested layout)."""
        flat = storage / "part" / message_id
        if flat.is_dir():
            return sorted(flat.glob("*.json"))
        nested = storage / "part" / session_id / message_id
        if nested.is_dir():
            return sorted(nested.glob("*.json"))
        return []

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    @staticmethod
    def _delete_session_db(db_path: Path, session_id: str) -> None:
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error:
            return
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            tables = db_tables(conn)
            if has_v2_schema(tables):
                # Child (sub-agent) sessions are hidden under their parent, so
                # they go with it rather than surfacing as orphans.
                targets = [session_id]
                for target in targets:
                    targets.extend(
                        row[0] for row in conn.execute(
                            "SELECT id FROM session_v2 WHERE parent_id = ?",
                            (target,),
                        )
                    )
                for target in targets:
                    conn.execute(
                        "DELETE FROM session_message WHERE session_id = ?",
                        (target,),
                    )
                    conn.execute("DELETE FROM session_v2 WHERE id = ?", (target,))
            # A migrated DB keeps the v1 copy too; leaving it would resurface
            # the session through the v1 fallback. v1 children (task-tool
            # sub-agents) go with their parent, as in v2.
            v1_targets = [session_id]
            if "session" in tables:
                v1_targets += descendants(
                    child_map(OpencodeProvider._session_rows(conn, "session")),
                    session_id,
                )
            for target in v1_targets:
                for table, column in (
                    ("part", "session_id"), ("message", "session_id"),
                    ("session", "id"),
                ):
                    if table in tables:
                        conn.execute(
                            f"DELETE FROM {table} WHERE {column} = ?", (target,)
                        )
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    @staticmethod
    def _delete_session_storage(info_file: Path, session_id: str) -> None:
        storage = info_file.parent.parent.parent
        msg_dir = storage / "message" / session_id

        # Remove parts for each message (flat layout keys parts by message id)
        if msg_dir.is_dir():
            for msg_file in msg_dir.glob("*.json"):
                minfo = _load_json(msg_file)
                message_id = (minfo or {}).get("id") or msg_file.stem
                shutil.rmtree(storage / "part" / message_id, ignore_errors=True)
        # Older nested layout keys parts by session id
        shutil.rmtree(storage / "part" / session_id, ignore_errors=True)
        shutil.rmtree(msg_dir, ignore_errors=True)

        diff_file = storage / "session_diff" / f"{session_id}.json"
        try:
            diff_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            info_file.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = ["OPENCODE_DATA_DIR", "OpencodeProvider"]
