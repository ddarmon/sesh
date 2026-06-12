"""Gemini CLI session provider.

Gemini CLI (google-gemini/gemini-cli) stores each session as one
pretty-printed JSON document under
``~/.gemini/tmp/{project-dir}/chats/session-{YYYY-MM-DDTHH-MM}-{shortid}.json``.

The ``{project-dir}`` component is either:

-   the SHA-256 hex digest of the project cwd, or
-   a friendly name the user assigned via ``/project`` (recorded in
    ``~/.gemini/projects.json`` as a ``{path: name}`` mapping).

The session document carries ``sessionId``, ``projectHash`` (SHA-256 of
the cwd), ``startTime``, ``lastUpdated``, ``messages``, and an optional
``summary``. Messages have ``type`` of ``user`` / ``gemini`` / ``error``
/ ``info``; ``gemini`` messages additionally carry ``thoughts`` (list of
``{subject, description}``), ``toolCalls`` (call + inline
``functionResponse`` result), ``tokens``
(``{input, output, cached, thoughts, tool, total}``), and ``model``.

**Project path limitation:** the on-disk format records only a SHA-256
hash of the cwd, which is not invertible. The provider resolves real
paths by (a) reverse-mapping friendly names through ``projects.json``
and (b) hashing every path listed in ``projects.json`` and comparing
against hash-named directories. Directories that resolve neither way
keep the tmp directory itself as their ``project_path`` and get a
``gemini:{hash8}`` display name.

Unlike the JSONL providers, a session file here is a single (nested)
JSON document, so it cannot be parsed line-by-line; files are parsed
with ``json.load`` on demand. Session files are bounded in size (one
conversation each), so this stays cheap.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Message, Provider, SessionMeta
from sesh.providers import SessionProvider

GEMINI_DIR = Path.home() / ".gemini"
TMP_DIR = GEMINI_DIR / "tmp"
PROJECTS_FILE = GEMINI_DIR / "projects.json"

# User-typed slash commands ("/model", "/chat resume", ...) are session
# control, not conversation content.
_COMMAND_PREFIX = "/"


def _parse_timestamp(ts) -> datetime:
    """Parse a Gemini timestamp (ISO-8601 string, usually with Z)."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    if isinstance(ts, str):
        s = ts.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def hash_gemini_path(path: str) -> str:
    """Hash a project path the way Gemini CLI names its tmp directories."""
    return hashlib.sha256(path.encode()).hexdigest()


