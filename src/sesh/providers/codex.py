"""Codex CLI session provider."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, MoveReport, Provider, SessionMeta, SubagentMeta
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


def _active_dialogue_metadata(file_path: Path) -> tuple[int, str | None]:
    """Replay Codex turn boundaries and return active dialogue metadata.

    Turn bookkeeping (kept identical to the cut logic in
    ``_parse_rollout_file`` so the metadata count and the actual transcript
    always agree):

    * ``task_started`` closes any in-progress turn -- flushing it as a
      completed turn rather than discarding its dialogue -- and opens a
      fresh one. This preserves aborted turns and nested ``task_started``
      sequences.
    * ``task_complete`` closes the in-progress turn.
    * Any dialogue record with no turn open starts an implicit turn, so
      dialogue outside an explicit ``task_started``/``task_complete`` pair
      (before the first turn, or between turns) counts as its own turn
      instead of being thrown away.
    * ``thread_rolled_back`` first closes any in-progress (aborted) turn --
      so it counts as one of the removed turns -- then removes the last N
      completed turns.

    "Dialogue" means a record the transcript actually renders (non-empty
    user_message text, non-blank assistant text) — the same conditions
    ``_parse_rollout_file`` uses — so with no rollback the replayed count
    equals the rendered transcript's dialogue count.
    """
    turns: list[tuple[int, str | None]] = []
    current: tuple[int, str | None] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            turns.append(current)
            current = None

    try:
        with open(file_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(entry, dict):
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                event_type = payload.get("type") if entry.get("type") == "event_msg" else None
                if event_type == "task_started":
                    flush()
                    current = (0, None)
                    continue
                if event_type == "task_complete":
                    flush()
                    continue
                if event_type == "thread_rolled_back":
                    flush()
                    count = payload.get("num_turns")
                    if isinstance(count, int) and count > 0 and turns:
                        remove = min(count, len(turns))
                        del turns[-remove:]
                    continue

                # Dialogue detection mirrors _parse_rollout_file's render
                # conditions exactly: a record that renders nothing (empty
                # user_message, blank assistant text) must not open or extend
                # a turn here either, or a later thread_rolled_back would cut
                # different turns in the two replays and message_count/summary
                # would disagree with the rendered transcript.
                text: str | None = None
                is_dialogue = False
                if event_type == "user_message":
                    text = payload.get("message", "")
                    is_dialogue = bool(text)
                elif entry.get("type") == "response_item" and payload.get("role") == "assistant":
                    content = payload.get("content", [])
                    rendered = (
                        _extract_text_from_content(content)
                        if isinstance(content, list)
                        else str(content)
                    )
                    is_dialogue = bool(rendered.strip())
                if not is_dialogue:
                    continue
                if current is None:
                    current = (0, None)
                count, first = current
                current = (count + 1, first or text)
    except OSError:
        return 0, None

    flush()
    count = sum(turn[0] for turn in turns)
    first = next((turn[1] for turn in turns if turn[1]), None)
    return count, first


class CodexProvider(SessionProvider):
    """Provider for OpenAI Codex CLI sessions."""

    def __init__(
        self,
        cache=None,
        base_dir: Path | None = None,
        host: str | None = None,
    ) -> None:
        self._index: dict[str, list[dict]] | None = None
        self._subagents_by_root: dict[str, list[dict]] = {}
        self._cache = cache
        self._base_dir = base_dir
        self.host = host

    @property
    def _codex_dir(self) -> Path:
        return CODEX_DIR if self._base_dir is None else self._base_dir / ".codex" / "sessions"

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each Codex project."""
        if not self._codex_dir.is_dir():
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
                start_timestamp=s.get("start_timestamp"),
                message_count=s.get("message_count", 0),
                model=s.get("model"),
                source_path=s.get("file_path"),
                input_tokens=s.get("input_tokens"),
                output_tokens=s.get("output_tokens"),
                cumulative_input_tokens=s.get("cumulative_input_tokens"),
                host=self.host,
                subagent_count=s.get("subagent_count", 0),
            ))

        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages from a Codex session file."""
        if not session.source_path:
            return []
        return self._get_messages_from_file(Path(session.source_path))

    def _get_messages_from_file(
        self, file_path: Path, *, child_agent_path: str | None = None
    ) -> list[Message]:
        """Return messages parsed from one rollout."""
        messages, _ = self._parse_rollout_file(
            file_path, child_agent_path=child_agent_path
        )
        return messages

    def _parse_rollout_file(
        self, file_path: Path, *, child_agent_path: str | None = None
    ) -> tuple[list[Message], int | None]:
        """Parse one rollout, optionally suppressing a child's forked history."""
        if not file_path.is_file():
            return [], None

        messages: list[Message] = []
        completed_turn_starts: list[int] = []
        current_turn_start: int | None = None
        output_tokens: int | None = None
        # A subagent rollout begins with a physical copy of the parent's context.
        # Its plaintext NEW_TASK handoff is the first child-owned record.
        child_started = child_agent_path is None
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
                    if not isinstance(entry, dict):
                        continue

                    entry_type = entry.get("type", "")
                    payload = entry.get("payload", {})
                    if not isinstance(payload, dict):
                        payload = {}
                    ts = _parse_timestamp(entry.get("timestamp", ""))
                    event_type = payload.get("type") if entry_type == "event_msg" else None

                    if child_started and event_type == "task_started":
                        # A task_started arriving while a turn is still open
                        # closes it (flushed as a completed turn) instead of
                        # abandoning the in-progress records, then opens a fresh
                        # turn. Mirrors _active_dialogue_metadata so the cut
                        # points and the metadata count stay in agreement.
                        if current_turn_start is not None:
                            completed_turn_starts.append(current_turn_start)
                        current_turn_start = len(messages)
                    elif child_started and event_type == "task_complete":
                        if current_turn_start is not None:
                            completed_turn_starts.append(current_turn_start)
                            current_turn_start = None
                    elif child_started and event_type == "thread_rolled_back":
                        # Flush any in-progress (aborted) turn so it counts as
                        # one of the removed turns, then drop the last N turns.
                        # Reset current_turn_start unconditionally so a rollback
                        # with no completed turns still clears in-progress state.
                        if current_turn_start is not None:
                            completed_turn_starts.append(current_turn_start)
                        count = payload.get("num_turns")
                        if isinstance(count, int) and count > 0 and completed_turn_starts:
                            remove = min(count, len(completed_turn_starts))
                            cut = completed_turn_starts[-remove]
                            del messages[cut:]
                            del completed_turn_starts[-remove:]
                        current_turn_start = None
                        continue

                    if not child_started:
                        content = payload.get("content")
                        if (
                            entry_type == "response_item"
                            and payload.get("type") == "agent_message"
                            and payload.get("recipient") == child_agent_path
                            and isinstance(content, list)
                            and any(
                                isinstance(item, dict)
                                and str(item.get("text", "")).startswith("Message Type: NEW_TASK")
                                for item in content
                            )
                        ):
                            child_started = True
                        continue

                    # Token totals are metadata rather than transcript messages.
                    # Track them during this same lazy pass so loading a selected
                    # child does not need to scan its rollout a second time.
                    if entry_type == "event_msg" and payload.get("type") == "token_count":
                        info = payload.get("info") or {}
                        total_usage = info.get("total_token_usage") or {}
                        last_usage = info.get("last_token_usage") or {}
                        value = total_usage.get("output_tokens")
                        if value is None:
                            value = last_usage.get("output_tokens")
                        if isinstance(value, int):
                            output_tokens = value

                    # User messages
                    if entry_type == "event_msg" and payload.get("type") == "user_message":
                        text = payload.get("message", "")
                        if text:
                            # Dialogue outside an explicit turn opens an implicit
                            # one (see _active_dialogue_metadata) so out-of-turn
                            # records are attributed to a removable turn.
                            if current_turn_start is None:
                                current_turn_start = len(messages)
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

                    # Function calls (standard and current custom-tool schema)
                    elif entry_type == "response_item" and payload.get("type") in (
                        "function_call", "custom_tool_call"
                    ):
                        name = payload.get("name", "")
                        call_id = payload.get("call_id", "")
                        if call_id and name:
                            call_id_map[call_id] = name
                        args = _stringify_tool_value(
                            payload.get("arguments", payload.get("input", ""))
                        )
                        messages.append(Message(
                            role="assistant",
                            content="",
                            timestamp=ts,
                            tool_name=name,
                            tool_input=args,
                            content_type="tool_use",
                        ))

                    # Function call output (standard and current custom-tool schema)
                    elif entry_type == "response_item" and payload.get("type") in (
                        "function_call_output", "custom_tool_call_output"
                    ):
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
                            if current_turn_start is None:
                                current_turn_start = len(messages)
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

        return messages, output_tokens

    def load_subagents(
        self, session: SessionMeta
    ) -> list[tuple[SubagentMeta, list[Message]]]:
        """Load native Codex child rollouts attached to a root thread."""
        self._build_index()
        loaded: list[tuple[SubagentMeta, list[Message]]] = []
        for child in self._subagents_by_root.get(session.id, []):
            file_path = Path(child["file_path"])
            messages, output_tokens = self._parse_rollout_file(
                file_path, child_agent_path=child.get("agent_path")
            )
            meta = SubagentMeta(
                agent_id=child["id"],
                file_path=str(file_path),
                description=child.get("agent_path"),
                agent_type=child.get("agent_role") or child.get("agent_nickname"),
                is_fork=True,
                first_timestamp=child.get("start_timestamp"),
                message_count=len([m for m in messages if m.role in ("user", "assistant")]),
                output_tokens=output_tokens,
            )
            loaded.append((meta, messages))
        loaded.sort(
            key=lambda pair: pair[0].first_timestamp
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return loaded

    def discover_subagents(self, session: SessionMeta) -> list[SubagentMeta]:
        return [meta for meta, _ in self.load_subagents(session)]

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a Codex root rollout and its native child rollouts."""
        self._build_index()
        for child in self._subagents_by_root.get(session.id, []):
            Path(child["file_path"]).unlink(missing_ok=True)
        if session.source_path:
            Path(session.source_path).unlink(missing_ok=True)
        self._index = None
        self._subagents_by_root = {}

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Update Codex metadata when a project path changes."""
        codex_dir = self._codex_dir
        if not codex_dir.is_dir():
            return MoveReport(provider=Provider.CODEX, success=True)

        files_modified = 0
        try:
            for jsonl_file in codex_dir.rglob("*.jsonl"):
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
        self._subagents_by_root = {}
        return MoveReport(
            provider=Provider.CODEX,
            success=True,
            files_modified=files_modified,
        )

    def _build_index(self) -> dict[str, list[dict]]:
        """Build the root-session index and native Codex child-thread map."""
        if self._index is not None:
            return self._index

        self._index = {}
        self._subagents_by_root = {}
        codex_dir = self._codex_dir
        if not codex_dir.is_dir():
            return self._index

        roots: list[Path] = []
        for jsonl_file in codex_dir.rglob("*.jsonl"):
            header = self._read_session_header(jsonl_file)
            if header and self._is_subagent_header(header):
                root_id = header.get("session_id") or header.get("parent_thread_id")
                child_id = header.get("id")
                if child_id and root_id:
                    source = header.get("source")
                    subagent_source = source.get("subagent") if isinstance(source, dict) else {}
                    spawn = (subagent_source or {}).get("thread_spawn") or {}
                    child = {
                        "id": child_id,
                        "file_path": str(jsonl_file),
                        "start_timestamp": (
                            _parse_timestamp(header["_timestamp"])
                            if header.get("_timestamp") else None
                        ),
                        "parent_thread_id": header.get("parent_thread_id")
                        or spawn.get("parent_thread_id"),
                        "agent_path": header.get("agent_path") or spawn.get("agent_path"),
                        "agent_nickname": header.get("agent_nickname")
                        or spawn.get("agent_nickname"),
                        "agent_role": spawn.get("agent_role"),
                    }
                    self._subagents_by_root.setdefault(str(root_id), []).append(child)
                continue
            roots.append(jsonl_file)

        cache = self._cache
        for jsonl_file in roots:
            file_str = str(jsonl_file)
            data = None
            cached_count: int | None = None
            if cache:
                cached = cache.get_sessions(file_str)
                if cached:
                    s = cached[0]
                    cached_count = s.subagent_count
                    data = {
                        "id": s.id,
                        "cwd": s.project_path,
                        "model": s.model,
                        "timestamp": s.timestamp,
                        "start_timestamp": s.start_timestamp,
                        "summary": s.summary,
                        "message_count": s.message_count,
                        "file_path": s.source_path or file_str,
                        "input_tokens": s.input_tokens,
                        "output_tokens": s.output_tokens,
                        "cumulative_input_tokens": s.cumulative_input_tokens,
                    }
            if data is None:
                data = self._parse_session_file(jsonl_file)
            if not data or not data.get("cwd"):
                continue

            data["subagent_count"] = len(self._subagents_by_root.get(data["id"], []))
            self._index.setdefault(data["cwd"], []).append(data)
            if cache and cached_count != data["subagent_count"]:
                cache.put_sessions(file_str, [SessionMeta(
                    id=data["id"],
                    project_path=data["cwd"],
                    provider=Provider.CODEX,
                    summary=data.get("summary", ""),
                    timestamp=data["timestamp"],
                    start_timestamp=data.get("start_timestamp"),
                    message_count=data.get("message_count", 0),
                    model=data.get("model"),
                    source_path=data.get("file_path"),
                    input_tokens=data.get("input_tokens"),
                    output_tokens=data.get("output_tokens"),
                    cumulative_input_tokens=data.get("cumulative_input_tokens"),
                    subagent_count=data["subagent_count"],
                )])

        return self._index

    @staticmethod
    def _read_session_header(file_path: Path) -> dict | None:
        """Read only a rollout's first-line session metadata payload."""
        try:
            with open(file_path) as f:
                entry = json.loads(f.readline())
            if entry.get("type") == "session_meta" and isinstance(entry.get("payload"), dict):
                payload = entry["payload"].copy()
                payload["_timestamp"] = entry.get("timestamp")
                return payload
        except (OSError, json.JSONDecodeError):
            pass
        return None

    @staticmethod
    def _is_subagent_header(payload: dict) -> bool:
        source = payload.get("source")
        return payload.get("thread_source") == "subagent" or (
            isinstance(source, dict) and isinstance(source.get("subagent"), dict)
        )

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
                    # Current Codex files put the provider (for example,
                    # "openai") here and the actual model in turn_context.
                    model = payload.get("model", "")
                    first_ts = first_entry.get("timestamp")

                    # Scan rest for last timestamp and user messages
                    last_ts = first_ts
                    first_user_msg = None
                    msg_count = 0
                    last_input_tokens = None
                    last_output_tokens = None
                    cumul_input_tokens = None

                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if not isinstance(entry, dict):
                                continue
                            if entry.get("timestamp"):
                                last_ts = entry["timestamp"]

                            etype = entry.get("type", "")
                            epayload = entry.get("payload")
                            if not isinstance(epayload, dict):
                                epayload = {}

                            if etype == "turn_context" and epayload.get("model"):
                                model = epayload["model"]

                            if etype == "event_msg" and epayload.get("type") == "user_message":
                                msg_count += 1
                                if first_user_msg is None:
                                    first_user_msg = epayload.get("message", "")
                            elif etype == "response_item" and epayload.get("role") == "assistant":
                                msg_count += 1
                            elif etype == "event_msg" and epayload.get("type") == "token_count":
                                info = epayload.get("info") or {}
                                last_usage = info.get("last_token_usage") or {}
                                if last_usage:
                                    last_input_tokens = last_usage.get("input_tokens")
                                    last_output_tokens = last_usage.get("output_tokens")
                                total_usage = info.get("total_token_usage") or {}
                                if total_usage:
                                    cumul_input_tokens = total_usage.get("input_tokens")
                                    last_output_tokens = total_usage.get("output_tokens")
                        except json.JSONDecodeError:
                            continue

                    # A cheap second streaming pass replays turn boundaries;
                    # it does not invoke lazy transcript/sub-agent loading.
                    msg_count, first_active_user = _active_dialogue_metadata(file_path)
                    first_active_user = first_active_user or first_user_msg
                    summary = "Codex Session"
                    if first_active_user:
                        summary = first_active_user[:80] + ("..." if len(first_active_user) > 80 else "")

                    return {
                        "id": session_id,
                        "cwd": cwd,
                        "model": model,
                        "timestamp": _parse_timestamp(last_ts),
                        "start_timestamp": _parse_timestamp(first_ts),
                        "summary": summary,
                        "message_count": msg_count,
                        "file_path": str(file_path),
                        "input_tokens": last_input_tokens,
                        "output_tokens": last_output_tokens,
                        "cumulative_input_tokens": cumul_input_tokens,
                    }

                # Legacy format: no session_meta, extract cwd from environment_context XML
                else:
                    cwd = ""
                    session_id = file_path.stem
                    first_ts = first_entry.get("timestamp")
                    last_ts = first_ts
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
                            payload = entry.get("payload") or {}
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
                        "start_timestamp": _parse_timestamp(first_ts),
                        "summary": summary,
                        "message_count": msg_count,
                        "file_path": str(file_path),
                    }

        except (json.JSONDecodeError, OSError):
            return None
