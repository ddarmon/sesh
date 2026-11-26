"""Full-text search via ripgrep."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sesh.models import Provider, SearchResult, SessionMeta
from sesh.providers.cline import resolve_data_dir as _cline_data_dir
from sesh.providers.opencode import (
    _parse_revert,
    _part_is_active,
    _v2_message_to_messages,
    db_paths as _opencode_db_paths,
    db_tables as _opencode_db_tables,
    has_v1_schema as _opencode_has_v1,
    has_v2_schema as _opencode_has_v2,
    v2_cutoffs as _opencode_v2_cutoffs,
    v2_is_active as _opencode_v2_is_active,
)

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CURSOR_PROJECTS = Path.home() / ".cursor" / "projects"
CURSOR_CHATS = Path.home() / ".cursor" / "chats"
COPILOT_SESSIONS = Path.home() / ".copilot" / "session-state"
PI_SESSIONS = Path.home() / ".pi" / "agent" / "sessions"
GEMINI_TMP = Path.home() / ".gemini" / "tmp"
OPENCODE_DATA = Path.home() / ".local" / "share" / "opencode"
# Honours CLINE_DATA_DIR / CLINE_DIR the same way the provider does.
CLINE_SESSIONS = _cline_data_dir() / "sessions"
AGY_DATA = Path.home() / ".gemini" / "antigravity-cli"

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_RG_REGEX_META = re.compile(r'[\\.*+?{}()\[\]|^$]')


# Record ``type`` values whose ``message`` is transcript: Claude (``user`` /
# ``assistant``, or absent in legacy files) and pi (``message``).
_MESSAGE_RECORD_TYPES = (None, "user", "assistant", "message")


def _is_literal(query: str) -> bool:
    """True when *query* contains no regex metacharacters."""
    return not _RG_REGEX_META.search(query)


class _Matcher:
    """The one definition of "this text matches the query", for every provider.

    Mirrors what ripgrep is asked to do (case-insensitive; a literal string
    unless the query contains regex metacharacters) so the ripgrep prefilter
    and the Python check on extracted message text agree. A regex ripgrep
    accepts but Python's ``re`` cannot compile leaves the matcher *unverified*:
    it then accepts any non-empty text, so such a query degrades to the raw
    ripgrep hits instead of silently returning nothing.
    """

    def __init__(self, query: str) -> None:
        self.query = query
        self.literal = _is_literal(query)
        self._lowered = query.lower()
        self._re: re.Pattern[str] | None = None
        if not self.literal:
            try:
                self._re = re.compile(query, re.IGNORECASE)
            except re.error:
                self._re = None
        self.verified = self.literal or self._re is not None

    def span(self, text: str) -> tuple[int, int] | None:
        """Return the (start, end) of the first match in *text*, or None."""
        if not text:
            return None
        if self.literal:
            idx = text.lower().find(self._lowered)
            # Lowercasing can change a string's length for a few non-ASCII
            # characters; the offset is only used to center a snippet.
            return (idx, idx + len(self.query)) if idx != -1 else None
        if self._re is None:
            return (0, 0)
        m = self._re.search(text)
        return m.span() if m else None

    def matches(self, text: str) -> bool:
        return self.span(text) is not None

    def snippet(self, text: str, max_len: int = 200) -> str:
        """A display window of *text* centered on the first match."""
        if not text:
            return ""
        span = self.span(text)
        if span is None:
            return text[:max_len]
        idx, end_idx = span
        margin = max(0, (max_len - (end_idx - idx)) // 2)
        start = max(0, idx - margin)
        end = start + max_len
        out = text[start:end]
        if start > 0:
            out = "..." + out[3:]
        if end < len(text):
            out = out[: max_len - 3] + "..."
        return out

    def best(self, candidates: list[str]) -> str:
        """The longest candidate containing a match, or "" when none does."""
        hits = [c for c in candidates if c and self.matches(c)]
        return max(hits, key=len) if hits else ""


def _document_snippet(matcher: _Matcher, load_messages, matched_text: str) -> str:
    """Snippet for a hit in a transcript that must be loaded whole.

    A matched line of a pretty-printed JSON document (or of a transcript with
    rewound steps) cannot be judged on its own, so the provider's own loader
    decides: the hit stands only if a message it renders contains the query.
    Returns "" when none does.
    """
    if not matcher.verified:
        return matcher.snippet(matched_text) or matched_text[:200]
    return matcher.snippet(matcher.best(_message_texts(load_messages())))


def _as_matcher(query: str | _Matcher) -> _Matcher:
    return query if isinstance(query, _Matcher) else _Matcher(query)


def _probe_session(
    session_id: str, project_path: str, provider: Provider, source_path: str
) -> SessionMeta:
    """A throwaway SessionMeta, enough for a provider's ``get_messages``."""
    return SessionMeta(
        id=session_id,
        project_path=project_path,
        provider=provider,
        summary="",
        timestamp=datetime.now(timezone.utc),
        source_path=source_path,
    )


def _message_texts(messages) -> list[str]:
    """Every searchable text field of provider-normalized messages."""
    return [
        text
        for msg in messages
        for text in (msg.content, msg.thinking, msg.tool_input, msg.tool_output)
        if text
    ]


@dataclass
class _SearchRoots:
    """Per-host (or local) scan roots for ripgrep_search.

    `host` is `None` for the local-mode roots and the per-host subdir
    name in aggregation mode. Any individual root may be `None` if it
    doesn't exist on disk; callers should skip missing ones.
    """

    host: str | None
    claude_projects: Path
    codex_sessions: Path
    cursor_projects: Path
    cursor_chats: Path
    copilot_sessions: Path
    pi_sessions: Path
    gemini_tmp: Path
    opencode_data: Path
    cline_sessions: Path
    agy_data: Path


def _local_roots() -> _SearchRoots:
    """Build the local-mode scan roots from the module-level constants.

    The constants stay module-level so tests can monkeypatch them
    (see `tests/conftest.py::tmp_search_dirs`).
    """
    return _SearchRoots(
        host=None,
        claude_projects=CLAUDE_PROJECTS,
        codex_sessions=CODEX_SESSIONS,
        cursor_projects=CURSOR_PROJECTS,
        cursor_chats=CURSOR_CHATS,
        copilot_sessions=COPILOT_SESSIONS,
        pi_sessions=PI_SESSIONS,
        gemini_tmp=GEMINI_TMP,
        opencode_data=OPENCODE_DATA,
        cline_sessions=CLINE_SESSIONS,
        agy_data=AGY_DATA,
    )


