"""Cursor IDE session provider."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, Provider, SessionMeta
from sesh.providers import SessionProvider

CURSOR_CHATS_DIR = Path.home() / ".cursor" / "chats"


class CursorProvider(SessionProvider):
    """Provider for Cursor IDE sessions."""

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for projects with Cursor sessions.

        Strategy: iterate all hash directories under ~/.cursor/chats/,
        then try to reverse-map them to known project paths from other providers.
        Since we can't reverse an MD5 hash, we instead enumerate known project
        paths (from Claude/Codex) and check if their hash directory exists.
        """
        if not CURSOR_CHATS_DIR.is_dir():
            return

        # Collect known project paths from other providers
        known_paths = self._collect_known_project_paths()

        for project_path in known_paths:
            md5 = hashlib.md5(project_path.encode()).hexdigest()
            cursor_dir = CURSOR_CHATS_DIR / md5
            if cursor_dir.is_dir():
                display_name = Path(project_path).name or project_path
                yield project_path, display_name

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

            # Read blobs table for messages
            try:
                cursor.execute("SELECT key, value FROM blobs ORDER BY rowid")
                for key, value in cursor.fetchall():
                    if not value:
                        continue
                    try:
                        data = self._decode_value(value)
                        if isinstance(data, dict):
                            role = data.get("role", "")
                            content = data.get("content", "")
                            if role and content:
                                messages.append(Message(
                                    role=role,
                                    content=content if isinstance(content, str) else str(content),
                                    timestamp=None,
                                ))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
            except sqlite3.OperationalError:
                pass

            conn.close()
        except (sqlite3.Error, OSError):
            pass

        return messages

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

            # Get message count from blobs table
            msg_count = 0
            try:
                cursor.execute("SELECT COUNT(*) FROM blobs")
                row = cursor.fetchone()
                if row:
                    msg_count = row[0]
            except sqlite3.OperationalError:
                pass

            conn.close()

            # Extract title
            title = "Untitled Session"
            if isinstance(metadata.get("title"), str):
                title = metadata["title"]
            elif isinstance(metadata.get("sessionTitle"), str):
                title = metadata["sessionTitle"]

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

    def _collect_known_project_paths(self) -> list[str]:
        """Collect project paths from Claude and Codex providers."""
        paths = set()

        # From Claude projects
        claude_projects_dir = Path.home() / ".claude" / "projects"
        if claude_projects_dir.is_dir():
            from sesh.providers.claude import _extract_project_path
            for entry in claude_projects_dir.iterdir():
                if entry.is_dir():
                    project_path = _extract_project_path(entry.name, entry)
                    paths.add(project_path)

        return sorted(paths)
