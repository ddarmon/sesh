"""Claude Code session provider."""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, MoveReport, Provider, SessionMeta, encode_claude_path
from sesh.providers import SessionProvider

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"

# System message prefixes to skip
SYSTEM_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<system-reminder>",
    "Caveat:",
    "This session is being continued from a previous",
    "Invalid API key",
    "Warmup",
)


def _extract_text(content) -> str:
    """Extract text from string or [{type: 'text', text: '...'}] array format."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return ""


def _is_system_message(text: str) -> bool:
    """Check if a user message is actually a system/command message."""
    if not text:
        return True
    return any(text.startswith(p) for p in SYSTEM_PREFIXES)


def _parse_timestamp(ts) -> datetime:
    """Parse a timestamp from string or epoch millis."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    if isinstance(ts, str):
        # Handle ISO format with or without Z
        ts = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _extract_project_path(project_name: str, project_dir: Path) -> str:
    """Determine actual project path from JSONL cwd fields."""
    cwd_counts: dict[str, int] = {}
    latest_ts = 0.0
    latest_cwd = None

    if not project_dir.is_dir():
        return project_name.replace("-", "/")

    for jsonl_file in project_dir.glob("*.jsonl"):
        if jsonl_file.name.startswith("agent-"):
            continue
        try:
            with open(jsonl_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        cwd = entry.get("cwd")
                        if cwd:
                            cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1
                            ts = entry.get("timestamp")
                            if ts:
                                t = _parse_timestamp(ts).timestamp()
                                if t > latest_ts:
                                    latest_ts = t
                                    latest_cwd = cwd
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            continue

    if not cwd_counts:
        return project_name.replace("-", "/")

    if len(cwd_counts) == 1:
        return next(iter(cwd_counts))

    # Multiple cwds — prefer most recent if it has reasonable usage
    if latest_cwd:
        max_count = max(cwd_counts.values())
        if cwd_counts.get(latest_cwd, 0) >= max_count * 0.25:
            return latest_cwd

    # Fall back to most frequent
    return max(cwd_counts, key=cwd_counts.get)


def _display_name_from_path(project_path: str) -> str:
    """Generate a short display name from a project path."""
    return Path(project_path).name or project_path


def _rewrite_cwd_in_jsonl(jsonl_file: Path, old_path: str, new_path: str) -> bool:
    """Rewrite exact cwd matches in a Claude JSONL file. Returns True if modified."""
    output: list[str] = []
    modified = False

    with open(jsonl_file) as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                output.append(line)
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                output.append(line)
                continue

            if entry.get("cwd") == old_path:
                entry["cwd"] = new_path
                line = json.dumps(entry) + "\n"
                modified = True
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


class ClaudeProvider(SessionProvider):
    """Provider for Claude Code sessions."""

    def __init__(self) -> None:
        self._path_to_dir: dict[str, Path] = {}

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each Claude project."""
        if not PROJECTS_DIR.is_dir():
            return

        from sesh.cache import load_project_paths, save_project_paths

        cached_paths = load_project_paths()
        updated = False

        for entry in sorted(PROJECTS_DIR.iterdir()):
            if not entry.is_dir():
                continue
            project_name = entry.name

            try:
                dir_mtime = entry.stat().st_mtime
            except OSError:
                continue

            cached = cached_paths.get(project_name)
            if cached and cached.get("mtime") == dir_mtime:
                project_path = cached["path"]
            else:
                project_path = _extract_project_path(project_name, entry)
                cached_paths[project_name] = {"path": project_path, "mtime": dir_mtime}
                updated = True

            self._path_to_dir[project_path] = entry
            display_name = _display_name_from_path(project_path)
            yield project_path, display_name

        if updated:
            save_project_paths(cached_paths)

    def get_sessions(self, project_path: str, cache=None) -> list[SessionMeta]:
        """Return sessions for a project, grouped by first user message."""
        project_dir = self._find_project_dir(project_path)
        if not project_dir:
            return []

        if cache:
            cached = cache.get_sessions_for_dir(str(project_dir))
            if cached is not None:
                return cached

        sessions = self._parse_sessions(project_dir, project_path)

        if cache:
            cache.put_sessions_for_dir(str(project_dir), sessions)

        return sessions

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load all messages for a session from its source JSONL file."""
        if not session.source_path:
            return []

        messages: list[Message] = []
        source_dir = Path(session.source_path)
        tool_id_map: dict[str, str] = {}  # tool_use_id -> tool_name

        # source_path points to the project directory; scan all JSONL files
        if source_dir.is_dir():
            jsonl_files = sorted(source_dir.glob("*.jsonl"))
        else:
            jsonl_files = [source_dir]

        for jsonl_file in jsonl_files:
            if jsonl_file.name.startswith("agent-"):
                continue
            try:
                with open(jsonl_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if entry.get("sessionId") != session.id:
                            continue

                        msg = entry.get("message")
                        if not msg:
                            continue

                        role = msg.get("role", "")
                        ts = _parse_timestamp(entry.get("timestamp"))
                        raw_content = msg.get("content", "")

                        if isinstance(raw_content, str):
                            # Plain string content — single text message
                            if not raw_content.strip():
                                continue
                            is_sys = role == "user" and _is_system_message(raw_content)
                            messages.append(Message(
                                role=role,
                                content=raw_content,
                                timestamp=ts,
                                is_system=is_sys,
                                content_type="text",
                            ))
                        elif isinstance(raw_content, list):
                            # Content array — emit one Message per block
                            for block in raw_content:
                                if not isinstance(block, dict):
                                    continue
                                btype = block.get("type", "")

                                if btype == "text":
                                    text = block.get("text", "")
                                    if not text.strip():
                                        continue
                                    is_sys = role == "user" and _is_system_message(text)
                                    messages.append(Message(
                                        role=role,
                                        content=text,
                                        timestamp=ts,
                                        is_system=is_sys,
                                        content_type="text",
                                    ))

                                elif btype == "thinking":
                                    thinking_text = block.get("thinking", "")
                                    if not thinking_text.strip():
                                        continue
                                    messages.append(Message(
                                        role="assistant",
                                        content="",
                                        timestamp=ts,
                                        thinking=thinking_text,
                                        content_type="thinking",
                                    ))

                                elif btype == "tool_use":
                                    name = block.get("name", "")
                                    tool_id = block.get("id", "")
                                    if tool_id and name:
                                        tool_id_map[tool_id] = name
                                    inp = block.get("input", {})
                                    messages.append(Message(
                                        role="assistant",
                                        content="",
                                        timestamp=ts,
                                        tool_name=name,
                                        tool_input=json.dumps(inp, indent=2),
                                        content_type="tool_use",
                                    ))

                                elif btype == "tool_result":
                                    tool_id = block.get("tool_use_id", "")
                                    resolved_name = tool_id_map.get(tool_id, "")
                                    result_content = block.get("content", "")
                                    if isinstance(result_content, list):
                                        parts = []
                                        for part in result_content:
                                            if isinstance(part, dict) and part.get("type") == "text":
                                                parts.append(part.get("text", ""))
                                        result_str = "\n".join(parts)
                                    else:
                                        result_str = str(result_content) if result_content else ""
                                    messages.append(Message(
                                        role="tool",
                                        content="",
                                        timestamp=ts,
                                        tool_name=resolved_name,
                                        tool_output=result_str,
                                        content_type="tool_result",
                                    ))
            except OSError:
                continue

        messages.sort(key=lambda m: m.timestamp or datetime.min.replace(tzinfo=timezone.utc))
        return messages

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a Claude session by removing its lines from JSONL files."""
        source_dir = Path(session.source_path)
        if not source_dir.is_dir():
            return

        for jsonl_file in source_dir.glob("*.jsonl"):
            if jsonl_file.name.startswith("agent-"):
                continue
            try:
                kept: list[str] = []
                removed_any = False
                with open(jsonl_file) as f:
                    for line in f:
                        stripped = line.strip()
                        if not stripped:
                            kept.append(line)
                            continue
                        try:
                            entry = json.loads(stripped)
                            if entry.get("sessionId") == session.id:
                                removed_any = True
                                continue
                        except json.JSONDecodeError:
                            pass
                        kept.append(line)

                if not removed_any:
                    continue

                if not any(l.strip() for l in kept):
                    jsonl_file.unlink()
                else:
                    fd, tmp = tempfile.mkstemp(
                        dir=str(source_dir), suffix=".jsonl.tmp"
                    )
                    try:
                        with os.fdopen(fd, "w") as f:
                            f.writelines(kept)
                        os.replace(tmp, str(jsonl_file))
                    except BaseException:
                        os.unlink(tmp)
                        raise
            except OSError:
                continue

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Update Claude metadata when a project path changes."""
        old_encoded = encode_claude_path(old_path)
        new_encoded = encode_claude_path(new_path)
        old_dir = PROJECTS_DIR / old_encoded
        new_dir = PROJECTS_DIR / new_encoded

        files_modified = 0
        dirs_renamed = 0
        target_dir: Path | None = None

        if old_dir.is_dir():
            if new_dir.exists():
                return MoveReport(
                    provider=Provider.CLAUDE,
                    success=False,
                    error=f"Target Claude project directory already exists: {new_dir}",
                )
            try:
                old_dir.rename(new_dir)
                dirs_renamed = 1
            except OSError as exc:
                return MoveReport(
                    provider=Provider.CLAUDE,
                    success=False,
                    error=f"Failed to rename Claude project directory: {exc}",
                )
            target_dir = new_dir
        elif new_dir.is_dir():
            target_dir = new_dir
        else:
            return MoveReport(provider=Provider.CLAUDE, success=True)

        try:
            for jsonl_file in target_dir.glob("*.jsonl"):
                if jsonl_file.name.startswith("agent-"):
                    continue
                if _rewrite_cwd_in_jsonl(jsonl_file, old_path, new_path):
                    files_modified += 1
        except OSError as exc:
            return MoveReport(
                provider=Provider.CLAUDE,
                success=False,
                files_modified=files_modified,
                dirs_renamed=dirs_renamed,
                error=f"Failed updating Claude JSONL metadata: {exc}",
            )

        self._path_to_dir.pop(old_path, None)
        self._path_to_dir[new_path] = target_dir

        return MoveReport(
            provider=Provider.CLAUDE,
            success=True,
            files_modified=files_modified,
            dirs_renamed=dirs_renamed,
        )

    def _find_project_dir(self, project_path: str) -> Path | None:
        """Find the Claude project directory for a given project path."""
        if project_path in self._path_to_dir:
            return self._path_to_dir[project_path]

        if not PROJECTS_DIR.is_dir():
            return None

        for entry in PROJECTS_DIR.iterdir():
            if not entry.is_dir():
                continue
            resolved = _extract_project_path(entry.name, entry)
            if resolved == project_path:
                self._path_to_dir[project_path] = entry
                return entry

        return None

    def _parse_sessions(self, project_dir: Path, project_path: str) -> list[SessionMeta]:
        """Parse JSONL files in a project directory to extract sessions."""
        sessions: dict[str, dict] = {}  # session_id -> session data
        first_user_msgs: dict[str, str] = {}  # session_id -> first user msg uuid
        summaries: dict[str, str] = {}  # leafUuid -> summary text
        pending_summaries: dict[str, str] = {}  # leafUuid -> summary (no sessionId)

        jsonl_files = sorted(project_dir.glob("*.jsonl"))

        for jsonl_file in jsonl_files:
            if jsonl_file.name.startswith("agent-"):
                continue
            try:
                with open(jsonl_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        entry_type = entry.get("type", "")

                        # Handle summary entries without sessionId
                        if entry_type == "summary" and entry.get("summary") and not entry.get("sessionId"):
                            leaf = entry.get("leafUuid")
                            if leaf:
                                pending_summaries[leaf] = entry["summary"]
                            continue

                        session_id = entry.get("sessionId")
                        if not session_id:
                            continue

                        if session_id not in sessions:
                            sessions[session_id] = {
                                "id": session_id,
                                "summary": None,
                                "timestamp": None,
                                "message_count": 0,
                                "model": None,
                                "last_user_message": None,
                                "cwd": entry.get("cwd"),
                            }

                        s = sessions[session_id]

                        # Update timestamp
                        ts = entry.get("timestamp")
                        if ts:
                            parsed = _parse_timestamp(ts)
                            if s["timestamp"] is None or parsed > s["timestamp"]:
                                s["timestamp"] = parsed

                        # Apply pending summary
                        parent_uuid = entry.get("parentUuid")
                        if parent_uuid and parent_uuid in pending_summaries and not s["summary"]:
                            s["summary"] = pending_summaries[parent_uuid]

                        # Summary entry with sessionId
                        if entry_type == "summary" and entry.get("summary"):
                            s["summary"] = entry["summary"]

                        msg = entry.get("message")
                        if not msg:
                            continue

                        role = msg.get("role")
                        s["message_count"] += 1

                        # Extract model from assistant messages
                        if role == "assistant" and msg.get("model"):
                            s["model"] = msg["model"]

                        # Track first user message (parentUuid is null)
                        if role == "user" and entry.get("parentUuid") is None and entry.get("uuid"):
                            first_user_msgs[session_id] = entry["uuid"]

                        # Track last non-system user message for summary fallback
                        if role == "user":
                            text = _extract_text(msg.get("content", ""))
                            if text and not _is_system_message(text):
                                s["last_user_message"] = text

            except OSError:
                continue

        # Group sessions by first user message UUID
        uuid_to_sessions: dict[str, list[str]] = defaultdict(list)
        ungrouped = []
        for sid, uuid in first_user_msgs.items():
            uuid_to_sessions[uuid].append(sid)
        grouped_ids = set()
        for uuid, sids in uuid_to_sessions.items():
            grouped_ids.update(sids)

        # Pick latest session from each group
        result_ids = set()
        for uuid, sids in uuid_to_sessions.items():
            best = max(sids, key=lambda sid: sessions[sid]["timestamp"] or datetime.min.replace(tzinfo=timezone.utc))
            result_ids.add(best)

        # Add ungrouped sessions
        for sid in sessions:
            if sid not in grouped_ids:
                result_ids.add(sid)

        # Build SessionMeta list
        result = []
        for sid in result_ids:
            s = sessions[sid]
            summary = s["summary"]
            if not summary:
                if s["last_user_message"]:
                    text = s["last_user_message"]
                    summary = text[:80] + "..." if len(text) > 80 else text
                else:
                    summary = "New Session"

            # Skip sessions that look like JSON blobs
            if summary.startswith('{ "'):
                continue

            ts = s["timestamp"] or datetime.now(tz=timezone.utc)

            result.append(SessionMeta(
                id=sid,
                project_path=project_path,
                provider=Provider.CLAUDE,
                summary=summary,
                timestamp=ts,
                message_count=s["message_count"],
                model=s["model"],
                source_path=str(project_dir),
            ))

        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result
