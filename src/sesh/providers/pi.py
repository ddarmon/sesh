"""pi CLI session provider.

pi (the Anthropic-adjacent CLI coding assistant; ``pi --help``) stores
each session as one JSONL file under
``~/.pi/agent/sessions/{encoded-cwd}/{ISO-timestamp}_{uuid}.jsonl``. The
encoded directory name wraps the cwd with a leading and trailing ``--``
(e.g. ``/Users/me/proj`` -> ``--Users-me-proj--``).

This provider is a hybrid of Claude (per-project encoded dirs, recover
the real cwd from JSONL headers) and Codex (one file per session, so
delete is a single ``unlink`` and indexing is per-file).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, MoveReport, Provider, SessionMeta
from sesh.providers import SessionProvider
from sesh.providers.claude import SYSTEM_PREFIXES, _is_system_message

PI_DIR = Path.home() / ".pi" / "agent"
SESSIONS_DIR = PI_DIR / "sessions"


def _parse_timestamp(ts) -> datetime:
    """Parse a pi timestamp (ISO string or epoch millis)."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    if isinstance(ts, str):
        s = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def encode_pi_path(path: str) -> str:
    """Encode a filesystem path the way pi does for ``~/.pi/agent/sessions/``.

    Wraps the path with a leading and trailing ``--``::

        /Users/me/My Project  ->  --Users-me-My-Project--
    """
    inner = path.lstrip("/").replace("/", "-").replace(" ", "-")
    return f"--{inner}--"