def _aggregated_roots(aggregation_root: Path):
    """Yield one `_SearchRoots` per host subdir under *aggregation_root*.

    Skips hidden and non-directory entries, matching the rule in
    `discovery._discover_aggregated`.
    """
    if not aggregation_root.is_dir():
        return
    for host_dir in sorted(aggregation_root.iterdir()):
        if not host_dir.is_dir() or host_dir.name.startswith("."):
            continue
        yield _SearchRoots(
            host=host_dir.name,
            claude_projects=host_dir / ".claude" / "projects",
            codex_sessions=host_dir / ".codex" / "sessions",
            cursor_projects=host_dir / ".cursor" / "projects",
            cursor_chats=host_dir / ".cursor" / "chats",
            copilot_sessions=host_dir / ".copilot" / "session-state",
            pi_sessions=host_dir / ".pi" / "agent" / "sessions",
            gemini_tmp=host_dir / ".gemini" / "tmp",
            opencode_data=host_dir / ".local" / "share" / "opencode",
            cline_sessions=host_dir / ".cline" / "data" / "sessions",
            agy_data=host_dir / ".gemini" / "antigravity-cli",
        )


def _stringify_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except TypeError:
        return str(value)


def _entry_texts(entry: dict) -> list[str]:
    """Collect the transcript text of one JSONL record.

    Covers the Claude, pi, Codex, and Copilot record shapes. Only fields the
    providers render as messages are collected, so opaque payloads on the same
    line (encrypted reasoning, thinking signatures, base64 images) and envelope
    metadata (ids, cwd, model) can never satisfy a search.
    """
    candidates: list[str] = []

    # Claude / pi format: message.content (list of parts or string). Other
    # record types can carry a ``message`` that is not transcript: Claude
    # Code's ``api-request-blob`` holds the raw API request, system prompt and
    # skill listings included.
    msg = entry.get("message", {})
    if entry.get("type") not in _MESSAGE_RECORD_TYPES:
        msg = {}
    if isinstance(msg, dict) and msg:
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type", "")
                if ptype == "text":
                    text = part.get("text", "")
                    if text:
                        candidates.append(text)
                elif ptype == "thinking":
                    text = part.get("thinking", "")
                    if text:
                        candidates.append(text)
                elif ptype == "tool_use":
                    inp = part.get("input", {})
                    if inp:
                        candidates.append(json.dumps(inp))
                elif ptype == "toolCall":  # pi
                    args = part.get("arguments")
                    if args:
                        candidates.append(_stringify_value(args))
                elif ptype == "tool_result":
                    rc = part.get("content", "")
                    if isinstance(rc, list):
                        for rp in rc:
                            if isinstance(rp, dict) and rp.get("type") == "text":
                                t = rp.get("text", "")
                                if t:
                                    candidates.append(t)
                    elif rc:
                        candidates.append(_stringify_value(rc))
        elif isinstance(content, str) and content:
            candidates.append(content)

    # Codex payload
    payload = entry.get("payload", {})
    if isinstance(payload, dict):
        ptype = payload.get("type", "")

        # Codex function_call / custom_tool_call
        if ptype in ("function_call", "custom_tool_call"):
            args = payload.get("arguments", payload.get("input", ""))
            if args:
                candidates.append(_stringify_value(args))

        # Codex function_call_output / custom_tool_call_output
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output", "")
            if output:
                candidates.append(_stringify_value(output))

        # Codex agent_reasoning
        elif ptype == "agent_reasoning":
            text = payload.get("text", "")
            if text:
                candidates.append(text)

        # Codex response_item: payload.content (list with output_text/input_text/text).
        # ``user`` / ``developer`` / ``system`` items are model input (injected
        # instructions, AGENTS.md, environment context) that the transcript
        # does not render; a prompt the user typed is the ``user_message``
        # event handled below.
        pcontent = payload.get("content", [])
        if payload.get("role") in ("user", "developer", "system"):
            pcontent = []
        if isinstance(pcontent, list):
            for item in pcontent:
                if isinstance(item, dict):
                    text = (
                        item.get("text")
                        or item.get("output_text")
                        or item.get("input_text")
                        or ""
                    )
                    if text:
                        candidates.append(text)

        # Codex event_msg: payload.message
        pmsg = payload.get("message", "")
        if isinstance(pmsg, str) and pmsg:
            candidates.append(pmsg)

    # Codex reasoning: summary list with summary_text parts
    summary = entry.get("summary", [])
    if isinstance(summary, list):
        for item in summary:
            if isinstance(item, dict) and item.get("type") == "summary_text":
                text = item.get("text", "")
                if text:
                    candidates.append(text)

    # Codex function_call_output: output field (often JSON-encoded)
    output = entry.get("output", "")
    if isinstance(output, str) and output:
        try:
            inner = json.loads(output)
            if isinstance(inner, dict):
                inner_out = inner.get("output", "")
                if isinstance(inner_out, str) and inner_out:
                    candidates.append(inner_out)
        except (json.JSONDecodeError, AttributeError):
            pass
        candidates.append(output)

    # Copilot event format: top-level "type" + "data" wrapper
    data = entry.get("data", {})
    if isinstance(data, dict):
        etype = entry.get("type", "")
        if etype in ("user.message", "assistant.message"):
            content_text = data.get("content", "")
            if content_text:
                candidates.append(content_text)
            reasoning = data.get("reasoningText", "")
            if reasoning:
                candidates.append(reasoning)
            for req in data.get("toolRequests", []):
                args = req.get("arguments")
                if args:
                    candidates.append(
                        json.dumps(args) if not isinstance(args, str) else args
                    )
        elif etype == "tool.execution_complete":
            result = data.get("result", {})
            if isinstance(result, dict):
                rc = result.get("content", "")
                if rc:
                    candidates.append(rc)
                dc = result.get("detailedContent", "")
                if dc:
                    candidates.append(dc)

    return [c for c in candidates if isinstance(c, str) and c]


def _extract_content_text(entry: dict, query: str | _Matcher | None = None) -> str:
    """Extract readable message text from a JSONL entry.

    Prefers the longest candidate containing the query so snippets reflect the
    matched content; with no query (or no match) returns the longest candidate.
    Callers that must *verify* a hit use ``_Matcher.best(_entry_texts(entry))``,
    which returns "" when no message text matches.
    """
    candidates = _entry_texts(entry)
    if not candidates:
        return ""
    if query:
        best = _as_matcher(query).best(candidates)
        if best:
            return best
    return max(candidates, key=len)