def _load_projects_file(gemini_dir: Path) -> dict[str, str]:
    """Load the ``{path: name}`` mapping from ``projects.json``, or {}."""
    projects_file = gemini_dir / "projects.json"
    if not projects_file.is_file():
        return {}
    try:
        with open(projects_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    mapping = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(mapping, dict):
        return {}
    return {
        str(path): str(name)
        for path, name in mapping.items()
        if isinstance(path, str) and isinstance(name, str)
    }


def _build_dir_name_map(gemini_dir: Path) -> dict[str, str]:
    """Map every resolvable tmp dir name to its real project path.

    Friendly names map directly; hash-named dirs resolve when their
    name equals the SHA-256 of a path listed in ``projects.json``.
    """
    dir_map: dict[str, str] = {}
    for path, name in _load_projects_file(gemini_dir).items():
        dir_map[name] = path
        dir_map[hash_gemini_path(path)] = path
    return dir_map


def resolve_chats_project_path(project_dir: Path, gemini_dir: Path) -> str:
    """Resolve the project path for one ``~/.gemini/tmp/{dir}`` directory.

    Falls back to the tmp directory path itself when the hash cannot be
    reversed (shared with `sesh.search` so search results and the index
    agree on project paths).
    """
    resolved = _build_dir_name_map(gemini_dir).get(project_dir.name)
    return resolved if resolved else str(project_dir)


def _display_name_for(project_path: str, dir_name: str) -> str:
    if project_path.endswith(f"/tmp/{dir_name}"):
        # Unresolved hash dir: a 64-char hex name is useless in the tree.
        return f"gemini:{dir_name[:8]}"
    return Path(project_path).name or project_path


_SESSION_ID_RE = re.compile(r'"sessionId"\s*:\s*"([^"]+)"')


def read_session_id(file_path: Path) -> str | None:
    """Stream the head of a session file for its ``sessionId`` field.

    The field is always near the top of the document, so this avoids a
    full parse (used by search, where only the id is needed).
    """
    try:
        with open(file_path) as f:
            for i, line in enumerate(f):
                m = _SESSION_ID_RE.search(line)
                if m:
                    return m.group(1)
                if i > 20:
                    break
    except OSError:
        return None
    return None


def _flatten_content(content) -> str:
    """Flatten Gemini message content (str or list of ``{text}`` parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(text)
            elif isinstance(item, str) and item:
                parts.append(item)
        return "\n".join(parts)
    return ""


def _is_command_message(text: str) -> bool:
    """True for user-typed slash commands like ``/model`` or ``/chat list``."""
    if not text.startswith(_COMMAND_PREFIX):
        return False
    head = text.split(None, 1)[0]
    # "/model" yes; a bare path like "/Users/me/file" no.
    return len(head) > 1 and "/" not in head[1:]


def _flatten_tool_result(result) -> str:
    """Flatten a toolCall ``result`` list of functionResponse wrappers."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if not isinstance(result, list):
        return json.dumps(result, indent=2)
    parts: list[str] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        response = item.get("functionResponse", {})
        if isinstance(response, dict):
            payload = response.get("response", response)
        else:
            payload = response
        if isinstance(payload, dict):
            output = payload.get("output")
            if isinstance(output, str):
                parts.append(output)
                continue
            error = payload.get("error")
            if isinstance(error, str):
                parts.append(error)
                continue
            parts.append(json.dumps(payload, indent=2))
        elif isinstance(payload, str) and payload:
            parts.append(payload)
    return "\n".join(parts)


class GeminiProvider(SessionProvider):
    """Provider for Gemini CLI sessions."""

    def __init__(
        self,
        cache=None,
        base_dir: Path | None = None,
        host: str | None = None,
    ) -> None:
        # One project path can map to several tmp dirs (an old hash dir
        # plus a later friendly-named dir), so keep a list per path.
        self._path_to_dirs: dict[str, list[Path]] = {}
        self._cache = cache
        self._base_dir = base_dir
        self.host = host

    @property
    def _gemini_dir(self) -> Path:
        return GEMINI_DIR if self._base_dir is None else self._base_dir / ".gemini"

    @property
    def _tmp_dir(self) -> Path:
        return TMP_DIR if self._base_dir is None else self._gemini_dir / "tmp"

    def discover_projects(self) -> Iterator[tuple[str, str]]:
        """Yield (project_path, display_name) for each Gemini project."""
        tmp_dir = self._tmp_dir
        if not tmp_dir.is_dir():
            return

        dir_map = _build_dir_name_map(self._gemini_dir)
        self._path_to_dirs = {}
        seen: set[str] = set()

        for entry in sorted(tmp_dir.iterdir()):
            if not entry.is_dir():
                continue
            chats_dir = entry / "chats"
            if not chats_dir.is_dir():
                continue
            if not any(chats_dir.glob("session-*.json")):
                continue

            project_path = dir_map.get(entry.name) or str(entry)
            self._path_to_dirs.setdefault(project_path, []).append(entry)
            if project_path in seen:
                continue
            seen.add(project_path)
            yield project_path, _display_name_for(project_path, entry.name)

    def get_sessions(self, project_path: str, cache=None) -> list[SessionMeta]:
        """Return sessions for a Gemini project (one session per file)."""
        project_dirs = self._find_project_dirs(project_path)
        if not project_dirs:
            return []

        active_cache = cache if cache is not None else self._cache

        result: list[SessionMeta] = []
        for project_dir in project_dirs:
            chats_dir = project_dir / "chats"
            if not chats_dir.is_dir():
                continue
            for session_file in sorted(chats_dir.glob("session-*.json")):
                file_str = str(session_file)

                if active_cache:
                    cached = active_cache.get_sessions(file_str)
                    if cached:
                        result.extend(cached)
                        continue

                data = self._parse_session_file(session_file)
                if not data:
                    continue
                session = SessionMeta(
                    id=data["id"],
                    project_path=project_path,
                    provider=Provider.GEMINI,
                    summary=data.get("summary", "Gemini Session"),
                    timestamp=data["timestamp"],
                    start_timestamp=data.get("start_timestamp"),
                    message_count=data.get("message_count", 0),
                    model=data.get("model"),
                    source_path=file_str,
                    input_tokens=data.get("input_tokens"),
                    output_tokens=data.get("output_tokens"),
                    cumulative_input_tokens=data.get("cumulative_input_tokens"),
                    host=self.host,
                )
                result.append(session)
                if active_cache:
                    active_cache.put_sessions(file_str, [session])

        result.sort(key=lambda s: s.timestamp, reverse=True)
        return result

    def get_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages from a Gemini session JSON file."""
        if not session.source_path:
            return []
        file_path = Path(session.source_path)
        if not file_path.is_file():
            return []

        try:
            with open(file_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, dict):
            return []

        messages: list[Message] = []
        for entry in data.get("messages", []):
            if not isinstance(entry, dict):
                continue
            mtype = entry.get("type", "")
            ts = _parse_timestamp(entry.get("timestamp"))

            if mtype == "user":
                text = _flatten_content(entry.get("content"))
                if not text.strip():
                    continue
                messages.append(Message(
                    role="user",
                    content=text,
                    timestamp=ts,
                    is_system=_is_command_message(text),
                    content_type="text",
                ))

            elif mtype == "gemini":
                for thought in entry.get("thoughts") or []:
                    if not isinstance(thought, dict):
                        continue
                    subject = thought.get("subject", "")
                    description = thought.get("description", "")
                    thinking_text = "\n".join(p for p in (subject, description) if p)
                    if not thinking_text.strip():
                        continue
                    messages.append(Message(
                        role="assistant",
                        content="",
                        timestamp=ts,
                        thinking=thinking_text,
                        content_type="thinking",
                    ))

                text = _flatten_content(entry.get("content"))
                if text.strip():
                    messages.append(Message(
                        role="assistant",
                        content=text,
                        timestamp=ts,
                        content_type="text",
                    ))

                for call in entry.get("toolCalls") or []:
                    if not isinstance(call, dict):
                        continue
                    name = call.get("name", "")
                    args = call.get("args", {})
                    messages.append(Message(
                        role="assistant",
                        content="",
                        timestamp=ts,
                        tool_name=name,
                        tool_input=json.dumps(args, indent=2),
                        content_type="tool_use",
                    ))
                    result_str = _flatten_tool_result(call.get("result"))
                    if result_str:
                        messages.append(Message(
                            role="tool",
                            content="",
                            timestamp=ts,
                            tool_name=name,
                            tool_output=result_str,
                            content_type="tool_result",
                        ))

            elif mtype in ("error", "info"):
                text = _flatten_content(entry.get("content"))
                if not text.strip():
                    continue
                messages.append(Message(
                    role="system",
                    content=text,
                    timestamp=ts,
                    is_system=True,
                    content_type="text",
                ))

        return messages

    def delete_session(self, session: SessionMeta) -> None:
        """Delete a Gemini session by removing its JSON file."""
        if session.source_path:
            Path(session.source_path).unlink(missing_ok=True)

    def _find_project_dirs(self, project_path: str) -> list[Path]:
        cached = self._path_to_dirs.get(project_path)
        if cached:
            live = [d for d in cached if d.is_dir()]
            if live:
                return live
            self._path_to_dirs.pop(project_path, None)

        tmp_dir = self._tmp_dir
        if not tmp_dir.is_dir():
            return []

        dirs: list[Path] = []

        # Unresolved-hash fallback: project_path is the tmp dir itself.
        as_path = Path(project_path)
        if as_path.parent == tmp_dir and as_path.is_dir():
            dirs.append(as_path)
        else:
            # Friendly name and/or hashed dir for a real cwd.
            projects = _load_projects_file(self._gemini_dir)
            name = projects.get(project_path)
            if name and (tmp_dir / name).is_dir():
                dirs.append(tmp_dir / name)
            hashed = tmp_dir / hash_gemini_path(project_path)
            if hashed.is_dir():
                dirs.append(hashed)

        if dirs:
            self._path_to_dirs[project_path] = dirs
        return dirs

    def _parse_session_file(self, file_path: Path) -> dict | None:
        """Extract metadata from a single Gemini session JSON file."""
        try:
            with open(file_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None

        session_id = data.get("sessionId") or file_path.stem
        start_ts = _parse_timestamp(data.get("startTime")) if data.get("startTime") else None
        last_ts = (
            _parse_timestamp(data.get("lastUpdated"))
            if data.get("lastUpdated")
            else start_ts
        )

        model: str | None = None
        first_user_msg: str | None = None
        message_count = 0
        last_input_tokens: int | None = None
        cumul_input_tokens = 0
        output_tokens = 0
        saw_tokens = False

        for entry in data.get("messages", []):
            if not isinstance(entry, dict):
                continue
            mtype = entry.get("type", "")

            if mtype == "user":
                message_count += 1
                if first_user_msg is None:
                    text = _flatten_content(entry.get("content"))
                    if text.strip() and not _is_command_message(text):
                        first_user_msg = text
            elif mtype == "gemini":
                message_count += 1
                m = entry.get("model")
                if m:
                    model = m
                tokens = entry.get("tokens")
                if isinstance(tokens, dict):
                    saw_tokens = True
                    turn_input = int(tokens.get("input", 0) or 0)
                    last_input_tokens = turn_input
                    cumul_input_tokens += turn_input
                    output_tokens += int(tokens.get("output", 0) or 0)
                    output_tokens += int(tokens.get("thoughts", 0) or 0)

        summary = data.get("summary") or ""
        if not summary and first_user_msg:
            summary = first_user_msg[:80] + ("..." if len(first_user_msg) > 80 else "")
        if not summary:
            summary = "Gemini Session"

        return {
            "id": session_id,
            "model": model,
            "timestamp": last_ts or datetime.now(tz=timezone.utc),
            "start_timestamp": start_ts,
            "summary": summary,
            "message_count": message_count,
            "input_tokens": last_input_tokens if saw_tokens else None,
            "output_tokens": output_tokens if saw_tokens else None,
            "cumulative_input_tokens": cumul_input_tokens if saw_tokens else None,
        }


__all__ = [
    "GEMINI_DIR",
    "TMP_DIR",
    "PROJECTS_FILE",
    "GeminiProvider",
    "hash_gemini_path",
    "read_session_id",
    "resolve_chats_project_path",
]
