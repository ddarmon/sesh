"""Claude Code session provider."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import (
    Message,
    MoveReport,
    Provider,
    SessionMeta,
    SubagentMeta,
    encode_claude_path,
)
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


def load_loose_session(path: Path) -> tuple[SessionMeta, list[Message]]:
    """Build ``(SessionMeta, messages)`` from a single loose Claude ``.jsonl``.

    For archived or copied transcripts that live outside
    ``~/.claude/projects`` and therefore have no index entry. Assumes Claude
    Code JSONL format.

    The session id is read from the first ``sessionId`` seen *inside* the file
    (not the filename), so a renamed archive still resolves — this matches the
    ``sessionId`` filter in :meth:`ClaudeProvider.get_messages`. If the file
    holds more than one session, the first wins and the others' records are
    filtered out downstream.

    Raises ``ValueError`` when no parseable record carrying a ``sessionId`` is
    found (empty or non-Claude file).
    """
    session_id: str | None = None
    cwd_counts: dict[str, int] = {}
    min_ts: datetime | None = None
    max_ts: datetime | None = None
    model: str | None = None
    message_count = 0
    input_tokens = 0
    output_tokens = 0
    cumulative_input_tokens = 0
    summary: str | None = None
    last_user_message: str | None = None

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                sid = entry.get("sessionId")
                if sid and session_id is None:
                    session_id = sid
                # Only aggregate metadata for the primary session's records.
                if sid and sid != session_id:
                    continue

                cwd = entry.get("cwd")
                if cwd:
                    cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1

                ts = entry.get("timestamp")
                if ts:
                    parsed = _parse_timestamp(ts)
                    if min_ts is None or parsed < min_ts:
                        min_ts = parsed
                    if max_ts is None or parsed > max_ts:
                        max_ts = parsed

                if entry.get("type") == "summary" and entry.get("summary"):
                    summary = entry["summary"]

                msg = entry.get("message")
                if not msg:
                    continue

                role = msg.get("role")
                message_count += 1

                if role == "assistant":
                    if msg.get("model"):
                        model = msg["model"]
                    usage = msg.get("usage")
                    if usage:
                        turn_input = (
                            usage.get("input_tokens", 0)
                            + usage.get("cache_creation_input_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0)
                        )
                        input_tokens = turn_input
                        cumulative_input_tokens += turn_input
                        output_tokens += usage.get("output_tokens", 0)

                if role == "user":
                    text = _extract_text(msg.get("content", ""))
                    if text and not _is_system_message(text):
                        last_user_message = text
    except OSError as exc:
        raise ValueError(f"cannot read transcript file: {exc}") from exc

    if session_id is None:
        raise ValueError("no Claude transcript records with a sessionId found")

    if not summary:
        if last_user_message:
            summary = (
                last_user_message[:80] + "..."
                if len(last_user_message) > 80
                else last_user_message
            )
        else:
            summary = path.stem

    if cwd_counts:
        project_path = max(cwd_counts, key=cwd_counts.get)
    else:
        project_path = str(path.parent)

    timestamp = max_ts or datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    )

    meta = SessionMeta(
        id=session_id,
        project_path=project_path,
        provider=Provider.CLAUDE,
        summary=summary,
        timestamp=timestamp,
        start_timestamp=min_ts,
        message_count=message_count,
        model=model,
        source_path=str(path),
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
        cumulative_input_tokens=cumulative_input_tokens or None,
    )
    messages = ClaudeProvider().get_messages(meta)
    return meta, messages


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


def _agent_id_from_path(path: Path) -> str:
    """Extract the agent id from an ``agent-{id}.jsonl`` filename stem."""
    stem = path.stem
    return stem[len("agent-"):] if stem.startswith("agent-") else stem


def _read_agent_sidecar(agent_file: Path) -> dict | None:
    """Read the optional ``agent-{id}.meta.json`` sidecar next to an agent file.

    Returns the parsed ``{agentType, isFork, description, toolUseId}`` dict, or
    None when there is no sidecar (or it cannot be read/parsed).
    """
    sidecar = agent_file.parent / (agent_file.stem + ".meta.json")
    try:
        with open(sidecar) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _scan_agent_file(agent_file: Path) -> dict | None:
    """Scan an ``agent-*.jsonl`` file for aggregate metadata.

    Reads line-by-line, swallowing per-line JSON errors. Records without a
    ``message`` field (e.g. ``fork-context-ref``) don't count toward
    ``message_count`` but their ``sessionId`` is skipped only when absent.
    Returns None on OSError; otherwise a dict with the internal (parent)
    ``session_id``, ``message_count``, ``output_tokens``, earliest
    ``first_timestamp``, and the ``first_user_text`` for description fallback.
    """
    session_id: str | None = None
    message_count = 0
    output_tokens = 0
    first_ts: datetime | None = None
    first_user_text: str | None = None

    try:
        with open(agent_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                sid = entry.get("sessionId")
                if sid and session_id is None:
                    session_id = sid

                ts = entry.get("timestamp")
                if ts:
                    parsed = _parse_timestamp(ts)
                    if first_ts is None or parsed < first_ts:
                        first_ts = parsed

                msg = entry.get("message")
                if not msg:
                    continue
                message_count += 1

                role = msg.get("role")
                if role == "assistant":
                    usage = msg.get("usage")
                    if usage:
                        output_tokens += usage.get("output_tokens", 0)
                elif role == "user" and first_user_text is None:
                    text = _extract_text(msg.get("content", ""))
                    if text and not _is_system_message(text):
                        first_user_text = text
    except OSError:
        return None

    return {
        "session_id": session_id,
        "message_count": message_count,
        "output_tokens": output_tokens,
        "first_timestamp": first_ts,
        "first_user_text": first_user_text,
    }


def _build_subagent_meta(
    agent_file: Path,
    sidecar_data: dict | None,
    *,
    require_session_id: str | None = None,
) -> SubagentMeta | None:
    """Build a :class:`SubagentMeta` from an agent file and optional sidecar.

    When ``require_session_id`` is given (legacy layouts with no sidecar), the
    file is included only if its internal ``sessionId`` matches. Description
    falls back to the first user message text (first 80 chars) when the sidecar
    has none.
    """
    scan = _scan_agent_file(agent_file)
    if scan is None:
        return None
    if require_session_id is not None and scan["session_id"] != require_session_id:
        return None

    description = None
    agent_type = None
    is_fork = False
    tool_use_id = None
    if sidecar_data:
        description = sidecar_data.get("description")
        agent_type = sidecar_data.get("agentType")
        is_fork = bool(sidecar_data.get("isFork", False))
        tool_use_id = sidecar_data.get("toolUseId")

    if not description and scan["first_user_text"]:
        text = scan["first_user_text"]
        description = text[:80] + "..." if len(text) > 80 else text

    return SubagentMeta(
        agent_id=_agent_id_from_path(agent_file),
        file_path=str(agent_file),
        description=description,
        agent_type=agent_type,
        is_fork=is_fork,
        tool_use_id=tool_use_id,
        first_timestamp=scan["first_timestamp"],
        message_count=scan["message_count"],
        output_tokens=scan["output_tokens"] or None,
    )


class ClaudeProvider(SessionProvider):
    """Provider for Claude Code sessions."""

    def __init__(self, base_dir: Path | None = None, host: str | None = None) -> None:
        self._path_to_dir: dict[str, Path] = {}
        self._base_dir = base_dir
        self.host = host

    @property
    def _claude_dir(self) -> Path:
        """Resolve the .claude dir, deferring to the module constant in local mode."""
        return CLAUDE_DIR if self._base_dir is None else self._base_dir / ".claude"

    @property
    def _projects_dir(self) -> Path:
        return PROJECTS_DIR if self._base_dir is None else self._base_dir / ".claude" / "projects"

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each Claude project."""
        projects_dir = self._projects_dir
        if not projects_dir.is_dir():
            return

        from sesh.cache import load_project_paths, save_project_paths

        cached_paths = load_project_paths()
        updated = False

        for entry in sorted(projects_dir.iterdir()):
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

        # source_path points to the project directory; scan all JSONL files.
        # When it points straight at one file (loose/archived transcript), use
        # that file as-is — including explicitly passed ``agent-*.jsonl`` — and
        # only skip sidechain files when scanning a directory.
        if source_dir.is_dir():
            jsonl_files = [
                p
                for p in sorted(source_dir.glob("*.jsonl"))
                if not p.name.startswith("agent-")
            ]
        else:
            jsonl_files = [source_dir]

        for jsonl_file in jsonl_files:
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

    def discover_subagents(self, session: SessionMeta) -> list[SubagentMeta]:
        """Discover sub-agent transcripts for a session across all three layouts.

        Layout (a) — current: ``{project}/{id}/subagents/agent-*.jsonl`` with an
        optional ``agent-{id}.meta.json`` sidecar. Layouts (b) legacy
        ``{project}/subagents/agent-*.jsonl`` and (c) oldest
        ``{project}/agent-*.jsonl`` have no sidecar, so a file is included only
        when its internal (parent) ``sessionId`` matches this session.

        Discovery is lazy and swallows parse errors per file; results are sorted
        by ``first_timestamp``.
        """
        if not session.source_path:
            return []

        source = Path(session.source_path)
        results: list[SubagentMeta] = []

        # source_path is the project dir (indexed sessions) or a loose file.
        if source.is_dir():
            project_dir = source

            # (a) current layout: per-session subagents dir, sidecar honored.
            current = project_dir / session.id / "subagents"
            if current.is_dir():
                for agent_file in sorted(current.glob("agent-*.jsonl")):
                    meta = _build_subagent_meta(
                        agent_file, _read_agent_sidecar(agent_file)
                    )
                    if meta:
                        results.append(meta)

            # (b) legacy: project-level subagents dir, internal sessionId probe.
            legacy = project_dir / "subagents"
            if legacy.is_dir():
                for agent_file in sorted(legacy.glob("agent-*.jsonl")):
                    meta = _build_subagent_meta(
                        agent_file, None, require_session_id=session.id
                    )
                    if meta:
                        results.append(meta)

            # (c) oldest: agent files loose in the project dir, same probe.
            for agent_file in sorted(project_dir.glob("agent-*.jsonl")):
                meta = _build_subagent_meta(
                    agent_file, None, require_session_id=session.id
                )
                if meta:
                    results.append(meta)
        else:
            # Loose/archived transcript: only the current per-session layout,
            # relative to the file's own directory. Keep it simple.
            current = source.parent / session.id / "subagents"
            if current.is_dir():
                for agent_file in sorted(current.glob("agent-*.jsonl")):
                    meta = _build_subagent_meta(
                        agent_file, _read_agent_sidecar(agent_file)
                    )
                    if meta:
                        results.append(meta)

        results.sort(
            key=lambda m: m.first_timestamp
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return results

    def get_subagent_messages(
        self, session: SessionMeta, meta: SubagentMeta
    ) -> list[Message]:
        """Load parsed messages for one sub-agent transcript.

        Reuses :meth:`get_messages` by pointing a shallow copy of the session at
        the agent file; the agent records carry the parent ``sessionId`` so the
        existing filter passes, and records without a ``message`` field (e.g.
        ``fork-context-ref``) are skipped by that code path.
        """
        agent_session = dataclasses.replace(session, source_path=meta.file_path)
        return self.get_messages(agent_session)

    def count_subagents(self, session_id: str, project_dir: Path) -> int:
        """Cheap sub-agent count for tree badges — current layout only, no reads."""
        subdir = project_dir / session_id / "subagents"
        if not subdir.is_dir():
            return 0
        return len(list(subdir.glob("agent-*.jsonl")))

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

        # Remove the per-session sidecar dir (subagents + tool-results).
        sidecar_dir = source_dir / session.id
        if sidecar_dir.is_dir():
            shutil.rmtree(sidecar_dir, ignore_errors=True)

        # Remove legacy agent files (layouts b/c) belonging to this session,
        # identified by their internal (parent) sessionId.
        for base in (source_dir / "subagents", source_dir):
            if not base.is_dir():
                continue
            for agent_file in base.glob("agent-*.jsonl"):
                scan = _scan_agent_file(agent_file)
                if scan and scan["session_id"] == session.id:
                    try:
                        agent_file.unlink()
                    except OSError:
                        continue

    def move_project(self, old_path: str, new_path: str) -> MoveReport:
        """Update Claude metadata when a project path changes."""
        projects_dir = self._projects_dir
        old_encoded = encode_claude_path(old_path)
        new_encoded = encode_claude_path(new_path)
        old_dir = projects_dir / old_encoded
        new_dir = projects_dir / new_encoded

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

            # Also rewrite cwd inside sub-agent files across all three layouts.
            for pattern in (
                "agent-*.jsonl",
                "subagents/*.jsonl",
                "*/subagents/*.jsonl",
            ):
                for jsonl_file in target_dir.glob(pattern):
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

        projects_dir = self._projects_dir
        if not projects_dir.is_dir():
            return None

        for entry in projects_dir.iterdir():
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
                                "start_timestamp": None,
                                "message_count": 0,
                                "model": None,
                                "last_user_message": None,
                                "cwd": entry.get("cwd"),
                                "input_tokens": 0,
                                "output_tokens": 0,
                                "cumulative_input_tokens": 0,
                            }

                        s = sessions[session_id]

                        # Update timestamp
                        ts = entry.get("timestamp")
                        if ts:
                            parsed = _parse_timestamp(ts)
                            if s["timestamp"] is None or parsed > s["timestamp"]:
                                s["timestamp"] = parsed
                            if s["start_timestamp"] is None or parsed < s["start_timestamp"]:
                                s["start_timestamp"] = parsed

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

                        # Extract model and token usage from assistant messages
                        if role == "assistant":
                            if msg.get("model"):
                                s["model"] = msg["model"]
                            usage = msg.get("usage")
                            if usage:
                                turn_input = (
                                    usage.get("input_tokens", 0)
                                    + usage.get("cache_creation_input_tokens", 0)
                                    + usage.get("cache_read_input_tokens", 0)
                                )
                                s["input_tokens"] = turn_input
                                s["cumulative_input_tokens"] += turn_input
                                s["output_tokens"] += usage.get("output_tokens", 0)

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
                start_timestamp=s["start_timestamp"],
                message_count=s["message_count"],
                model=s["model"],
                source_path=str(project_dir),
                input_tokens=s["input_tokens"] or None,
                output_tokens=s["output_tokens"] or None,
                cumulative_input_tokens=s["cumulative_input_tokens"] or None,
                host=self.host,
                subagent_count=self.count_subagents(sid, project_dir),
            ))

        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result