def _extract_display_text(
    content: str, query: str | _Matcher, max_len: int = 200
) -> str:
    """Extract a display window around the first match of query in content."""
    return _as_matcher(query).snippet(content, max_len)


def _first_text_match(
    file_path: str, matcher: _Matcher, after_line: int = 0
) -> tuple[dict, str] | None:
    """Find the first record past *after_line* whose message text matches.

    ripgrep reports only the first matching line of a file, and that line may
    match only inside an opaque payload. This continues down the same file,
    parsing just the lines the query appears in at all, and returns
    ``(record, matching text)`` for the first real hit, or None.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            for line_number, line in enumerate(f, start=1):
                if line_number <= after_line or not matcher.matches(line):
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                text = matcher.best(_entry_texts(entry))
                if text:
                    return entry, text
    except OSError:
        return None
    return None


def _extract_codex_session_id(file_path: str) -> str:
    """Extract the session UUID from a Codex filename."""
    stem = Path(file_path).stem
    matches = _UUID_RE.findall(stem)
    return matches[-1] if matches else stem


def _is_claude_agent_file(file_path: str) -> bool:
    """True when the basename is a Claude sub-agent transcript (agent-*.jsonl)."""
    name = Path(file_path).name
    return name.startswith("agent-") and name.endswith(".jsonl")


def _agent_id_from_filename(file_path: str) -> str:
    """Derive the agent id from an `agent-{id}.jsonl` filename (stem minus prefix)."""
    stem = Path(file_path).stem  # e.g. "agent-abc123"
    if stem.startswith("agent-"):
        return stem[len("agent-"):]
    return ""


def _agent_parent_session_from_path(fp: Path) -> str:
    """Recover the parent sessionId from the current sub-agent layout.

    Current layout: ``{project}/{sessionId}/subagents/agent-*.jsonl`` — the
    grandparent directory name is the parent session id. The legacy layout
    ``{project}/subagents/agent-*.jsonl`` puts the encoded project dir there
    instead (it sits directly under ``projects``), so it is excluded.

    Workflow-tool agents live one level deeper,
    ``{project}/{sessionId}/subagents/workflows/{workflowId}/agent-*.jsonl``;
    the parent session is four dirs up.
    """
    parents = fp.parents
    if (
        fp.parent.name == "subagents"
        and len(parents) >= 3
        and parents[2].name != "projects"
    ):
        return parents[1].name
    if (
        len(parents) >= 4
        and parents[1].name == "workflows"
        and parents[2].name == "subagents"
    ):
        return parents[3].name
    return ""


def _decode_cursor_projects_path(encoded: str, *, validate_locally: bool = True) -> str:
    """Reverse the Cursor path encoding: 'Users-foo-bar' -> '/Users/foo/bar'.

    In local mode (validate_locally=True) falls back to the raw encoded
    name if the decoded path doesn't exist on this machine — that probe
    protects against folder names that legitimately contain hyphens.

    In aggregation mode the source-host filesystem isn't reachable, so
    the probe would always fail; callers pass validate_locally=False to
    skip it and return the decoded path unconditionally.
    """
    decoded = "/" + encoded.replace("-", "/")
    if not validate_locally:
        return decoded
    if Path(decoded).is_dir():
        return decoded
    return encoded


def _search_cursor_transcripts(
    rg: str,
    query: str,
    cursor_projects: Path,
    host: str | None,
) -> list[SearchResult]:
    """Search .txt transcript files under *cursor_projects* via ripgrep."""
    if not cursor_projects.is_dir():
        return []

    cmd = [
        rg, "--json", "-i", "-m", "1",
        *(("-F",) if _is_literal(query) else ()),
        "--glob", "*.txt",
        query,
        str(cursor_projects),
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    results: list[SearchResult] = []
    seen: set[str] = set()

    for line in proc.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") != "match":
            continue

        match_data = data.get("data", {})
        file_path = match_data.get("path", {}).get("text", "")
        matched_text = match_data.get("lines", {}).get("text", "").strip()

        if not file_path or not matched_text:
            continue

        fp = Path(file_path)
        session_id = fp.stem

        dedup_key = f"cursor:{session_id}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # Decode project path from the encoded directory name
        # Path structure: {cursor_projects}/{encoded}/agent-transcripts/{id}.txt
        encoded_name = fp.parent.parent.name
        project_path = _decode_cursor_projects_path(
            encoded_name, validate_locally=host is None,
        )

        display_text = _extract_display_text(matched_text, query)
        if not display_text:
            display_text = matched_text[:200]

        results.append(SearchResult(
            session_id=session_id,
            project_path=project_path,
            provider=Provider.CURSOR,
            matched_line=display_text,
            file_path=file_path,
            host=host,
        ))

    return results


def _search_gemini(
    rg: str,
    query: str,
    gemini_tmp: Path,
    host: str | None,
) -> list[SearchResult]:
    """Search Gemini CLI session JSON files under *gemini_tmp* via ripgrep.

    Gemini chats are pretty-printed JSON (not JSONL), so the matched line
    is a fragment; session id and project path are recovered from the
    file head and the directory layout instead of the matched line.
    """
    if not gemini_tmp.is_dir():
        return []

    from sesh.providers.gemini import (
        GeminiProvider,
        read_session_id,
        resolve_chats_project_path,
    )

    cmd = [
        rg, "--json", "-i", "-m", "1",
        *(("-F",) if _is_literal(query) else ()),
        "--glob", "session-*.json",
        query,
        str(gemini_tmp),
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    matcher = _Matcher(query)
    provider = GeminiProvider()
    results: list[SearchResult] = []
    seen: set[str] = set()
    project_path_cache: dict[str, str] = {}
    gemini_dir = gemini_tmp.parent

    for line in proc.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") != "match":
            continue

        match_data = data.get("data", {})
        file_path = match_data.get("path", {}).get("text", "")
        matched_text = match_data.get("lines", {}).get("text", "").strip()

        if not file_path or not matched_text:
            continue

        fp = Path(file_path)
        # Layout: {gemini_tmp}/{project-dir}/chats/session-*.json
        if fp.parent.name != "chats":
            continue

        if file_path in seen:
            continue
        seen.add(file_path)

        session_id = read_session_id(fp) or fp.stem

        project_dir = fp.parent.parent
        dir_key = project_dir.name
        if dir_key not in project_path_cache:
            project_path_cache[dir_key] = resolve_chats_project_path(
                project_dir, gemini_dir
            )
        project_path = project_path_cache[dir_key]

        display_text = _document_snippet(
            matcher,
            lambda: provider.get_messages(_probe_session(
                session_id, project_path, Provider.GEMINI, file_path,
            )),
            matched_text,
        )
        if not display_text:
            continue

        results.append(SearchResult(
            session_id=session_id,
            project_path=project_path,
            provider=Provider.GEMINI,
            matched_line=display_text,
            file_path=file_path,
            host=host,
        ))

    return results


def _search_cline(
    rg: str,
    query: str,
    cline_sessions: Path,
    host: str | None,
) -> list[SearchResult]:
    """Search Cline transcripts under *cline_sessions* via ripgrep.

    Each session is one ``sessions/{id}/{id}.messages.json`` document (not
    JSONL), so the matched line is a fragment; the session id comes from the
    parent directory name and the project path from the sibling record.
    Mirrors `_search_gemini`.
    """
    if not cline_sessions.is_dir():
        return []

    from sesh.providers.cline import ClineProvider, is_valid_session_id

    matcher = _Matcher(query)
    provider = ClineProvider()

    cmd = [
        rg, "--json", "-i", "-m", "1",
        *(("-F",) if _is_literal(query) else ()),
        "--glob", "*.messages.json",
        query,
        str(cline_sessions),
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    results: list[SearchResult] = []
    seen: set[str] = set()

    for line in proc.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") != "match":
            continue

        match_data = data.get("data", {})
        file_path = match_data.get("path", {}).get("text", "")
        matched_text = match_data.get("lines", {}).get("text", "").strip()
        if not file_path or not matched_text or file_path in seen:
            continue
        seen.add(file_path)

        fp = Path(file_path)
        session_id = fp.parent.name
        if not is_valid_session_id(session_id):
            continue

        project_path = _cline_project_path(fp.parent, session_id)
        if not project_path:
            continue

        display_text = _document_snippet(
            matcher,
            lambda: provider.get_messages(_probe_session(
                session_id, project_path, Provider.CLINE, file_path,
            )),
            matched_text,
        )
        if not display_text:
            continue

        results.append(SearchResult(
            session_id=session_id,
            project_path=project_path,
            provider=Provider.CLINE,
            matched_line=display_text,
            file_path=file_path,
            host=host,
        ))

    return results


def _cline_project_path(session_dir: Path, session_id: str) -> str:
    """Read ``workspace_root`` (else ``cwd``) from a Cline session record.

    Returns "" for a sub-agent record, which discovery also excludes: a hit
    attributed to a session the tree never lists could neither be opened nor
    (via ``sesh clean``) safely deleted.  When Cline ships spawning, this is
    where child hits should instead be attributed to their parent with an
    ``agent_id``, the way the Claude provider does.
    """
    try:
        with open(session_dir / f"{session_id}.json") as f:
            record = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(record, dict) or record.get("is_subagent"):
        return ""
    for field in ("workspace_root", "cwd"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def _search_agy(
    rg: str,
    query: str,
    agy_data: Path,
    host: str | None,
) -> list[SearchResult]:
    """Search Antigravity CLI transcripts under *agy_data* via ripgrep.

    Only ``brain/{id}/.system_generated/logs/transcript_full.jsonl`` is
    searched: the sibling ``transcript.jsonl`` and the ``chunks/`` copies
    repeat the same records. The conversation id is the ``brain`` directory
    name; the provider maps it to its root conversation and project, so a hit
    inside a sub-agent is attributed to the session the tree actually lists.
    """
    brain = agy_data / "brain"
    if not brain.is_dir():
        return []

    from sesh.providers.agy import AgyProvider, read_transcript, records_to_messages

    matcher = _Matcher(query)
    cmd = [
        # --hidden: the transcripts sit under the dot-directory
        # ``.system_generated``, which ripgrep skips by default.
        rg, "--json", "-i", "-m", "1", "--hidden", "--no-ignore",
        *(("-F",) if _is_literal(query) else ()),
        "--glob", "**/.system_generated/logs/transcript_full.jsonl",
        query,
        str(brain),
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    # The data dir is ``{home}/.gemini/antigravity-cli``; pointing the
    # provider at that home keeps it on the same tree ripgrep just searched.
    provider = AgyProvider(base_dir=agy_data.parent.parent, host=host)
    results: list[SearchResult] = []
    seen: set[str] = set()

    for line in proc.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") != "match":
            continue

        match_data = data.get("data", {})
        file_path = match_data.get("path", {}).get("text", "")
        matched_text = match_data.get("lines", {}).get("text", "").strip()
        if not file_path or not matched_text or file_path in seen:
            continue
        seen.add(file_path)

        conversation_id = Path(file_path).parents[2].name
        located = provider.locate(conversation_id)
        if located is None:
            continue
        root_id, project_path, agent_id = located

        # Loading through the provider also drops steps a /rewind abandoned.
        display_text = _document_snippet(
            matcher,
            lambda: records_to_messages(read_transcript(Path(file_path))[0]),
            matched_text,
        )
        if not display_text:
            continue

        results.append(SearchResult(
            session_id=root_id,
            project_path=project_path,
            provider=Provider.AGY,
            matched_line=display_text,
            file_path=file_path,
            host=host,
            agent_id=agent_id,
            root_file_path=provider.transcript_path(root_id) if agent_id else None,
        ))

    return results


def _like_filter(column: str, matcher: _Matcher) -> tuple[str, tuple]:
    """SQL prefilter for *column*, as ``(condition, params)``.

    ``LIKE`` can only narrow a literal query. A regex query scans every row
    and leaves the decision to the matcher, which runs on the extracted text
    either way.
    """
    if matcher.literal:
        return f"{column} LIKE ? ESCAPE '!'", (f"%{_escape_like(matcher.query)}%",)
    return "1", ()


def _escape_like(s: str) -> str:
    """Escape ``%``, ``_``, and ``!`` for a SQLite LIKE with ``ESCAPE '!'``."""
    return s.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _search_cursor_stores(
    query: str,
    cursor_chats: Path,
    host: str | None,
) -> list[SearchResult]:
    """Search store.db files under *cursor_chats* via SQLite."""
    if not cursor_chats.is_dir():
        return []

    results: list[SearchResult] = []
    matcher = _Matcher(query)
    blob_filter, blob_params = _like_filter("data", matcher)

    for hash_dir in cursor_chats.iterdir():
        if not hash_dir.is_dir():
            continue
        for session_dir in hash_dir.iterdir():
            store_db = session_dir / "store.db"
            if not store_db.is_file():
                continue

            try:
                conn = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
                cur = conn.cursor()

                # Extract project path from the blob containing "Workspace Path:"
                project_path = ""
                cur.execute(
                    "SELECT data FROM blobs"
                    " WHERE data LIKE '%Workspace Path:%' ESCAPE '!'"
                    " LIMIT 1",
                )
                for (blob_data,) in cur.fetchall():
                    try:
                        text = (
                            blob_data.decode("utf-8")
                            if isinstance(blob_data, bytes)
                            else str(blob_data)
                        )
                        obj = json.loads(text)
                        if isinstance(obj, dict):
                            content = obj.get("content", "")
                            if isinstance(content, str):
                                m = re.search(r"Workspace Path: ([^\n]+)", content)
                                if m:
                                    project_path = m.group(1).strip()
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                        pass

                # Search for matching blobs using LIKE to pre-filter in C
                matched_text = ""
                cur.execute(
                    f"SELECT data FROM blobs WHERE {blob_filter}", blob_params,
                )
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
                        if not isinstance(obj, dict):
                            continue

                        content = obj.get("content", "")
                        if isinstance(content, str):
                            content_text = content
                        elif isinstance(content, list):
                            parts = []
                            for item in content:
                                if isinstance(item, dict):
                                    bt = item.get("type", "")
                                    if bt in ("text", "reasoning"):
                                        t = item.get("text", "")
                                        if t:
                                            parts.append(t)
                                    elif bt == "tool-call":
                                        args = item.get("args", {})
                                        if args:
                                            parts.append(json.dumps(args))
                                    elif bt == "tool-result":
                                        r = item.get("result", "")
                                        if r:
                                            parts.append(_stringify_value(r))
                            content_text = "\n".join(parts)
                        else:
                            content_text = ""

                        if content_text and matcher.matches(content_text):
                            matched_text = matcher.snippet(content_text)
                            break
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                        continue

                conn.close()

                if matched_text:
                    results.append(SearchResult(
                        session_id=session_dir.name,
                        project_path=project_path,
                        provider=Provider.CURSOR,
                        matched_line=matched_text,
                        file_path=str(store_db),
                        host=host,
                    ))

            except (sqlite3.Error, OSError):
                continue

    return results


def _opencode_part_candidates(obj: dict) -> list[str]:
    """Pull searchable text out of an opencode part/message/session JSON."""
    candidates: list[str] = []
    text = obj.get("text", "")
    if isinstance(text, str) and text:
        candidates.append(text)
    title = obj.get("title", "")
    if isinstance(title, str) and title:
        candidates.append(title)
    state = obj.get("state")
    if isinstance(state, dict):
        inp = state.get("input")
        if inp:
            candidates.append(_stringify_value(inp))
        for key in ("output", "error"):
            val = state.get(key, "")
            if isinstance(val, str) and val:
                candidates.append(val)
    return candidates


def _opencode_session_info_path(storage: Path, session_id: str) -> Path | None:
    """Locate ``storage/session/{projectID}/{session_id}.json``."""
    if not session_id:
        return None
    return next(storage.glob(f"session/*/{session_id}.json"), None)


def _search_opencode_storage(
    rg: str,
    query: str,
    opencode_data: Path,
    host: str | None,
) -> list[SearchResult]:
    """Search the legacy opencode JSON storage tree via ripgrep."""
    storage = opencode_data / "storage"
    if not storage.is_dir():
        return []

    cmd = [
        rg, "--json", "-i", "-m", "1",
        *(("-F",) if _is_literal(query) else ()),
        "--glob", "*.json",
        query,
        str(storage),
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return []

    results: list[SearchResult] = []
    seen: set[str] = set()
    matcher = _Matcher(query)

    for line in proc.stdout.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") != "match":
            continue

        match_data = data.get("data", {})
        file_path = match_data.get("path", {}).get("text", "")
        matched_text = match_data.get("lines", {}).get("text", "").strip()
        if not file_path or not matched_text:
            continue

        fp = Path(file_path)
        try:
            rel = fp.relative_to(storage)
        except ValueError:
            continue
        kind = rel.parts[0] if rel.parts else ""
        if kind not in ("session", "message", "part"):
            continue

        # Opencode JSON files are pretty-printed; load the file to get IDs.
        try:
            with open(fp) as f:
                obj = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue

        if kind == "session":
            session_id = obj.get("id") or fp.stem
        else:
            session_id = obj.get("sessionID", "")
            if not session_id and kind == "message":
                session_id = fp.parent.name
            if not session_id and kind == "part" and len(rel.parts) == 4:
                # Older nested layout: part/{sessionID}/{messageID}/{partID}.json
                session_id = rel.parts[1]
        if not session_id or session_id in seen:
            continue

        # Resolve the project path and staged-undo cutoff from session info.
        project_path = ""
        info_path = (
            fp if kind == "session"
            else _opencode_session_info_path(storage, session_id)
        )
        info: object = None
        if info_path is not None:
            info = obj if kind == "session" else None
            if info is None:
                try:
                    with open(info_path) as f:
                        info = json.load(f)
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    info = None
            if isinstance(info, dict):
                project_path = info.get("directory", "") or ""

        revert = _parse_revert(info.get("revert")) if isinstance(info, dict) else None
        if kind == "message":
            message_id = obj.get("id") or fp.stem
            if not _part_is_active(message_id, None, revert):
                continue
        elif kind == "part":
            message_id = obj.get("messageID")
            if not message_id:
                if len(rel.parts) == 3:  # part/{messageID}/{partID}.json
                    message_id = rel.parts[1]
                elif len(rel.parts) == 4:  # part/{sessionID}/{messageID}/{partID}.json
                    message_id = rel.parts[2]
            part_id = obj.get("id") or fp.stem
            if not _part_is_active(message_id, part_id, revert):
                continue

        # Each file is one part/message/session object; a file matching only
        # in ids or paths is skipped without claiming the session, so another
        # of its parts can still produce the hit.
        content_text = matcher.best(_opencode_part_candidates(obj))
        if content_text:
            display_text = matcher.snippet(content_text)
        elif matcher.verified:
            continue
        else:
            display_text = matcher.snippet(matched_text) or matched_text[:200]
        seen.add(session_id)

        results.append(SearchResult(
            session_id=session_id,
            project_path=project_path,
            provider=Provider.OPENCODE,
            matched_line=display_text,
            file_path=str(info_path) if info_path is not None else file_path,
            host=host,
        ))

    return results


def _search_opencode_db(
    query: str,
    opencode_data: Path,
    host: str | None,
) -> list[SearchResult]:
    """Search opencode SQLite databases via LIKE.

    v2 (``session_message``) rows are searched first; v1 ``part`` rows only
    for sessions with no v2 copy, since a migrated DB keeps both.
    """
    results: list[SearchResult] = []
    matcher = _Matcher(query)

    for db_path in _opencode_db_paths(opencode_data):
        seen: set[str] = set()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            tables = _opencode_db_tables(conn)
            if _opencode_has_v2(tables):
                results.extend(_search_opencode_db_v2(
                    conn, matcher, db_path, host, seen,
                ))
            if not _opencode_has_v1(tables) or "part" not in tables:
                conn.close()
                continue
            cur = conn.cursor()
            columns = {
                row[1] for row in cur.execute("PRAGMA table_info(session)").fetchall()
            }
            revert_column = "s.revert" if "revert" in columns else "NULL"
            # Child (sub-agent) sessions are not listed as sessions, so their
            # hits are dropped, as in v2.
            root_filter = (
                " AND s.parent_id IS NULL" if "parent_id" in columns else ""
            )
            data_filter, data_params = _like_filter("p.data", matcher)
            cur.execute(
                "SELECT p.session_id, p.message_id, p.id, p.data,"
                f" s.directory, {revert_column} FROM part p"
                " LEFT JOIN session s ON s.id = p.session_id"
                f" WHERE {data_filter}{root_filter}"
                " ORDER BY p.session_id, p.message_id, p.id",
                data_params,
            )
            for (
                session_id,
                message_id,
                part_id,
                part_data,
                directory,
                raw_revert,
            ) in cur:
                if not session_id or session_id in seen:
                    continue
                if not _part_is_active(
                    message_id, part_id, _parse_revert(raw_revert)
                ):
                    continue
                try:
                    obj = json.loads(part_data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                content_text = matcher.best(_opencode_part_candidates(obj))
                if not content_text:
                    continue
                seen.add(session_id)
                results.append(SearchResult(
                    session_id=session_id,
                    project_path=directory or "",
                    provider=Provider.OPENCODE,
                    matched_line=matcher.snippet(content_text),
                    file_path=str(db_path),
                    host=host,
                ))
            conn.close()
        except (sqlite3.Error, OSError):
            continue

    return results


def _search_opencode_db_v2(
    conn: sqlite3.Connection,
    matcher: _Matcher,
    db_path: Path,
    host: str | None,
    seen: set[str],
) -> list[SearchResult]:
    """Search opencode 2.0 ``session_message`` rows.

    Every v2 session ID lands in *seen* (hit or not) so the v1 pass skips
    the stale pre-migration copies. Child (sub-agent) sessions are not
    listed as sessions, so their hits are dropped.
    """
    results: list[SearchResult] = []
    seen.update(row[0] for row in conn.execute("SELECT id FROM session_v2"))
    cutoffs = _opencode_v2_cutoffs(conn)
    data_filter, data_params = _like_filter("m.data", matcher)
    rows = conn.execute(
        "SELECT m.session_id, m.seq, m.type, m.data, s.directory"
        " FROM session_message m JOIN session_v2 s ON s.id = m.session_id"
        f" WHERE {data_filter} AND s.parent_id IS NULL"
        " ORDER BY m.session_id, m.seq",
        data_params,
    ).fetchall()
    matched: set[str] = set()
    for session_id, seq, mtype, data, directory in rows:
        if session_id in matched:
            continue
        if not _opencode_v2_is_active(seq, cutoffs.get(session_id)):
            continue
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        content_text = matcher.best(
            _message_texts(_v2_message_to_messages(mtype, obj))
        )
        if not content_text:
            continue
        matched.add(session_id)
        results.append(SearchResult(
            session_id=session_id,
            project_path=directory or "",
            provider=Provider.OPENCODE,
            matched_line=matcher.snippet(content_text),
            file_path=str(db_path),
            host=host,
        ))
    return results


def _find_codex_root_rollout(sessions_dir: Path, root_id: str) -> str | None:
    """Find a root rollout by first-line id without scanning transcript bodies."""
    try:
        files = sessions_dir.rglob("*.jsonl")
        for file_path in files:
            payload = _read_codex_session_header(file_path)
            if (
                payload.get("id") == root_id
                and not _is_codex_subagent_header(payload)
            ):
                return str(file_path)
    except OSError:
        pass
    return None


def _read_codex_session_header(file_path: str | Path) -> dict:
    """Return a Codex rollout's authoritative first-line metadata payload."""
    try:
        with open(file_path) as f:
            first = json.loads(f.readline())
        payload = first.get("payload")
        if first.get("type") == "session_meta" and isinstance(payload, dict):
            return payload
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return {}


