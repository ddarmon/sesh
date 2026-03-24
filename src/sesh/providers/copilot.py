"""GitHub Copilot CLI session provider."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, MoveReport, Provider, SessionMeta
from sesh.providers import SessionProvider

COPILOT_DIR = Path.home() / ".copilot" / "session-state"


def _parse_workspace_yaml(path: Path) -> dict[str, str]:
    """Parse a flat key-value workspace.yaml without PyYAML."""
    result: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                colon = line.find(":")
                if colon == -1:
                    continue
                key = line[:colon].strip()
                value = line[colon + 1 :].strip()
                # Strip surrounding quotes if present
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                result[key] = value
    except OSError:
        pass
    return result


def _parse_timestamp(ts) -> datetime:
    if isinstance(ts, str):
        ts_str = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(ts_str)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _stringify_tool_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2)
    except TypeError:
        return str(value)


class CopilotProvider(SessionProvider):
    """Provider for GitHub Copilot CLI sessions."""

    def __init__(self, cache=None) -> None:
        self._index: dict[str, list[dict]] | None = None
        self._cache = cache

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each Copilot project."""
        if not COPILOT_DIR.is_dir():
            return

        index = self._build_index()
        for project_path in sorted(index.keys()):
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
                provider=Provider.COPILOT,
                summary=s.get("summary", "Copilot Session"),
                timestamp=s["timestamp"],
                start_timestamp=s.get("start_timestamp"),
                message_count=s.get("message_count", 0),
                model=s.get("model"),
                source_path=s.get("session_dir"),
                input_tokens=s.get("input_tokens"),
                output_tokens=s.get("output_tokens"),
                cumulative_input_tokens=s.get("cumulative_input_tokens"),
            ))

        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages from a Copilot session's events.jsonl."""
        if not session.source_path:
            return []

        source = Path(session.source_path)
        # Accept either the session dir or events.jsonl directly
        if source.is_dir():
            events_file = source / "events.jsonl"
        else:
            events_file = source

        if not events_file.is_file():
            return []

        messages: list[Message] = []
        # Build toolCallId -> toolName map from assistant.message toolRequests
        tool_name_map: dict[str, str] = {}

        try:
            with open(events_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type", "")
                    data = event.get("data", {})
                    ts = _parse_timestamp(event.get("timestamp", ""))

                    if etype == "user.message":
                        content = data.get("content", "")
                        if content:
                            messages.append(Message(
                                role="user",
                                content=content,
                                timestamp=ts,
                            ))

                    elif etype == "assistant.message":
                        # Thinking (reasoning)
                        reasoning = data.get("reasoningText", "")
                        if reasoning:
                            messages.append(Message(
                                role="assistant",
                                content="",
                                timestamp=ts,
                                thinking=reasoning,
                                content_type="thinking",
                            ))

                        # Text content
                        content = data.get("content", "")
                        if content:
                            messages.append(Message(
                                role="assistant",
                                content=content,
                                timestamp=ts,
                            ))

                        # Tool requests
                        for req in data.get("toolRequests", []):
                            tool_call_id = req.get("toolCallId", "")
                            name = req.get("name", "")
                            if tool_call_id and name:
                                tool_name_map[tool_call_id] = name
                            args = _stringify_tool_value(req.get("arguments"))
                            messages.append(Message(
                                role="assistant",
                                content="",
                                timestamp=ts,
                                tool_name=name,
                                tool_input=args,
                                content_type="tool_use",
                            ))

                    elif etype == "tool.execution_complete":
                        tool_call_id = data.get("toolCallId", "")
                        resolved_name = tool_name_map.get(tool_call_id, "")
                        result = data.get("result", {})
                        output = ""
                        if isinstance(result, dict):
                            output = result.get("content", "") or ""
                        messages.append(Message(
                            role="tool",
                            content="",
                            timestamp=ts,
                            tool_name=resolved_name,
                            tool_output=output,
                            content_type="tool_result",
                        ))

                    # Skip: tool.execution_start, session.*, assistant.turn_*

        except OSError:
            pass

        return messages

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a Copilot session by removing its directory."""
        if session.source_path:
            shutil.rmtree(session.source_path, ignore_errors=True)

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Update Copilot metadata when a project path changes."""
        if not COPILOT_DIR.is_dir():
            return MoveReport(provider=Provider.COPILOT, success=True)

        files_modified = 0
        try:
            for session_dir in COPILOT_DIR.iterdir():
                if not session_dir.is_dir():
                    continue
                yaml_path = session_dir / "workspace.yaml"
                if not yaml_path.is_file():
                    continue
                meta = _parse_workspace_yaml(yaml_path)
                if meta.get("cwd") != old_path:
                    continue

                # Rewrite the workspace.yaml with updated cwd
                lines: list[str] = []
                with open(yaml_path) as f:
                    for ln in f:
                        stripped = ln.strip()
                        colon = stripped.find(":")
                        if colon != -1 and stripped[:colon].strip() == "cwd":
                            lines.append(f"cwd: {new_path}\n")
                        else:
                            lines.append(ln)

                fd, tmp = tempfile.mkstemp(
                    dir=str(session_dir), suffix=".yaml.tmp"
                )
                try:
                    with os.fdopen(fd, "w") as f:
                        f.writelines(lines)
                    os.replace(tmp, str(yaml_path))
                    files_modified += 1
                except BaseException:
                    os.unlink(tmp)
                    raise

        except OSError as exc:
            return MoveReport(
                provider=Provider.COPILOT,
                success=False,
                files_modified=files_modified,
                error=f"Failed updating Copilot session metadata: {exc}",
            )

        self._index = None
        return MoveReport(
            provider=Provider.COPILOT,
            success=True,
            files_modified=files_modified,
        )

    def _build_index(self) -> dict[str, list[dict]]:
        """Build index of project_path -> [{session data}]."""
        if self._index is not None:
            return self._index

        self._index = {}
        if not COPILOT_DIR.is_dir():
            return self._index

        cache = self._cache

        for session_dir in COPILOT_DIR.iterdir():
            if not session_dir.is_dir():
                continue

            dir_str = str(session_dir)

            # Check cache first
            if cache:
                cached_sessions = cache.get_sessions(dir_str)
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
                            "start_timestamp": s.start_timestamp,
                            "summary": s.summary,
                            "message_count": s.message_count,
                            "session_dir": s.source_path or dir_str,
                            "input_tokens": s.input_tokens,
                            "output_tokens": s.output_tokens,
                            "cumulative_input_tokens": s.cumulative_input_tokens,
                        })
                    continue

            data = self._parse_session_dir(session_dir)
            if data and data.get("cwd"):
                cwd = data["cwd"]
                if cwd not in self._index:
                    self._index[cwd] = []
                self._index[cwd].append(data)

                # Store in cache
                if cache:
                    cache.put_sessions(dir_str, [SessionMeta(
                        id=data["id"],
                        project_path=data["cwd"],
                        provider=Provider.COPILOT,
                        summary=data.get("summary", ""),
                        timestamp=data["timestamp"],
                        start_timestamp=data.get("start_timestamp"),
                        message_count=data.get("message_count", 0),
                        model=data.get("model"),
                        source_path=data.get("session_dir"),
                        input_tokens=data.get("input_tokens"),
                        output_tokens=data.get("output_tokens"),
                        cumulative_input_tokens=data.get("cumulative_input_tokens"),
                    )])

        return self._index

    def _parse_session_dir(self, session_dir: Path) -> dict | None:
        """Parse a Copilot session directory to extract metadata."""
        yaml_path = session_dir / "workspace.yaml"
        meta = _parse_workspace_yaml(yaml_path)
        if not meta:
            return None

        cwd = meta.get("cwd", "")
        if not cwd:
            return None

        session_id = meta.get("id", session_dir.name)
        summary = meta.get("summary", "")
        created_at = meta.get("created_at", "")
        updated_at = meta.get("updated_at", "")

        # Quick scan of events.jsonl for model, message count, and fallback summary
        events_file = session_dir / "events.jsonl"
        model = ""
        msg_count = 0
        first_user_msg = None
        input_tokens = 0
        output_tokens = 0

        if events_file.is_file():
            try:
                with open(events_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type", "")
                        data = event.get("data", {})

                        if etype == "user.message":
                            msg_count += 1
                            if first_user_msg is None:
                                first_user_msg = data.get("content", "")
                        elif etype == "assistant.message":
                            msg_count += 1
                        elif etype == "session.model_change":
                            model = data.get("newModel", model)
                        elif etype == "session.shutdown":
                            model = data.get("currentModel", model) or model
                            for metrics in data.get("modelMetrics", {}).values():
                                usage = metrics.get("usage", {})
                                input_tokens += (
                                    usage.get("inputTokens", 0)
                                    + usage.get("cacheReadTokens", 0)
                                    + usage.get("cacheWriteTokens", 0)
                                )
                                output_tokens += usage.get("outputTokens", 0)
            except OSError:
                pass

        if not summary and first_user_msg:
            summary = first_user_msg[:80] + ("..." if len(first_user_msg) > 80 else "")
        if not summary:
            summary = "Copilot Session"

        return {
            "id": session_id,
            "cwd": cwd,
            "model": model or None,
            "timestamp": _parse_timestamp(updated_at),
            "start_timestamp": _parse_timestamp(created_at),
            "summary": summary,
            "message_count": msg_count,
            "session_dir": str(session_dir),
            "input_tokens": input_tokens or None,
            "output_tokens": output_tokens or None,
            "cumulative_input_tokens": input_tokens or None,
        }
