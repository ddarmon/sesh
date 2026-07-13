"""Claude Code session provider."""

from __future__ import annotations

import json
import os
import re
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
from sesh.providers.history import active_ancestor_ids

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


def _claude_active_ids(files: list[Path], session_id: str) -> set[str] | None:
    """Return the active UUID ancestry for a Claude session.

    Current Claude writes an explicit ``last-prompt.leafUuid``. Older linear
    files are projected only when they have one unambiguous leaf; otherwise
    callers retain the legacy linear transcript.
    """
    parents: dict[str, str | None] = {}
    explicit_leaf: str | None = None
    unlinked_message = False
    try:
        for file_path in files:
            with open(file_path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(entry, dict) or entry.get("sessionId") != session_id:
                        continue
                    if entry.get("type") == "last-prompt" and isinstance(entry.get("leafUuid"), str):
                        explicit_leaf = entry["leafUuid"]
                    entry_id = entry.get("uuid")
                    if isinstance(entry_id, str) and entry_id:
                        parent = entry.get("parentUuid")
                        parents[entry_id] = parent if isinstance(parent, str) else None
                    elif isinstance(entry.get("message"), dict):
                        unlinked_message = True
    except OSError:
        return None

    if unlinked_message:
        return None

    leaf = explicit_leaf
    if leaf is None and parents:
        parent_ids = {parent for parent in parents.values() if parent is not None}
        leaves = [entry_id for entry_id in parents if entry_id not in parent_ids]
        if len(leaves) == 1:
            leaf = leaves[0]
    return active_ancestor_ids(parents, leaf)


def _claude_active_metadata(
    files: list[Path], session_id: str, active_ids: set[str] | None
) -> dict | None:
    """Collect branch-sensitive discovery fields in a streaming pass."""
    if active_ids is None:
        return None
    result = {"message_count": 0, "last_user_message": None, "model": None, "input_tokens": None}
    try:
        for file_path in files:
            with open(file_path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if (
                        not isinstance(entry, dict)
                        or entry.get("sessionId") != session_id
                        or entry.get("uuid") not in active_ids
                    ):
                        continue
                    msg = entry.get("message")
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role")
                    result["message_count"] += 1
                    if role == "user":
                        text = _extract_text(msg.get("content", ""))
                        if text and not _is_system_message(text):
                            result["last_user_message"] = text
                    elif role == "assistant":
                        if msg.get("model"):
                            result["model"] = msg["model"]
                        usage = msg.get("usage")
                        if isinstance(usage, dict):
                            result["input_tokens"] = (
                                usage.get("input_tokens", 0)
                                + usage.get("cache_creation_input_tokens", 0)
                                + usage.get("cache_read_input_tokens", 0)
                            )
    except OSError:
        return None
    return result


def _parse_timestamp(ts) -> datetime:
    """Parse a timestamp from string or epoch millis.

    Always returns a timezone-aware datetime. A parsed ISO value with no
    offset is assumed to be UTC — matching the repo convention that naive
    datetimes are treated as UTC (see ``--since``/``--until``). This keeps
    every downstream ``min``/``max``/``<`` comparison between parsed
    timestamps safe even when a single file mixes offset-bearing and
    offset-naive stamps (which otherwise raises ``TypeError: can't compare
    offset-naive and offset-aware datetimes``).
    """
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    if isinstance(ts, str):
        # Handle ISO format with or without Z
        ts = ts.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(ts)
        except ValueError:
            pass
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
    return datetime.now(tz=timezone.utc)


_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9._-]+")


def _is_safe_session_id(session_id: str) -> bool:
    """True when ``session_id`` is a single, traversal-safe path component.

    Session ids come from the ``sessionId`` field of JSONL records, so a
    corrupt or hostile transcript could carry ``../`` or an absolute path.
    Anything used to build a filesystem path (sidecar dir, per-session
    subagents dir) is gated on this: a conservative allowlist (matching how
    ``viewcache`` sanitizes) with no path separators, and not a pure-dot name
    like ``.`` / ``..`` that could still traverse.
    """
    if not session_id or _SAFE_SESSION_ID.fullmatch(session_id) is None:
        return False
    return session_id.strip(".") != ""


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


def _blocks_to_messages(
    role: str,
    ts: datetime,
    raw_content,
    tool_id_map: dict[str, str],
) -> list[Message]:
    """Turn one record's ``message.content`` into a list of :class:`Message`.

    Shared by the main-session loader (:meth:`ClaudeProvider.get_messages`)
    and the single-pass sub-agent loader (:func:`_parse_agent_file`) so both
    parse content blocks identically. ``tool_id_map`` is threaded across
    records so ``tool_result`` blocks can resolve the spawning tool's name.
    """
    out: list[Message] = []
    if isinstance(raw_content, str):
        if not raw_content.strip():
            return out
        is_sys = role == "user" and _is_system_message(raw_content)
        out.append(Message(
            role=role,
            content=raw_content,
            timestamp=ts,
            is_system=is_sys,
            content_type="text",
        ))
        return out
    if not isinstance(raw_content, list):
        return out

    for block in raw_content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")

        if btype == "text":
            text = block.get("text", "")
            if not text.strip():
                continue
            is_sys = role == "user" and _is_system_message(text)
            out.append(Message(
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
            out.append(Message(
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
            out.append(Message(
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
            out.append(Message(
                role="tool",
                content="",
                timestamp=ts,
                tool_name=resolved_name,
                tool_output=result_str,
                content_type="tool_result",
            ))
    return out


def _probe_agent_session_id(
    agent_file: Path, *, max_records: int = 5, max_lines: int = 50
) -> str | None:
    """Cheaply read the internal (parent) ``sessionId`` from a file's head.

    Legacy layouts (b/c) attribute an agent file to a session by its internal
    ``sessionId``. Reading a whole file just to learn it belongs to a
    *different* session is wasteful when a project holds many legacy agent
    files, so we probe only the head: the first ``sessionId`` seen within the
    first ``max_records`` object records (or ``max_lines`` physical lines).

    Tradeoff: an agent file whose early records omit ``sessionId`` (rare —
    records normally carry it) is reported as ``None`` and treated as a
    non-match rather than parsed in full. Legacy layouts accept that miss to
    keep session-open latency bounded by the session, not the whole project.
    Returns the ``sessionId`` string, or ``None`` if none appears in the probe
    window. Returns ``None`` on OSError.
    """
    try:
        with open(agent_file) as f:
            records = 0
            for i, line in enumerate(f):
                if i >= max_lines or records >= max_records:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                records += 1
                sid = entry.get("sessionId")
                if sid:
                    return sid
    except OSError:
        return None
    return None


def _parse_agent_file(agent_file: Path) -> tuple[dict, list[Message]] | None:
    """Single-pass read of an ``agent-*.jsonl``: aggregate meta + messages.

    Reads the file exactly once, building both the scan metadata (internal
    parent ``session_id``, ``message_count``, ``output_tokens``, earliest
    ``first_timestamp``, ``first_user_text``) and the parsed interior
    :class:`Message` list. This replaces the old two-pass approach (scan, then
    reopen and reparse for messages).

    No ``sessionId`` filter is applied: an agent file is a single sub-agent
    thread by construction, so every record with a ``message`` field belongs
    to it — including older forks whose records carry no internal ``sessionId``
    at all. Non-dict JSON lines, string ``message`` fields, and non-dict
    ``usage`` are skipped defensively. Returns ``None`` on OSError.
    """
    session_id: str | None = None
    message_count = 0
    output_tokens = 0
    first_ts: datetime | None = None
    first_user_text: str | None = None
    messages: list[Message] = []
    tool_id_map: dict[str, str] = {}

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
                if not isinstance(entry, dict):
                    continue

                sid = entry.get("sessionId")
                if sid and session_id is None:
                    session_id = sid

                raw_ts = entry.get("timestamp")
                msg_ts = _parse_timestamp(raw_ts)
                if raw_ts and (first_ts is None or msg_ts < first_ts):
                    first_ts = msg_ts

                msg = entry.get("message")
                if not isinstance(msg, dict):
                    continue
                message_count += 1

                role = msg.get("role", "")
                if role == "assistant":
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        output_tokens += usage.get("output_tokens", 0)
                elif role == "user" and first_user_text is None:
                    text = _extract_text(msg.get("content", ""))
                    if text and not _is_system_message(text):
                        first_user_text = text

                messages.extend(
                    _blocks_to_messages(role, msg_ts, msg.get("content", ""), tool_id_map)
                )
    except OSError:
        return None

    messages.sort(
        key=lambda m: m.timestamp or datetime.min.replace(tzinfo=timezone.utc)
    )
    scan = {
        "session_id": session_id,
        "message_count": message_count,
        "output_tokens": output_tokens,
        "first_timestamp": first_ts,
        "first_user_text": first_user_text,
    }
    return scan, messages


def _meta_from_scan(
    agent_file: Path,
    scan: dict,
    sidecar_data: dict | None,
    workflow_id: str | None = None,
) -> SubagentMeta:
    """Build a :class:`SubagentMeta` from parsed scan metadata + optional sidecar.

    Description falls back to the first user message text (first 80 chars) when
    the sidecar has none. ``workflow_id`` is set for Workflow-tool sub-agents
    (transcripts under ``subagents/workflows/{workflow_id}/``).
    """
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
        workflow_id=workflow_id,
    )


def _load_agent_file(
    agent_file: Path, sidecar_data: dict | None, workflow_id: str | None = None
) -> tuple[SubagentMeta, list[Message]] | None:
    """Parse one agent file into ``(SubagentMeta, interior messages)`` in one pass."""
    parsed = _parse_agent_file(agent_file)
    if parsed is None:
        return None
    scan, messages = parsed
    return _meta_from_scan(agent_file, scan, sidecar_data, workflow_id), messages


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
        """Load messages on the active Claude conversation branch."""
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

        active_ids = _claude_active_ids(jsonl_files, session.id)
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
                        if not isinstance(entry, dict):
                            continue

                        if entry.get("sessionId") != session.id:
                            continue
                        if active_ids is not None and entry.get("uuid") not in active_ids:
                            continue

                        msg = entry.get("message")
                        if not isinstance(msg, dict):
                            continue

                        role = msg.get("role", "")
                        ts = _parse_timestamp(entry.get("timestamp"))
                        messages.extend(
                            _blocks_to_messages(
                                role, ts, msg.get("content", ""), tool_id_map
                            )
                        )
            except OSError:
                continue

        messages.sort(key=lambda m: m.timestamp or datetime.min.replace(tzinfo=timezone.utc))
        return messages

    def _workflow_subagent_files(
        self, subagents_dir: Path
    ) -> Iterator[tuple[Path, dict | None, bool, str | None]]:
        """Yield Workflow-tool agent files under ``{subagents}/workflows/``.

        Layout ``{subagents}/workflows/{workflowId}/agent-*.jsonl`` (only under
        the current per-session layout — no legacy variants). Workflow agents
        are grouped by workflow id (dirs sorted), each group sorted like the
        existing glob-sort. Each workflow dir name is gated on the same
        traversal-safe allowlist as session/agent ids so a hostile
        ``../`` dir can't escape the subagents tree; the sidecar is honored.
        """
        workflows_dir = subagents_dir / "workflows"
        if not workflows_dir.is_dir():
            return
        for wf_dir in sorted(workflows_dir.iterdir()):
            if not wf_dir.is_dir() or not _is_safe_session_id(wf_dir.name):
                continue
            for agent_file in sorted(wf_dir.glob("agent-*.jsonl")):
                yield agent_file, _read_agent_sidecar(agent_file), False, wf_dir.name

    def _subagent_files(
        self, session: SessionMeta
    ) -> Iterator[tuple[Path, dict | None, bool, str | None]]:
        """Yield ``(agent_file, sidecar_data, needs_probe, workflow_id)`` per layout.

        ``needs_probe`` is True for legacy layouts (b/c) that have no sidecar and
        must be attributed to this session by their internal parent
        ``sessionId`` — callers probe the head cheaply and skip non-matching
        files without a full read. ``workflow_id`` is set only for Workflow-tool
        agents (layout d). The current per-session layouts (a/d) are gated on a
        traversal-safe ``session.id`` so a hostile id can't glob outside the
        project dir. Ordering: top-level agents first (existing order preserved),
        then workflow agents grouped by workflow id.
        """
        source = Path(session.source_path)

        if source.is_dir():
            project_dir = source

            # (a) current layout: per-session subagents dir, sidecar honored.
            if _is_safe_session_id(session.id):
                current = project_dir / session.id / "subagents"
                if current.is_dir():
                    for agent_file in sorted(current.glob("agent-*.jsonl")):
                        yield agent_file, _read_agent_sidecar(agent_file), False, None
                    # (d) Workflow-tool agents, one level deeper. Grouped after
                    # the top-level agents so ordering stays deterministic.
                    yield from self._workflow_subagent_files(current)

            # (b) legacy: project-level subagents dir, internal sessionId probe.
            legacy = project_dir / "subagents"
            if legacy.is_dir():
                for agent_file in sorted(legacy.glob("agent-*.jsonl")):
                    yield agent_file, None, True, None

            # (c) oldest: agent files loose in the project dir, same probe.
            for agent_file in sorted(project_dir.glob("agent-*.jsonl")):
                yield agent_file, None, True, None
        elif _is_safe_session_id(session.id):
            # Loose/archived transcript: only the current per-session layout,
            # relative to the file's own directory. Keep it simple.
            current = source.parent / session.id / "subagents"
            if current.is_dir():
                for agent_file in sorted(current.glob("agent-*.jsonl")):
                    yield agent_file, _read_agent_sidecar(agent_file), False, None
                yield from self._workflow_subagent_files(current)

    def load_subagents(
        self, session: SessionMeta
    ) -> list[tuple[SubagentMeta, list[Message]]]:
        """Discover and load every sub-agent thread, one pass per file.

        Reads each ``agent-*.jsonl`` exactly once, building its
        :class:`SubagentMeta` and parsed interior messages together (replacing
        the old discover-then-reload double read). Legacy layouts (b/c) are
        probed cheaply for the internal parent ``sessionId`` and skipped
        without a full read when they belong to a different session.

        Lazy and defensive: swallows per-file parse errors; results are sorted
        by ``first_timestamp``.
        """
        if not session.source_path:
            return []

        results: list[tuple[SubagentMeta, list[Message]]] = []
        for agent_file, sidecar, needs_probe, workflow_id in self._subagent_files(session):
            if needs_probe and _probe_agent_session_id(agent_file) != session.id:
                continue
            loaded = _load_agent_file(agent_file, sidecar, workflow_id)
            if loaded is not None:
                results.append(loaded)

        results.sort(
            key=lambda pair: pair[0].first_timestamp
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return results

    def discover_subagents(self, session: SessionMeta) -> list[SubagentMeta]:
        """Sub-agent metadata (meta-only) across all on-disk layouts.

        Shares :meth:`load_subagents`' single-pass parsing and drops the
        interior messages; use ``load_subagents`` directly when the messages
        are also needed (view/export/TUI) to avoid re-reading files.
        """
        return [meta for meta, _ in self.load_subagents(session)]

    def get_subagent_messages(
        self, session: SessionMeta, meta: SubagentMeta
    ) -> list[Message]:
        """Load parsed messages for one sub-agent transcript.

        Single-pass parse of the agent file with no ``sessionId`` filter — an
        agent file is a single-thread transcript, so every record with a
        ``message`` field belongs to it (older forks may carry no internal
        ``sessionId`` at all). Records without a ``message`` field (e.g.
        ``fork-context-ref``) are skipped.
        """
        parsed = _parse_agent_file(Path(meta.file_path))
        return parsed[1] if parsed is not None else []

    def count_subagents(self, session_id: str, project_dir: Path) -> int:
        """Cheap sub-agent count for tree badges — current layout only, no reads.

        Counts both top-level agents (``subagents/agent-*.jsonl``) and
        Workflow-tool agents one level deeper
        (``subagents/workflows/*/agent-*.jsonl``). Both are cheap globs.
        """
        if not _is_safe_session_id(session_id):
            return 0
        subdir = project_dir / session_id / "subagents"
        if not subdir.is_dir():
            return 0
        return len(list(subdir.glob("agent-*.jsonl"))) + len(
            list(subdir.glob("workflows/*/agent-*.jsonl"))
        )

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
        # session.id comes from JSONL records, so a corrupt/hostile transcript
        # could carry ``../`` or an absolute path — never rmtree an
        # id-derived path unsanitized. Require: a traversal-safe id, a real
        # (non-symlink) directory, and a resolved parent that is exactly the
        # project dir, so nothing outside the project can be deleted.
        sidecar_dir = source_dir / session.id
        if (
            _is_safe_session_id(session.id)
            and sidecar_dir.is_dir()
            and not sidecar_dir.is_symlink()
            and sidecar_dir.resolve().parent == source_dir.resolve()
        ):
            shutil.rmtree(sidecar_dir, ignore_errors=True)

        # Remove legacy agent files (layouts b/c) belonging to this session,
        # identified by their internal (parent) sessionId (cheap head probe).
        for base in (source_dir / "subagents", source_dir):
            if not base.is_dir():
                continue
            for agent_file in base.glob("agent-*.jsonl"):
                if _probe_agent_session_id(agent_file) == session.id:
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

            # Also rewrite cwd inside sub-agent files across all layouts,
            # including Workflow-tool agents one level deeper
            # (``{sessionId}/subagents/workflows/{wf}/agent-*.jsonl``).
            for pattern in (
                "agent-*.jsonl",
                "subagents/*.jsonl",
                "*/subagents/*.jsonl",
                "*/subagents/workflows/*/agent-*.jsonl",
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

        # Build SessionMeta list. Branch-sensitive fields get one additional
        # streaming pass; cumulative usage above intentionally includes paid
        # work on abandoned branches.
        result = []
        for sid in result_ids:
            s = sessions[sid]
            active = _claude_active_metadata(
                jsonl_files, sid, _claude_active_ids(jsonl_files, sid)
            )
            if active is not None:
                s.update(active)
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
