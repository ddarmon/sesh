"""Cursor IDE session provider."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, MoveReport, Provider, SessionMeta, encode_project_path, workspace_uri
from sesh.providers import SessionProvider

CURSOR_CHATS_DIR = Path.home() / ".cursor" / "chats"
CURSOR_PROJECTS_DIR = Path.home() / ".cursor" / "projects"

if sys.platform == "darwin":
    WORKSPACE_STORAGE = (
        Path.home() / "Library" / "Application Support"
        / "Cursor" / "User" / "workspaceStorage"
    )
else:
    WORKSPACE_STORAGE = Path.home() / ".config" / "Cursor" / "User" / "workspaceStorage"


def _stringify_tool_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2)
    except TypeError:
        return str(value)


def _rewrite_workspace_json(workspace_json: Path, old_uri: str, new_uri: str) -> bool:
    """Rewrite a workspace.json folder URI atomically. Returns True if modified."""
    data = json.loads(workspace_json.read_text())
    if data.get("folder") != old_uri:
        return False

    data["folder"] = new_uri
    fd, tmp = tempfile.mkstemp(dir=str(workspace_json.parent), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(workspace_json))
    except BaseException:
        os.unlink(tmp)
        raise
    return True


def _rewrite_store_db_blobs(store_db: Path, old_path: str, new_path: str) -> bool:
    """Rewrite old_path references in a store.db blobs table."""
    conn = sqlite3.connect(store_db, timeout=5)
    modified = False
    try:
        cur = conn.cursor()
        cur.execute("SELECT rowid, data FROM blobs")
        updates = []
        for rowid, blob_data in cur.fetchall():
            if not blob_data:
                continue
            if isinstance(blob_data, bytes):
                try:
                    text = blob_data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                new_text = text.replace(old_path, new_path)
                if new_text != text:
                    updates.append((new_text.encode("utf-8"), rowid))
            else:
                text = str(blob_data)
                new_text = text.replace(old_path, new_path)
                if new_text != text:
                    updates.append((new_text, rowid))

        if updates:
            cur.executemany("UPDATE blobs SET data = ? WHERE rowid = ?", updates)
            conn.commit()
            modified = True
    finally:
        conn.close()
    return modified


class CursorProvider(SessionProvider):
    """Provider for Cursor IDE sessions."""

    def __init__(self) -> None:
        self._workspace_map: dict[str, str] | None = None
        self._projects_dir_map: dict[str, Path] | None = None

    def _build_workspace_map(self) -> dict[str, str]:
        """Return {project_path: workspace_hash} from workspaceStorage."""
        if self._workspace_map is not None:
            return self._workspace_map
        self._workspace_map = {}
        if not WORKSPACE_STORAGE.is_dir():
            return self._workspace_map
        for ws_dir in WORKSPACE_STORAGE.iterdir():
            ws_json = ws_dir / "workspace.json"
            if not ws_json.is_file():
                continue
            try:
                data = json.loads(ws_json.read_text())
                folder_uri = data.get("folder", "")
                if folder_uri.startswith("file:///"):
                    project_path = folder_uri[len("file://"):]
                    self._workspace_map[project_path] = ws_dir.name
            except (json.JSONDecodeError, OSError):
                continue
        return self._workspace_map

    def _build_projects_dir_map(self) -> dict[str, Path]:
        """Return {project_path: cursor_projects_subdir} from ~/.cursor/projects/."""
        if self._projects_dir_map is not None:
            return self._projects_dir_map
        self._projects_dir_map = {}
        if not CURSOR_PROJECTS_DIR.is_dir():
            return self._projects_dir_map
        workspace_map = self._build_workspace_map()
        # Build reverse: encoded_name -> project_path from workspace_map
        for project_path in workspace_map:
            encoded = encode_project_path(project_path)
            candidate = CURSOR_PROJECTS_DIR / encoded
            if candidate.is_dir():
                self._projects_dir_map[project_path] = candidate
        return self._projects_dir_map

    def _find_projects_dir(self, project_path: str) -> Path | None:
        """Find the ~/.cursor/projects/ subdirectory for a project path."""
        return self._build_projects_dir_map().get(project_path)

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for projects with Cursor sessions."""
        seen: set[str] = set()

        # 1. Existing: CLI agent sessions in ~/.cursor/chats/
        if CURSOR_CHATS_DIR.is_dir():
            for hash_dir in CURSOR_CHATS_DIR.iterdir():
                if not hash_dir.is_dir():
                    continue
                workspace = self._extract_workspace_path(hash_dir)
                if workspace:
                    seen.add(workspace)
                    display_name = Path(workspace).name or workspace
                    yield workspace, display_name

        # 2. IDE sessions via workspaceStorage
        projects_dir_map = self._build_projects_dir_map()
        for project_path, proj_dir in projects_dir_map.items():
            if project_path in seen:
                continue
            transcripts = proj_dir / "agent-transcripts"
            if transcripts.is_dir() and any(transcripts.glob("*.txt")):
                seen.add(project_path)
                display_name = Path(project_path).name or project_path
                yield project_path, display_name

    def get_sessions(self, project_path: str, cache=None) -> list[SessionMeta]:
        """Return Cursor sessions for a project path."""
        sessions: list[SessionMeta] = []
        seen_ids: set[str] = set()

        # 1. CLI agent sessions from ~/.cursor/chats/
        md5 = hashlib.md5(project_path.encode()).hexdigest()
        cursor_dir = CURSOR_CHATS_DIR / md5
        if cursor_dir.is_dir():
            for session_dir in cursor_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                store_db = session_dir / "store.db"
                if not store_db.is_file():
                    continue

                if cache:
                    cached = cache.get_sessions(str(store_db))
                    if cached:
                        for s in cached:
                            seen_ids.add(s.id)
                            sessions.append(s)
                        continue

                meta = self._read_session_meta(store_db)
                if meta:
                    seen_ids.add(session_dir.name)
                    session = SessionMeta(
                        id=session_dir.name,
                        project_path=project_path,
                        provider=Provider.CURSOR,
                        summary=meta.get("title", "Untitled Session"),
                        timestamp=meta.get("timestamp", datetime.now(tz=timezone.utc)),
                        message_count=meta.get("message_count", 0),
                        model=meta.get("model"),
                        source_path=str(store_db),
                    )
                    sessions.append(session)
                    if cache:
                        cache.put_sessions(str(store_db), [session])

        # 2. IDE sessions from workspaceStorage + agent-transcripts
        ide_sessions = self._get_ide_sessions(project_path)
        for s in ide_sessions:
            if s.id not in seen_ids:
                sessions.append(s)

        sessions.sort(key=lambda s: s.timestamp, reverse=True)
        return sessions

    def _get_ide_sessions(self, project_path: str) -> list[SessionMeta]:
        """Return IDE sessions from state.vscdb + agent-transcripts."""
        proj_dir = self._find_projects_dir(project_path)
        if not proj_dir:
            return []
        transcripts_dir = proj_dir / "agent-transcripts"
        if not transcripts_dir.is_dir():
            return []

        # Build set of available transcript files
        txt_files: dict[str, Path] = {}
        for f in transcripts_dir.iterdir():
            if f.suffix == ".txt" and f.is_file():
                txt_files[f.stem] = f

        if not txt_files:
            return []

        # Try to get rich metadata from state.vscdb
        composer_meta = self._read_composer_data(project_path)

        sessions: list[SessionMeta] = []
        matched_ids: set[str] = set()

        # Match composer metadata to transcript files
        for entry in composer_meta:
            composer_id = entry.get("composerId", "")
            if composer_id in txt_files:
                matched_ids.add(composer_id)
                transcript_path = txt_files[composer_id]

                name = entry.get("name", "")
                if not name:
                    name = self._first_user_message(transcript_path) or "Untitled Session"

                timestamp = datetime.now(tz=timezone.utc)
                created = entry.get("createdAt")
                if isinstance(created, (int, float)):
                    try:
                        timestamp = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
                    except (ValueError, OSError):
                        pass

                model = entry.get("lastUsedModel")
                msg_count = self._count_transcript_messages(transcript_path)

                sessions.append(SessionMeta(
                    id=composer_id,
                    project_path=project_path,
                    provider=Provider.CURSOR,
                    summary=name,
                    timestamp=timestamp,
                    message_count=msg_count,
                    model=model,
                    source_path=str(transcript_path),
                ))

        # Pick up any .txt files not matched by composer metadata
        for stem, path in txt_files.items():
            if stem in matched_ids:
                continue
            name = self._first_user_message(path) or "Untitled Session"
            try:
                mtime = path.stat().st_mtime
                timestamp = datetime.fromtimestamp(mtime, tz=timezone.utc)
            except OSError:
                timestamp = datetime.now(tz=timezone.utc)
            sessions.append(SessionMeta(
                id=stem,
                project_path=project_path,
                provider=Provider.CURSOR,
                summary=name,
                timestamp=timestamp,
                message_count=self._count_transcript_messages(path),
                source_path=str(path),
            ))

        return sessions

    def _read_composer_data(self, project_path: str) -> list[dict]:
        """Read composer.composerData from the workspace's state.vscdb."""
        workspace_map = self._build_workspace_map()
        ws_hash = workspace_map.get(project_path)
        if not ws_hash:
            return []
        vscdb = WORKSPACE_STORAGE / ws_hash / "state.vscdb"
        if not vscdb.is_file():
            return []
        try:
            conn = sqlite3.connect(f"file:{vscdb}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
            )
            row = cur.fetchone()
            conn.close()
            if not row:
                return []
            data = json.loads(row[0])
            return data.get("allComposers", [])
        except (sqlite3.Error, json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def _first_user_message(transcript: Path) -> str | None:
        """Extract the first user message text from a .txt transcript."""
        try:
            in_user = False
            lines: list[str] = []
            for line in open(transcript):
                if line.rstrip() == "user:" and not in_user:
                    in_user = True
                    continue
                if in_user:
                    if line.rstrip() == "assistant:" or (
                        lines and line.rstrip() == ""
                        and any(l.strip() for l in lines)
                    ):
                        break
                    stripped = line.strip()
                    if stripped in ("<user_query>", "</user_query>"):
                        continue
                    if stripped:
                        lines.append(stripped)
            text = " ".join(lines).strip()
            return text[:80] if text else None
        except OSError:
            return None

    @staticmethod
    def _count_transcript_messages(transcript: Path) -> int:
        """Count user+assistant turns in a .txt transcript."""
        count = 0
        try:
            for line in open(transcript):
                if line.rstrip() in ("user:", "assistant:"):
                    count += 1
        except OSError:
            pass
        return count

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages from a Cursor session."""
        if not session.source_path:
            return []

        source = Path(session.source_path)
        if not source.is_file():
            return []

        if source.suffix == ".txt":
            return self._parse_txt_transcript(source)
        return self._parse_store_db(source)

    @staticmethod
    def _parse_store_db(store_db: Path) -> list[Message]:
        """Load messages from a store.db file."""
        messages: list[Message] = []
        try:
            conn = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
            cursor = conn.cursor()

            try:
                cursor.execute("SELECT id, data FROM blobs ORDER BY rowid")
                for _blob_id, blob_data in cursor.fetchall():
                    if not blob_data:
                        continue
                    try:
                        text = (
                            blob_data.decode("utf-8")
                            if isinstance(blob_data, bytes)
                            else str(blob_data)
                        )
                        data = json.loads(text)
                        if not isinstance(data, dict):
                            continue
                        role = data.get("role", "")
                        if not role:
                            continue
                        raw_content = data.get("content", "")

                        if isinstance(raw_content, str):
                            # Plain string content
                            if raw_content.strip():
                                messages.append(Message(
                                    role=role,
                                    content=raw_content,
                                    timestamp=None,
                                    is_system=role == "system",
                                    content_type="text",
                                ))
                        elif isinstance(raw_content, list):
                            for block in raw_content:
                                if not isinstance(block, dict):
                                    continue
                                btype = block.get("type", "")

                                if btype == "text":
                                    t = block.get("text", "")
                                    if t.strip():
                                        messages.append(Message(
                                            role=role,
                                            content=t,
                                            timestamp=None,
                                            is_system=role == "system",
                                            content_type="text",
                                        ))

                                elif btype == "reasoning":
                                    t = block.get("text", "")
                                    if t.strip():
                                        messages.append(Message(
                                            role="assistant",
                                            content="",
                                            timestamp=None,
                                            thinking=t,
                                            content_type="thinking",
                                        ))

                                elif btype == "tool-call":
                                    name = block.get("toolName", "")
                                    args = block.get("args", {})
                                    messages.append(Message(
                                        role="assistant",
                                        content="",
                                        timestamp=None,
                                        tool_name=name,
                                        tool_input=_stringify_tool_value(args),
                                        content_type="tool_use",
                                    ))

                                elif btype == "tool-result":
                                    name = block.get("toolName", "")
                                    result = _stringify_tool_value(block.get("result", ""))
                                    messages.append(Message(
                                        role="tool",
                                        content="",
                                        timestamp=None,
                                        tool_name=name,
                                        tool_output=result,
                                        content_type="tool_result",
                                    ))
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                        continue
            except sqlite3.OperationalError:
                pass

            conn.close()
        except (sqlite3.Error, OSError):
            pass

        return messages

    @staticmethod
    def _parse_txt_transcript(transcript: Path) -> list[Message]:
        """Parse a plain-text agent transcript file into messages."""
        messages: list[Message] = []
        current_role: str | None = None
        lines: list[str] = []

        def _flush() -> None:
            if current_role and lines:
                text = "\n".join(lines).strip()
                if text:
                    messages.append(Message(
                        role=current_role,
                        content=text,
                        timestamp=None,
                        is_system=current_role == "system",
                    ))

        try:
            for raw_line in open(transcript):
                line = raw_line.rstrip("\n")
                stripped = line.rstrip()

                # Detect role transitions
                if stripped == "user:" or stripped == "assistant:" or stripped == "system:":
                    _flush()
                    current_role = stripped[:-1]  # strip trailing ':'
                    lines = []
                    continue

                # Strip <user_query> / </user_query> tags
                if stripped in ("<user_query>", "</user_query>"):
                    continue

                if current_role is not None:
                    lines.append(line)

            _flush()
        except OSError:
            pass

        return messages

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a Cursor session."""
        if not session.source_path:
            return
        source = Path(session.source_path)
        if source.suffix == ".txt":
            # IDE transcript: just remove the file
            if source.is_file():
                source.unlink()
        else:
            # CLI agent store.db: remove the session directory
            session_dir = source.parent
            if session_dir.is_dir():
                shutil.rmtree(session_dir)

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Update Cursor metadata when a project path changes."""
        old_md5 = hashlib.md5(old_path.encode()).hexdigest()
        new_md5 = hashlib.md5(new_path.encode()).hexdigest()
        old_chats_dir = CURSOR_CHATS_DIR / old_md5
        new_chats_dir = CURSOR_CHATS_DIR / new_md5

        old_encoded = encode_project_path(old_path)
        new_encoded = encode_project_path(new_path)
        old_projects_dir = CURSOR_PROJECTS_DIR / old_encoded
        new_projects_dir = CURSOR_PROJECTS_DIR / new_encoded

        conflicts = []
        if old_chats_dir.is_dir() and new_chats_dir.exists():
            conflicts.append(f"target chats directory exists: {new_chats_dir}")
        if old_projects_dir.is_dir() and new_projects_dir.exists():
            conflicts.append(f"target projects directory exists: {new_projects_dir}")
        if conflicts:
            return MoveReport(
                provider=Provider.CURSOR,
                success=False,
                error="; ".join(conflicts),
            )

        files_modified = 0
        dirs_renamed = 0
        warnings: list[str] = []

        chats_dir: Path | None = None
        if old_chats_dir.is_dir():
            try:
                old_chats_dir.rename(new_chats_dir)
            except OSError as exc:
                return MoveReport(
                    provider=Provider.CURSOR,
                    success=False,
                    error=f"Failed to rename Cursor chats directory: {exc}",
                )
            dirs_renamed += 1
            chats_dir = new_chats_dir
        elif new_chats_dir.is_dir():
            chats_dir = new_chats_dir

        if old_projects_dir.is_dir():
            try:
                old_projects_dir.rename(new_projects_dir)
            except OSError as exc:
                return MoveReport(
                    provider=Provider.CURSOR,
                    success=False,
                    dirs_renamed=dirs_renamed,
                    error=f"Failed to rename Cursor projects directory: {exc}",
                )
            dirs_renamed += 1

        old_uri = workspace_uri(old_path)
        new_uri = workspace_uri(new_path)
        if WORKSPACE_STORAGE.is_dir():
            for ws_dir in WORKSPACE_STORAGE.iterdir():
                workspace_json = ws_dir / "workspace.json"
                if not workspace_json.is_file():
                    continue
                try:
                    if _rewrite_workspace_json(workspace_json, old_uri, new_uri):
                        files_modified += 1
                except (json.JSONDecodeError, OSError):
                    continue

        if chats_dir and chats_dir.is_dir():
            for store_db in chats_dir.rglob("store.db"):
                try:
                    if _rewrite_store_db_blobs(store_db, old_path, new_path):
                        files_modified += 1
                except (sqlite3.Error, OSError) as exc:
                    warnings.append(f"{store_db}: {exc}")

        self._workspace_map = None
        self._projects_dir_map = None

        warning_msg = None
        if warnings:
            snippet = "; ".join(warnings[:3])
            if len(warnings) > 3:
                snippet += "; ..."
            warning_msg = f"Best-effort store.db update had errors: {snippet}"

        return MoveReport(
            provider=Provider.CURSOR,
            success=True,
            files_modified=files_modified,
            dirs_renamed=dirs_renamed,
            error=warning_msg,
        )

    def _read_session_meta(self, store_db: Path) -> dict | None:
        """Read metadata from a Cursor session's store.db."""
        try:
            conn = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
            cursor = conn.cursor()

            metadata = {}

            # Read meta table
            try:
                cursor.execute("SELECT key, value FROM meta")
                for key, value in cursor.fetchall():
                    if value:
                        try:
                            metadata[key] = self._decode_value(value)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            metadata[key] = str(value)
            except sqlite3.OperationalError:
                pass

            # Count actual message blobs (JSON with a role field).
            # Most blobs are binary protobuf internal data.
            msg_count = 0
            try:
                cursor.execute("SELECT data FROM blobs")
                for (blob_data,) in cursor.fetchall():
                    if not blob_data:
                        continue
                    try:
                        text = (
                            blob_data.decode("utf-8")
                            if isinstance(blob_data, bytes)
                            else str(blob_data)
                        )
                        obj = json.loads(text)
                        if isinstance(obj, dict) and obj.get("role"):
                            msg_count += 1
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                        continue
            except sqlite3.OperationalError:
                pass

            conn.close()

            # The meta table stores a single key "0" whose hex-decoded
            # value is a dict with the actual session fields.  Flatten
            # any nested dicts so field lookups work at the top level.
            for v in list(metadata.values()):
                if isinstance(v, dict):
                    metadata.update(v)

            # Extract title (Cursor uses "name", not "title")
            title = "Untitled Session"
            for key in ("name", "title", "sessionTitle"):
                val = metadata.get(key)
                if isinstance(val, str) and val.strip():
                    title = val
                    break

            # Extract model
            model = metadata.get("lastUsedModel")

            # Extract timestamp
            timestamp = datetime.now(tz=timezone.utc)
            created = metadata.get("createdAt")
            if created:
                try:
                    if isinstance(created, str):
                        timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    elif isinstance(created, (int, float)):
                        timestamp = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
                except (ValueError, OSError):
                    pass
            else:
                # Fall back to file mtime
                try:
                    stat = store_db.stat()
                    timestamp = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                except OSError:
                    pass

            return {
                "title": title,
                "timestamp": timestamp,
                "message_count": msg_count,
                "model": model,
            }

        except (sqlite3.Error, OSError):
            return None

    def _decode_value(self, value) -> object:
        """Decode a value that may be hex-encoded JSON or plain text."""
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        else:
            text = str(value)

        # Try hex-encoded JSON first
        if all(c in "0123456789abcdefABCDEF" for c in text) and len(text) > 2:
            try:
                decoded = bytes.fromhex(text).decode("utf-8")
                return json.loads(decoded)
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                pass

        # Try plain JSON
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    @staticmethod
    def _extract_workspace_path(hash_dir: Path) -> str | None:
        """Extract workspace path from a store.db user_info blob."""
        for session_dir in hash_dir.iterdir():
            store_db = session_dir / "store.db"
            if not store_db.is_file():
                continue
            try:
                conn = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT data FROM blobs LIMIT 10")
                for (blob_data,) in cur.fetchall():
                    if not blob_data:
                        continue
                    try:
                        text = (
                            blob_data.decode("utf-8")
                            if isinstance(blob_data, bytes)
                            else str(blob_data)
                        )
                        obj = json.loads(text)
                        content = obj.get("content", "")
                        if isinstance(content, str):
                            m = re.search(r"Workspace Path: ([^\n]+)", content)
                            if m:
                                conn.close()
                                return m.group(1).strip()
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                        continue
                conn.close()
            except (sqlite3.Error, OSError):
                continue
        return None
