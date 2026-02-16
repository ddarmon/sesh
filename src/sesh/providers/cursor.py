"""Cursor IDE session provider."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, Provider, SessionMeta
from sesh.providers import SessionProvider

CURSOR_CHATS_DIR = Path.home() / ".cursor" / "chats"


def _extract_content(content) -> str:
    """Extract text from string or list-of-blocks content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            block_type = item.get("type", "")
            if block_type in ("text", "reasoning"):
                text = item.get("text", "")
                if text:
                    parts.append(text)
            elif block_type == "tool-result":
                result = item.get("result", "")
                if result:
                    parts.append(result)
        return "\n".join(parts)
    return ""


def _extract_tool_name(content) -> str | None:
    """Extract tool name from list-of-blocks content."""
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict):
            name = item.get("toolName")
            if name:
                return name
    return None


class CursorProvider(SessionProvider):
    """Provider for Cursor IDE sessions."""

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for projects with Cursor sessions."""
        if not CURSOR_CHATS_DIR.is_dir():
            return

        for hash_dir in CURSOR_CHATS_DIR.iterdir():
            if not hash_dir.is_dir():
                continue
            workspace = self._extract_workspace_path(hash_dir)
            if workspace:
                display_name = Path(workspace).name or workspace
                yield workspace, display_name

    def get_sessions(self, project_path: str) -> list[SessionMeta]:
        """Return Cursor sessions for a project path."""
        md5 = hashlib.md5(project_path.encode()).hexdigest()
        cursor_dir = CURSOR_CHATS_DIR / md5

        if not cursor_dir.is_dir():
            return []

        sessions = []
        for session_dir in cursor_dir.iterdir():
            if not session_dir.is_dir():
                continue

            store_db = session_dir / "store.db"
            if not store_db.is_file():
                continue

            meta = self._read_session_meta(store_db)
            if meta:
                sessions.append(SessionMeta(
                    id=session_dir.name,
                    project_path=project_path,
                    provider=Provider.CURSOR,
                    summary=meta.get("title", "Untitled Session"),
                    timestamp=meta.get("timestamp", datetime.now(tz=timezone.utc)),
                    message_count=meta.get("message_count", 0),
                    model=meta.get("model"),
                    source_path=str(store_db),
                ))

        sessions.sort(key=lambda s: s.timestamp, reverse=True)
        return sessions

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages from a Cursor session's store.db."""
        if not session.source_path:
            return []

        store_db = Path(session.source_path)
        if not store_db.is_file():
            return []

        messages = []
        try:
            conn = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
            cursor = conn.cursor()

            # Read blobs table for messages (schema: id TEXT, data BLOB)
            try:
                cursor.execute("SELECT id, data FROM blobs ORDER BY rowid")
                for _blob_id, blob_data in cursor.fetchall():
                    if not blob_data:
                        continue
                    # Blobs are either JSON messages or binary protobuf
                    # internal data.  Only JSON blobs have role/content.
                    try:
                        text = (
                            blob_data.decode("utf-8")
                            if isinstance(blob_data, bytes)
                            else str(blob_data)
                        )
                        data = json.loads(text)
                        if isinstance(data, dict):
                            role = data.get("role", "")
                            raw_content = data.get("content", "")
                            extracted = _extract_content(raw_content)
                            if role and extracted:
                                messages.append(Message(
                                    role=role,
                                    content=extracted,
                                    timestamp=None,
                                    tool_name=_extract_tool_name(raw_content),
                                    is_system=role == "system",
                                ))
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                        continue
            except sqlite3.OperationalError:
                pass

            conn.close()
        except (sqlite3.Error, OSError):
            pass

        return messages

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a Cursor session by removing its directory."""
        if session.source_path:
            session_dir = Path(session.source_path).parent
            if session_dir.is_dir():
                shutil.rmtree(session_dir)

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
