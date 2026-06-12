"""opencode (sst/opencode) session provider.

opencode stores its data under ``~/.local/share/opencode``. Two on-disk
formats exist in the wild:

1. **SQLite** (current, 2026+): a single ``opencode.db`` (or
   ``opencode-{channel}.db``) database with ``session``, ``message``,
   and ``part`` tables. ``message.data`` / ``part.data`` are JSON
   columns holding the V1 message/part payloads.
2. **Legacy JSON storage** (2025-era): individual JSON files under
   ``storage/``::

       storage/session/{projectID}/{sessionID}.json   # session info
       storage/message/{sessionID}/{messageID}.json   # message info
       storage/part/{messageID}/{partID}.json         # content parts

Both formats are read; sessions found in SQLite take precedence when
the same session ID appears in both (the SQLite migration imported the
JSON data).

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

Token conventions follow the repo standard: ``input_tokens`` is the
LAST assistant turn's ``tokens.input + cache.read + cache.write``
(context size); ``output_tokens`` sums ``tokens.output`` across turns;
``cumulative_input_tokens`` sums per-turn input+cache across the
session.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, MoveReport, Provider, SessionMeta
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
        if not self._data_dir.is_dir():
            return []
        return sorted(p for p in self._data_dir.glob("opencode*.db") if p.is_file())

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
                    cur = conn.execute(
                        "UPDATE session SET directory = ? WHERE directory = ?",
                        (new_path, old_path),
                    )
                    conn.commit()
                    files_modified += cur.rowcount if cur.rowcount > 0 else 0
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
        """Read all sessions from one opencode SQLite database."""
        db_str = str(db_path)
        cache = self._cache
        if cache:
            cached = cache.get_sessions(db_str)
            if cached:
                return cached

        sessions: list[SessionMeta] = []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return sessions

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

            counts: dict[str, int] = {}
            try:
                for sid, n in cur.execute(
                    "SELECT session_id, COUNT(*) FROM message GROUP BY session_id"
                ).fetchall():
                    counts[sid] = n
            except sqlite3.Error:
                pass

            for row in rows:
                (
                    sid, directory, title, t_created, t_updated,
                    model_json, tok_in, tok_out, tok_cr, tok_cw,
                ) = row
                if not sid or not directory:
                    continue

                model = None
                if model_json:
                    try:
                        model_obj = json.loads(model_json)
                        if isinstance(model_obj, dict):
                            model = model_obj.get("id") or None
                    except (json.JSONDecodeError, TypeError):
                        pass

                last_input, last_model = self._last_assistant_tokens_db(cur, sid)
                if model is None:
                    model = last_model

                output_tokens = int(tok_out) if tok_out else None
                cumulative = None
                if any(v for v in (tok_in, tok_cr, tok_cw)):
                    cumulative = int(tok_in or 0) + int(tok_cr or 0) + int(tok_cw or 0)

                sessions.append(SessionMeta(
                    id=sid,
                    project_path=directory,
                    provider=Provider.OPENCODE,
                    summary=(title or "").strip() or "OpenCode Session",
                    timestamp=_parse_timestamp(t_updated),
                    start_timestamp=_parse_timestamp(t_created),
                    message_count=counts.get(sid, 0),
                    model=model,
                    source_path=db_str,
                    input_tokens=last_input,
                    output_tokens=output_tokens,
                    cumulative_input_tokens=cumulative,
                    host=self.host,
                ))
        except sqlite3.Error:
            pass
        finally:
            conn.close()

        if cache and sessions:
            cache.put_sessions(db_str, sessions)
        return sessions

    @staticmethod
    def _last_assistant_tokens_db(
        cur: sqlite3.Cursor, session_id: str
    ) -> tuple[int | None, str | None]:
        """Return (last turn input+cache tokens, modelID) for a session."""
        try:
            cur2 = cur.connection.execute(
                "SELECT data FROM message WHERE session_id = ? ORDER BY id DESC",
                (session_id,),
            )
        except sqlite3.Error:
            return None, None
        for (data,) in cur2:
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
                role = minfo.get("role")
                if role in ("user", "assistant"):
                    message_count += 1
                if role == "assistant":
                    model = minfo.get("modelID") or model
                    tokens = _tokens_from_message(minfo)
                    if tokens is not None and any(tokens):
                        saw_tokens = True
                        last_input = tokens[0]
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
        messages: list[Message] = []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return messages

        try:
            rows = conn.execute(
                "SELECT m.id, m.data, p.id, p.data"
                " FROM message m LEFT JOIN part p ON p.message_id = m.id"
                " WHERE m.session_id = ?"
                " ORDER BY m.id, p.id",
                (session_id,),
            )
            for _mid, mdata, _pid, pdata in rows:
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
        finally:
            conn.close()

        return messages

    def _get_messages_storage(self, info_file: Path, session_id: str) -> list[Message]:
        storage = info_file.parent.parent.parent
        msg_dir = storage / "message" / session_id
        if not msg_dir.is_dir():
            return []

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
            conn.execute("DELETE FROM part WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM message WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
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