def _is_codex_subagent_header(payload: dict) -> bool:
    """Whether a Codex session_meta payload identifies a child rollout."""
    source = payload.get("source")
    return payload.get("thread_source") == "subagent" or (
        isinstance(source, dict) and isinstance(source.get("subagent"), dict)
    )


def _codex_root_id_from_header(payload: dict) -> str:
    """Extract a child rollout's root id across current header variants."""
    root_id = payload.get("session_id") or payload.get("parent_thread_id")
    if root_id:
        return str(root_id)
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    return str(spawn.get("parent_thread_id") or "") if isinstance(spawn, dict) else ""


def _search_one_host(
    rg: str,
    query: str,
    roots: _SearchRoots,
    cwd_lookup: dict[tuple[str, str], str] | None = None,
) -> list[SearchResult]:
    """Run all per-host searches and return tagged SearchResults."""
    matcher = _Matcher(query)
    search_paths = []
    if roots.claude_projects.is_dir():
        search_paths.append(str(roots.claude_projects))
    if roots.codex_sessions.is_dir():
        search_paths.append(str(roots.codex_sessions))
    if roots.copilot_sessions.is_dir():
        search_paths.append(str(roots.copilot_sessions))
    if roots.pi_sessions.is_dir():
        search_paths.append(str(roots.pi_sessions))

    results: list[SearchResult] = []
    seen_sessions: set[str] = set()
    file_cwd_cache: dict[str, str] = {}
    codex_header_cache: dict[str, dict] = {}
    codex_root_path_cache: dict[str, str] = {}

    if search_paths:
        cmd = [
            rg, "--json", "-i", "-m", "1",
            *(("-F",) if _is_literal(query) else ()),
            "--glob", "*.jsonl",
            query,
            *search_paths,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError):
            proc = None

        if proc is not None:
            for line in proc.stdout.splitlines():
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("type") != "match":
                    continue

                match_data = data.get("data", {})
                file_path = match_data.get("path", {}).get("text", "")
                matched_text = match_data.get("lines", {}).get("text", "").strip()

                if not file_path or not matched_text:
                    continue

                # The reported line is only the FIRST raw match in the file.
                # Keep the hit only if the query appears in that record's
                # message text; otherwise look further down the same file for
                # a record where it does, and drop the file if there is none.
                entry: dict = {}
                try:
                    parsed = json.loads(matched_text)
                    if isinstance(parsed, dict):
                        entry = parsed
                except json.JSONDecodeError:
                    pass
                content_text = matcher.best(_entry_texts(entry)) if entry else ""
                if not content_text and matcher.verified:
                    found = _first_text_match(
                        file_path, matcher,
                        after_line=match_data.get("line_number") or 0,
                    )
                    if found is None:
                        continue
                    entry, content_text = found

                # Determine provider from path
                if "/.claude/" in file_path:
                    provider = Provider.CLAUDE
                elif "/.codex/" in file_path:
                    provider = Provider.CODEX
                elif "/.copilot/" in file_path:
                    provider = Provider.COPILOT
                elif "/.pi/" in file_path:
                    provider = Provider.PI
                else:
                    provider = Provider.CLAUDE

                # Codex child linkage lives only in the first-line session_meta,
                # not necessarily in the matched record.  Always classify the
                # matched file from that authoritative header: older indexes
                # contain child rollout ids and cannot reliably identify roots.
                codex_agent_id: str | None = None
                codex_is_child = False
                codex_filename_id = (
                    _extract_codex_session_id(file_path)
                    if provider == Provider.CODEX else ""
                )
                if provider == Provider.CODEX:
                    header = _read_codex_session_header(file_path)
                    codex_header_cache[file_path] = header
                    codex_is_child = _is_codex_subagent_header(header)
                    if codex_is_child:
                        codex_agent_id = str(
                            header.get("id") or codex_filename_id or ""
                        ) or None

                # Try to extract sessionId from the matched JSONL line
                session_id = ""
                try:
                    session_id = entry.get("sessionId", "") or ""
                    if not session_id:
                        payload_id = entry.get("payload", {}).get("id", "")
                        # Only use payload.id from session_meta entries (not message IDs)
                        if payload_id and entry.get("type") == "session_meta":
                            session_id = payload_id
                except AttributeError:
                    pass

                # Codex child hits belong to the root session, not the child
                # rollout id. Ordinary rollouts still fall back to the filename.
                if provider == Provider.CODEX:
                    header = codex_header_cache.get(file_path, {})
                    if codex_is_child:
                        session_id = _codex_root_id_from_header(header) or session_id
                    else:
                        session_id = str(
                            header.get("id") or session_id or codex_filename_id
                        )

                # For Copilot, session ID is the directory name (UUID)
                if not session_id and provider == Provider.COPILOT:
                    session_id = Path(file_path).parent.name

                # For pi, the session header is the only line carrying the
                # session id; fall back to the trailing UUID in the filename.
                if not session_id and provider == Provider.PI:
                    if entry.get("type") == "session" and entry.get("id"):
                        session_id = entry["id"]
                    else:
                        session_id = _extract_codex_session_id(file_path)

                # Claude sub-agent transcripts (agent-*.jsonl): the record's
                # sessionId is the PARENT session — attribute the hit there and
                # tag agent_id so downstream knows it's a "phantom" sub-agent
                # match. Prefer the record's own agentId; fall back to filename.
                agent_id: str | None = codex_agent_id
                if provider == Provider.CLAUDE and _is_claude_agent_file(file_path):
                    aid = ((entry.get("agentId") or "") if entry else "")
                    agent_id = (aid or _agent_id_from_filename(file_path)) or None
                    # A leading fork-context-ref record carries no sessionId
                    # but a parentSessionId; and older forks none at all, so
                    # derive the parent id from the current-layout directory.
                    if not session_id and entry:
                        session_id = entry.get("parentSessionId", "") or ""
                    if not session_id:
                        session_id = _agent_parent_session_from_path(Path(file_path))

                # Deduplicate by session — skip cwd lookup and content
                # extraction for matches we've already seen.
                dedup_key = f"{session_id}:{file_path}" if session_id else file_path
                if dedup_key in seen_sessions:
                    continue
                seen_sessions.add(dedup_key)

                # Extract project_path (cwd) for session resume.
                # Fallback chain: entry field → index → file cache → file I/O
                project_path = ""
                if entry:
                    project_path = entry.get("cwd", "") or ""
                    if not project_path:
                        project_path = entry.get("payload", {}).get("cwd", "") or ""

                if not project_path and cwd_lookup and session_id:
                    project_path = cwd_lookup.get((session_id, provider.value), "")

                if not project_path and file_path in file_cwd_cache:
                    project_path = file_cwd_cache[file_path]

                if not project_path and provider == Provider.CODEX:
                    project_path = codex_header_cache.get(file_path, {}).get("cwd", "") or ""
                    if project_path:
                        file_cwd_cache[file_path] = project_path

                if not project_path and provider == Provider.PI:
                    try:
                        with open(file_path) as f:
                            for raw in f:
                                stripped = raw.strip()
                                if not stripped:
                                    continue
                                first = json.loads(stripped)
                                project_path = first.get("cwd", "") or ""
                                break
                        if project_path:
                            file_cwd_cache[file_path] = project_path
                    except (OSError, json.JSONDecodeError, AttributeError):
                        pass

                if not project_path and provider == Provider.COPILOT:
                    from sesh.providers.copilot import _parse_workspace_yaml
                    yaml_path = Path(file_path).parent / "workspace.yaml"
                    meta = _parse_workspace_yaml(yaml_path)
                    project_path = meta.get("cwd", "")
                    if project_path:
                        file_cwd_cache[file_path] = project_path

                # Display window around the match in the message text. Only an
                # unverifiable regex (see _Matcher) reaches here without one.
                display_text = matcher.snippet(content_text) or matched_text[:200]

                root_file_path = None
                if provider == Provider.CODEX and codex_agent_id and session_id:
                    if session_id not in codex_root_path_cache:
                        codex_root_path_cache[session_id] = _find_codex_root_rollout(
                            roots.codex_sessions, session_id
                        ) or ""
                    root_file_path = codex_root_path_cache[session_id] or None

                results.append(SearchResult(
                    session_id=session_id,
                    project_path=project_path,
                    provider=provider,
                    matched_line=display_text,
                    file_path=file_path,
                    host=roots.host,
                    agent_id=agent_id,
                    root_file_path=root_file_path,
                ))

    # Cursor search: transcripts (.txt) and store.db files
    cursor_seen: set[str] = set()

    cursor_transcripts = _search_cursor_transcripts(
        rg, query, roots.cursor_projects, roots.host,
    )
    for r in cursor_transcripts:
        cursor_seen.add(r.session_id)
        results.append(r)

    cursor_stores = _search_cursor_stores(query, roots.cursor_chats, roots.host)
    for r in cursor_stores:
        if r.session_id not in cursor_seen:
            results.append(r)

    # Gemini search: pretty-printed JSON session files (separate rg pass)
    results.extend(_search_gemini(rg, query, roots.gemini_tmp, roots.host))

    # Cline search: one JSON transcript per session dir (separate rg pass)
    results.extend(_search_cline(rg, query, roots.cline_sessions, roots.host))

    # agy search: one JSONL transcript per conversation dir (separate rg pass)
    results.extend(_search_agy(rg, query, roots.agy_data, roots.host))

    # opencode: SQLite databases first, then the legacy JSON storage tree
    opencode_seen: set[str] = set()
    for r in _search_opencode_db(query, roots.opencode_data, roots.host):
        opencode_seen.add(r.session_id)
        results.append(r)
    for r in _search_opencode_storage(rg, query, roots.opencode_data, roots.host):
        if r.session_id not in opencode_seen:
            results.append(r)

    return results