def _read_first_jsonl_entry(file_path: Path) -> dict | None:
    """Return the parsed first JSON object in a JSONL file, or None."""
    try:
        with open(file_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    return None
    except OSError:
        return None
    return None


def _extract_project_path(project_dir: Path) -> str | None:
    """Return the cwd recorded in the first session JSONL header, if any."""
    if not project_dir.is_dir():
        return None
    for jsonl_file in sorted(project_dir.glob("*.jsonl")):
        first = _read_first_jsonl_entry(jsonl_file)
        if first and isinstance(first, dict):
            cwd = first.get("cwd")
            if cwd:
                return cwd
    return None


def _display_name_from_path(project_path: str) -> str:
    return Path(project_path).name or project_path


def _rewrite_cwd_in_pi_jsonl(jsonl_file: Path, old_path: str, new_path: str) -> bool:
    """Rewrite the ``cwd`` field on the first ``type:session`` line.

    pi only stores cwd in the first session header, so we only need to
    touch line 1. Returns True if the file was modified.
    """
    output: list[str] = []
    modified = False
    rewritten_first = False

    try:
        with open(jsonl_file) as f:
            for line in f:
                stripped = line.strip()
                if rewritten_first or not stripped:
                    output.append(line)
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    output.append(line)
                    rewritten_first = True
                    continue

                if (
                    isinstance(entry, dict)
                    and entry.get("type") == "session"
                    and entry.get("cwd") == old_path
                ):
                    entry["cwd"] = new_path
                    line = json.dumps(entry) + "\n"
                    modified = True
                output.append(line)
                rewritten_first = True
    except OSError:
        return False

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


class PiProvider(SessionProvider):
    """Provider for pi CLI sessions."""

    def __init__(self, cache=None) -> None:
        self._path_to_dir: dict[str, Path] = {}
        self._cache = cache

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each pi project."""
        if not SESSIONS_DIR.is_dir():
            return

        for entry in sorted(SESSIONS_DIR.iterdir()):
            if not entry.is_dir():
                continue
            project_path = _extract_project_path(entry)
            if not project_path:
                continue
            self._path_to_dir[project_path] = entry
            yield project_path, _display_name_from_path(project_path)

    def get_sessions(self, project_path: str, cache=None) -> list[SessionMeta]:
        """Return sessions for a pi project (one session per file)."""
        project_dir = self._find_project_dir(project_path)
        if not project_dir:
            return []

        active_cache = cache if cache is not None else self._cache

        result: list[SessionMeta] = []
        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            file_str = str(jsonl_file)

            if active_cache:
                cached = active_cache.get_sessions(file_str)
                if cached:
                    result.extend(cached)
                    continue

            data = self._parse_session_file(jsonl_file, project_path)
            if not data:
                continue
            session = SessionMeta(
                id=data["id"],
                project_path=project_path,
                provider=Provider.PI,
                summary=data.get("summary", "Pi Session"),
                timestamp=data["timestamp"],
                start_timestamp=data.get("start_timestamp"),
                message_count=data.get("message_count", 0),
                model=data.get("model"),
                source_path=file_str,
                input_tokens=data.get("input_tokens"),
                output_tokens=data.get("output_tokens"),
                cumulative_input_tokens=data.get("cumulative_input_tokens"),
            )
            result.append(session)
            if active_cache:
                active_cache.put_sessions(file_str, [session])

        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages from a pi session JSONL file."""
        if not session.source_path:
            return []
        file_path = Path(session.source_path)
        if not file_path.is_file():
            return []

        messages: list[Message] = []
        tool_id_map: dict[str, str] = {}

        try:
            with open(file_path) as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entry = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue

                    if entry.get("type") != "message":
                        continue

                    msg = entry.get("message")
                    if not isinstance(msg, dict):
                        continue

                    role = msg.get("role", "")
                    ts = _parse_timestamp(
                        msg.get("timestamp") or entry.get("timestamp")
                    )
                    raw_content = msg.get("content", "")

                    # pi uses a top-level "toolResult" role with toolName/toolCallId
                    # at the message level (rather than tool_result content blocks
                    # inside a user message, which is Claude's convention).
                    if role == "toolResult":
                        tool_id = msg.get("toolCallId", "")
                        resolved_name = msg.get("toolName") or tool_id_map.get(tool_id, "")
                        result_str = _flatten_text_content(raw_content)
                        messages.append(Message(
                            role="tool",
                            content="",
                            timestamp=ts,
                            tool_name=resolved_name,
                            tool_output=result_str,
                            content_type="tool_result",
                        ))
                        continue

                    if isinstance(raw_content, str):
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
                        continue

                    if not isinstance(raw_content, list):
                        continue

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
                            thinking_text = block.get("thinking") or block.get("text", "")
                            if not thinking_text.strip():
                                continue
                            messages.append(Message(
                                role="assistant",
                                content="",
                                timestamp=ts,
                                thinking=thinking_text,
                                content_type="thinking",
                            ))

                        # pi: "toolCall" block; Claude-style "tool_use" kept for safety
                        elif btype in ("toolCall", "tool_use"):
                            name = block.get("name", "")
                            tool_id = block.get("id", "")
                            if tool_id and name:
                                tool_id_map[tool_id] = name
                            inp = block.get("arguments", block.get("input", {}))
                            messages.append(Message(
                                role="assistant",
                                content="",
                                timestamp=ts,
                                tool_name=name,
                                tool_input=json.dumps(inp, indent=2),
                                content_type="tool_use",
                            ))

                        # Claude-style inline tool_result block (pi normally uses
                        # the toolResult role above, but handle this defensively).
                        elif btype == "tool_result":
                            tool_id = block.get("tool_use_id", "")
                            resolved_name = tool_id_map.get(tool_id, "")
                            result_str = _flatten_text_content(block.get("content", ""))
                            messages.append(Message(
                                role="tool",
                                content="",
                                timestamp=ts,
                                tool_name=resolved_name,
                                tool_output=result_str,
                                content_type="tool_result",
                            ))
        except OSError:
            return messages

        messages.sort(key=lambda m: m.timestamp or datetime.min.replace(tzinfo=timezone.utc))
        return messages

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a pi session by removing its JSONL file."""
        if session.source_path:
            Path(session.source_path).unlink(missing_ok=True)

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Rename the encoded dir and rewrite cwd in each session header."""
        if not SESSIONS_DIR.is_dir():
            return MoveReport(provider=Provider.PI, success=True)

        old_dir = SESSIONS_DIR / encode_pi_path(old_path)
        new_dir = SESSIONS_DIR / encode_pi_path(new_path)

        files_modified = 0
        dirs_renamed = 0
        target_dir: Path | None = None

        if old_dir.is_dir():
            if new_dir.exists():
                return MoveReport(
                    provider=Provider.PI,
                    success=False,
                    error=f"Target pi project directory already exists: {new_dir}",
                )
            try:
                old_dir.rename(new_dir)
                dirs_renamed = 1
            except OSError as exc:
                return MoveReport(
                    provider=Provider.PI,
                    success=False,
                    error=f"Failed to rename pi project directory: {exc}",
                )
            target_dir = new_dir
        elif new_dir.is_dir():
            target_dir = new_dir
        else:
            return MoveReport(provider=Provider.PI, success=True)

        try:
            for jsonl_file in target_dir.glob("*.jsonl"):
                if _rewrite_cwd_in_pi_jsonl(jsonl_file, old_path, new_path):
                    files_modified += 1
        except OSError as exc:
            return MoveReport(
                provider=Provider.PI,
                success=False,
                files_modified=files_modified,
                dirs_renamed=dirs_renamed,
                error=f"Failed updating pi JSONL metadata: {exc}",
            )

        self._path_to_dir.pop(old_path, None)
        self._path_to_dir[new_path] = target_dir

        return MoveReport(
            provider=Provider.PI,
            success=True,
            files_modified=files_modified,
            dirs_renamed=dirs_renamed,
        )

    def _find_project_dir(self, project_path: str) -> Path | None:
        if project_path in self._path_to_dir:
            cached = self._path_to_dir[project_path]
            if cached.is_dir():
                return cached
            self._path_to_dir.pop(project_path, None)

        if not SESSIONS_DIR.is_dir():
            return None

        # Try the canonical encoding first (cheap path).
        encoded = SESSIONS_DIR / encode_pi_path(project_path)
        if encoded.is_dir():
            resolved = _extract_project_path(encoded)
            if resolved == project_path:
                self._path_to_dir[project_path] = encoded
                return encoded

        # Fallback: scan all dirs (handles paths whose `-` chars made
        # the encoding lossy / ambiguous).
        for entry in SESSIONS_DIR.iterdir():
            if not entry.is_dir():
                continue
            resolved = _extract_project_path(entry)
            if resolved == project_path:
                self._path_to_dir[project_path] = entry
                return entry

        return None

    def _parse_session_file(
        self, file_path: Path, project_path: str
    ) -> dict | None:
        """Extract metadata from a single pi JSONL file."""
        session_id: str | None = None
        cwd: str | None = None
        first_ts: datetime | None = None
        last_ts: datetime | None = None
        model: str | None = None
        first_user_msg: str | None = None
        message_count = 0
        last_input_tokens: int | None = None
        cumul_input_tokens = 0
        output_tokens = 0
        saw_usage = False

        try:
            with open(file_path) as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entry = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue

                    etype = entry.get("type")

                    if etype == "session":
                        session_id = entry.get("id") or session_id
                        cwd = entry.get("cwd") or cwd
                        ts = entry.get("timestamp")
                        if ts:
                            first_ts = first_ts or _parse_timestamp(ts)
                            last_ts = _parse_timestamp(ts)
                        continue

                    if etype != "message":
                        continue

                    msg = entry.get("message")
                    if not isinstance(msg, dict):
                        continue

                    ts = entry.get("timestamp") or msg.get("timestamp")
                    if ts:
                        parsed = _parse_timestamp(ts)
                        if first_ts is None or parsed < first_ts:
                            first_ts = parsed
                        if last_ts is None or parsed > last_ts:
                            last_ts = parsed

                    role = msg.get("role")
                    if role in ("user", "assistant"):
                        message_count += 1

                    if role == "user" and first_user_msg is None:
                        text = _first_text_block(msg.get("content"))
                        if text and not _is_system_message(text):
                            first_user_msg = text

                    if role == "assistant":
                        m = msg.get("model")
                        if m:
                            model = m
                        usage = msg.get("usage")
                        if isinstance(usage, dict):
                            saw_usage = True
                            turn_input = (
                                int(usage.get("input", 0) or 0)
                                + int(usage.get("cacheRead", 0) or 0)
                                + int(usage.get("cacheWrite", 0) or 0)
                            )
                            last_input_tokens = turn_input
                            cumul_input_tokens += turn_input
                            output_tokens += int(usage.get("output", 0) or 0)
        except OSError:
            return None

        if not session_id:
            session_id = file_path.stem.split("_", 1)[-1]
        if not cwd:
            cwd = project_path

        timestamp = last_ts or datetime.now(tz=timezone.utc)

        summary = "Pi Session"
        if first_user_msg:
            summary = first_user_msg[:80] + ("..." if len(first_user_msg) > 80 else "")

        return {
            "id": session_id,
            "cwd": cwd,
            "model": model,
            "timestamp": timestamp,
            "start_timestamp": first_ts,
            "summary": summary,
            "message_count": message_count,
            "input_tokens": last_input_tokens if saw_usage else None,
            "output_tokens": output_tokens if saw_usage else None,
            "cumulative_input_tokens": cumul_input_tokens if saw_usage else None,
        }


def _first_text_block(content) -> str:
    """Pull the first non-empty text out of a pi message content payload."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    return text
    return ""


def _flatten_text_content(content) -> str:
    """Concatenate the text inside a content list (or return a string as-is)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text", "")
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return str(content) if content else ""


__all__ = [
    "PI_DIR",
    "SESSIONS_DIR",
    "PiProvider",
    "encode_pi_path",
    "SYSTEM_PREFIXES",
]
