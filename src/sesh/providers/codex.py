"""Codex CLI session provider."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, MoveReport, Provider, SessionMeta
from sesh.providers import SessionProvider

CODEX_DIR = Path.home() / ".codex" / "sessions"


def _parse_timestamp(ts) -> datetime:
    if isinstance(ts, str):
        ts_str = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(ts_str)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _extract_text_from_content(content: list) -> str:
    """Extract text from a Codex content array."""
    parts = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text") or item.get("input_text") or item.get("output_text") or ""
            if text:
                parts.append(text)
    return "\n".join(parts)


def _stringify_tool_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2)
    except TypeError:
        return str(value)


def _rewrite_codex_jsonl(jsonl_file: Path, old_path: str, new_path: str) -> bool:
    """Rewrite Codex cwd references in a JSONL file. Returns True if modified."""
    old_cwd_tag = f"<cwd>{old_path}</cwd>"
    new_cwd_tag = f"<cwd>{new_path}</cwd>"
    output: list[str] = []
    modified = False

    with open(jsonl_file) as f:
        for idx, line in enumerate(f):
            stripped = line.strip()
            if not stripped:
                output.append(line)
                continue

            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                replaced = line.replace(old_cwd_tag, new_cwd_tag)
                if replaced != line:
                    modified = True
                output.append(replaced)
                continue

            # New format: update cwd in the session_meta first entry.
            if idx == 0 and entry.get("type") == "session_meta":
                payload = entry.get("payload")
                if isinstance(payload, dict) and payload.get("cwd") == old_path:
                    payload = payload.copy()
                    payload["cwd"] = new_path
                    entry = entry.copy()
                    entry["payload"] = payload
                    line = json.dumps(entry) + "\n"
                    output.append(line)
                    modified = True
                    continue

            # Legacy format: replace <cwd>...</cwd> in content text blocks.
            entry_changed = False
            payload = entry.get("payload")
            if isinstance(payload, dict):
                content = payload.get("content")
                if isinstance(content, list):
                    new_content = []
                    for item in content:
                        if isinstance(item, dict):
                            new_item = item.copy()
                            item_changed = False
                            for key in ("text", "input_text", "output_text"):
                                value = new_item.get(key)
                                if isinstance(value, str) and old_cwd_tag in value:
                                    new_item[key] = value.replace(old_cwd_tag, new_cwd_tag)
                                    item_changed = True
                            if item_changed:
                                entry_changed = True
                            new_content.append(new_item)
                        else:
                            new_content.append(item)
                    if entry_changed:
                        payload = payload.copy()
                        payload["content"] = new_content
                        entry = entry.copy()
                        entry["payload"] = payload

            if entry_changed:
                line = json.dumps(entry) + "\n"
                output.append(line)
                modified = True
                continue

            replaced = line.replace(old_cwd_tag, new_cwd_tag)
            if replaced != line:
                modified = True
                output.append(replaced)
            else:
                output.append(line)

    if not modified:
        return False

    fd, tmp = tempfile.mkstemp(dir=str(jsonl_file.parent), suffix=".jsonl.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(output)
        os.replace(tmp, str(jsonl_file))
    except BaseException:
        os.unlink(tmp)
        raise
    return True


class CodexProvider(SessionProvider):
    """Provider for OpenAI Codex CLI sessions."""

    def __init__(self, cache=None) -> None:
        self._index: dict[str, list[dict]] | None = None
        self._cache = cache

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each Codex project."""
        if not CODEX_DIR.is_dir():
            return

        index = self._build_index()
        for project_path in sorted(index.keys()):
            # Skip clearly invalid project paths
            if project_path in ("/", ""):
                continue
            display_name = Path(project_path).name or project_path
            yield project_path, display_name

    def get_sessions(self, project_path: str, cache=None) -> list[SessionMeta]:
        """Return sessions for a given project path."""
        index = self._build_index()
        sessions_data = index.get(project_path, [])

        result = []
        for s in sessions_data:
            result.append(SessionMeta(
                id=s["id"],
                project_path=project_path,
                provider=Provider.CODEX,
                summary=s.get("summary", "Codex Session"),
                timestamp=s["timestamp"],
                message_count=s.get("message_count", 0),
                model=s.get("model"),
                source_path=s.get("file_path"),
            ))

        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages from a Codex session file."""
        if not session.source_path:
            return []

        file_path = Path(session.source_path)
        if not file_path.is_file():
            return []

        messages = []
        call_id_map: dict[str, str] = {}  # call_id -> function name
        try:
            with open(file_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    entry_type = entry.get("type", "")
                    payload = entry.get("payload", {})
                    ts = _parse_timestamp(entry.get("timestamp", ""))

                    # User messages
                    if entry_type == "event_msg" and payload.get("type") == "user_message":
                        text = payload.get("message", "")
                        if text:
                            messages.append(Message(
                                role="user",
                                content=text,
                                timestamp=ts,
                            ))

                    # Agent reasoning
                    elif entry_type == "event_msg" and payload.get("type") == "agent_reasoning":
                        text = payload.get("text", "")
                        if text.strip():
                            messages.append(Message(
                                role="assistant",
                                content="",
                                timestamp=ts,
                                thinking=text,
                                content_type="thinking",
                            ))

                    # Function calls
                    elif entry_type == "response_item" and payload.get("type") == "function_call":
                        name = payload.get("name", "")
                        call_id = payload.get("call_id", "")
                        if call_id and name:
                            call_id_map[call_id] = name
                        args = _stringify_tool_value(payload.get("arguments", ""))
                        messages.append(Message(
                            role="assistant",
                            content="",
                            timestamp=ts,
                            tool_name=name,
                            tool_input=args,
                            content_type="tool_use",
                        ))

                    # Function call output
                    elif entry_type == "response_item" and payload.get("type") == "function_call_output":
                        call_id = payload.get("call_id", "")
                        resolved_name = call_id_map.get(call_id, "")
                        output = _stringify_tool_value(payload.get("output", ""))
                        messages.append(Message(
                            role="tool",
                            content="",
                            timestamp=ts,
                            tool_name=resolved_name,
                            tool_output=output,
                            content_type="tool_result",
                        ))

                    # Assistant messages
                    elif entry_type == "response_item" and payload.get("role") == "assistant":
                        content = payload.get("content", [])
                        text = _extract_text_from_content(content) if isinstance(content, list) else str(content)
                        if text.strip():
                            messages.append(Message(
                                role="assistant",
                                content=text,
                                timestamp=ts,
                            ))

                    # Developer/system context in response_item format — skip these
                    elif entry_type == "response_item" and payload.get("role") in ("user", "developer"):
                        pass  # These are system instructions, not real user messages

        except OSError:
            pass

        return messages

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a Codex session by removing its JSONL file."""
        if session.source_path:
            Path(session.source_path).unlink(missing_ok=True)

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Update Codex metadata when a project path changes."""
        if not CODEX_DIR.is_dir():
            return MoveReport(provider=Provider.CODEX, success=True)

        files_modified = 0
        try:
            for jsonl_file in CODEX_DIR.rglob("*.jsonl"):
                if _rewrite_codex_jsonl(jsonl_file, old_path, new_path):
                    files_modified += 1
        except OSError as exc:
            return MoveReport(
                provider=Provider.CODEX,
                success=False,
                files_modified=files_modified,
                error=f"Failed updating Codex session metadata: {exc}",
            )

        self._index = None
        return MoveReport(
            provider=Provider.CODEX,
            success=True,
            files_modified=files_modified,
        )

    def _build_index(self) -> dict[str, list[dict]]:
        """Build index of project_path -> [{session data}]."""
        if self._index is not None:
            return self._index

        self._index = {}
        if not CODEX_DIR.is_dir():
            return self._index

        cache = self._cache

        for jsonl_file in CODEX_DIR.rglob("*.jsonl"):
            file_str = str(jsonl_file)

            # Check per-file cache first
            if cache:
                cached_sessions = cache.get_sessions(file_str)
                if cached_sessions:
                    for s in cached_sessions:
                        cwd = s.project_path
                        if cwd not in self._index:
                            self._index[cwd] = []
                        self._index[cwd].append({
                            "id": s.id,
                            "cwd": s.project_path,
                            "model": s.model,
                            "timestamp": s.timestamp,
                            "summary": s.summary,
                            "message_count": s.message_count,
                            "file_path": s.source_path or file_str,
                        })
                    continue

            data = self._parse_session_file(jsonl_file)
            if data and data.get("cwd"):
                cwd = data["cwd"]
                if cwd not in self._index:
                    self._index[cwd] = []
                self._index[cwd].append(data)

                # Store in cache for next time
                if cache:
                    cache.put_sessions(file_str, [SessionMeta(
                        id=data["id"],
                        project_path=data["cwd"],
                        provider=Provider.CODEX,
                        summary=data.get("summary", ""),
                        timestamp=data["timestamp"],
                        message_count=data.get("message_count", 0),
                        model=data.get("model"),
                        source_path=data.get("file_path"),
                    )])

        return self._index

    def _parse_session_file(self, file_path: Path) -> dict | None:
        """Parse a Codex JSONL file to extract session metadata."""
        try:
            with open(file_path) as f:
                first_line = f.readline().strip()
                if not first_line:
                    return None

                first_entry = json.loads(first_line)

                # New format: first line has type=session_meta
                if first_entry.get("type") == "session_meta":
                    payload = first_entry.get("payload", {})
                    session_id = payload.get("id", file_path.stem)
                    cwd = payload.get("cwd", "")
                    model = payload.get("model") or payload.get("model_provider", "")

                    # Scan rest for last timestamp and user messages
                    last_ts = first_entry.get("timestamp")
                    first_user_msg = None
                    msg_count = 0

                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("timestamp"):
                                last_ts = entry["timestamp"]

                            etype = entry.get("type", "")
                            epayload = entry.get("payload", {})

                            if etype == "event_msg" and epayload.get("type") == "user_message":
                                msg_count += 1
                                if first_user_msg is None:
                                    first_user_msg = epayload.get("message", "")
                            elif etype == "response_item" and epayload.get("role") == "assistant":
                                msg_count += 1
                        except json.JSONDecodeError:
                            continue

                    summary = "Codex Session"
                    if first_user_msg:
                        summary = first_user_msg[:80] + ("..." if len(first_user_msg) > 80 else "")

                    return {
                        "id": session_id,
                        "cwd": cwd,
                        "model": model,
                        "timestamp": _parse_timestamp(last_ts),
                        "summary": summary,
                        "message_count": msg_count,
                        "file_path": str(file_path),
                    }

                # Legacy format: no session_meta, extract cwd from environment_context XML
                else:
                    cwd = ""
                    session_id = file_path.stem
                    last_ts = first_entry.get("timestamp")
                    first_user_msg = None
                    msg_count = 0

                    # Re-read from start for legacy format
                    f.seek(0)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("timestamp"):
                                last_ts = entry["timestamp"]

                            # Extract cwd from environment_context XML
                            payload = entry.get("payload", {})
                            content = payload.get("content", [])
                            if isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict):
                                        text = item.get("text", "") or item.get("input_text", "")
                                        if "<cwd>" in text:
                                            match = re.search(r"<cwd>(.*?)</cwd>", text)
                                            if match:
                                                cwd = match.group(1)

                            etype = entry.get("type", "")
                            if etype == "event_msg" and payload.get("type") == "user_message":
                                msg_count += 1
                                if first_user_msg is None:
                                    first_user_msg = payload.get("message", "")
                        except json.JSONDecodeError:
                            continue

                    if not cwd:
                        return None

                    summary = "Codex Session"
                    if first_user_msg:
                        summary = first_user_msg[:80] + ("..." if len(first_user_msg) > 80 else "")

                    return {
                        "id": session_id,
                        "cwd": cwd,
                        "model": "",
                        "timestamp": _parse_timestamp(last_ts),
                        "summary": summary,
                        "message_count": msg_count,
                        "file_path": str(file_path),
                    }

        except (json.JSONDecodeError, OSError):
            return None