def ripgrep_search(
    query: str,
    aggregation_root: Path | None = None,
    cwd_lookup: dict[tuple[str, str], str] | None = None,
) -> list[SearchResult]:
    """Run ripgrep across session files and return search results.

    If *aggregation_root* is set, scan each per-host subdirectory under it
    (one rg invocation per host) and tag each result with the host name.
    Local-mode behaviour (aggregation_root=None) is unchanged.

    *cwd_lookup*, when provided, maps ``(session_id, provider_value)`` to
    ``project_path``.  It is consulted before falling back to file I/O for
    cwd resolution.
    """
    rg = shutil.which("rg")
    if not rg:
        return []

    if aggregation_root is None:
        roots_list = [_local_roots()]
    else:
        roots_list = list(_aggregated_roots(aggregation_root))

    results: list[SearchResult] = []
    if len(roots_list) <= 1:
        for roots in roots_list:
            results.extend(_search_one_host(rg, query, roots, cwd_lookup=cwd_lookup))
    else:
        with ThreadPoolExecutor(max_workers=len(roots_list)) as pool:
            futures = [
                pool.submit(_search_one_host, rg, query, r, cwd_lookup)
                for r in roots_list
            ]
            for f in as_completed(futures):
                results.extend(f.result())

    return results


def _aware(ts: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC so results from any provider compare."""
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _file_mtime(file_path: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(Path(file_path).stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def rank_results(results: list[SearchResult]) -> list[SearchResult]:
    """Order results: title matches first, then most recently active first.

    Results with no timestamp sort last within their group; the sort is
    stable, so ties keep their incoming order.
    """
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    def key(r: SearchResult):
        return (
            0 if "title" in r.matched_in else 1,
            -(_aware(r.timestamp) or oldest).timestamp() if r.timestamp else float("inf"),
        )

    return sorted(results, key=key)


def search_sessions(
    query: str,
    sessions: list[SessionMeta],
    *,
    aggregation_root: Path | None = None,
    cwd_lookup: dict[tuple[str, str], str] | None = None,
    titles_only: bool = False,
) -> list[SearchResult]:
    """Search session titles and transcript text, joined to *sessions* and ranked.

    *sessions* is the session index. A content hit (from `ripgrep_search`) is
    given its session's title and timestamp; a session whose title matches but
    whose transcript produced no hit is added as a title-only result with an
    empty ``matched_line``. ``matched_in`` records which of "title" /
    "content" matched. With *titles_only*, transcripts are not searched at
    all (no ripgrep needed). Ordering is `rank_results`.
    """
    matcher = _Matcher(query)
    by_key = {(s.provider, s.id, s.host): s for s in sessions}

    def title_matches(session: SessionMeta) -> bool:
        # An unverifiable regex accepts any text; it must not match every title.
        return matcher.verified and matcher.matches(session.summary or "")

    results: list[SearchResult] = []
    if not titles_only:
        results = ripgrep_search(
            query, aggregation_root=aggregation_root, cwd_lookup=cwd_lookup,
        )

    covered: set[tuple[Provider, str, str | None]] = set()
    for r in results:
        key = (r.provider, r.session_id, r.host)
        covered.add(key)
        session = by_key.get(key)
        if session is None:
            r.timestamp = _file_mtime(r.root_file_path or r.file_path)
            continue
        r.title = session.summary
        r.timestamp = _aware(session.timestamp)
        if not r.project_path:
            r.project_path = session.project_path
        if title_matches(session):
            r.matched_in = ("title", "content")

    for key, session in by_key.items():
        if key in covered or not title_matches(session):
            continue
        results.append(SearchResult(
            session_id=session.id,
            project_path=session.project_path,
            provider=session.provider,
            matched_line="",
            file_path=session.source_path or "",
            host=session.host,
            title=session.summary,
            timestamp=_aware(session.timestamp),
            matched_in=("title",),
        ))

    return rank_results(results)
