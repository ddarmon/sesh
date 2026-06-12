"""Full-text search via ripgrep."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from sesh.models import Provider, SearchResult

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CURSOR_PROJECTS = Path.home() / ".cursor" / "projects"
CURSOR_CHATS = Path.home() / ".cursor" / "chats"
COPILOT_SESSIONS = Path.home() / ".copilot" / "session-state"
PI_SESSIONS = Path.home() / ".pi" / "agent" / "sessions"
GEMINI_TMP = Path.home() / ".gemini" / "tmp"
OPENCODE_DATA = Path.home() / ".local" / "share" / "opencode"

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_RG_REGEX_META = re.compile(r'[\\.*+?{}()\[\]|^$]')


def _is_literal(query: str) -> bool:
    """True when *query* contains no regex metacharacters."""
    return not _RG_REGEX_META.search(query)


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


def _extract_content_text(entry: dict, query: str | None = None) -> str:
    """Extract readable message text from a JSONL entry (Claude or Codex).

    Aggregates text from relevant block types and prefers candidates that
    contain the search query so snippets reflect the matched content.
    """
    candidates: list[str] = []

    # Claude format: message.content (list of parts or string)
    msg = entry.get("message", {})
    if msg:
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

        # Codex function_call
        if ptype == "function_call":
            args = payload.get("arguments", "")
            if args:
                candidates.append(args)

        # Codex function_call_output
        elif ptype == "function_call_output":
            output = payload.get("output", "")
            if output:
                candidates.append(_stringify_value(output))

        # Codex agent_reasoning
        elif ptype == "agent_reasoning":
            text = payload.get("text", "")
            if text:
                candidates.append(text)

        # Codex response_item: payload.content (list with output_text/input_text/text)
        pcontent = payload.get("content", [])
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

    if candidates:
        if query:
            q = query.lower()
            query_matches = [c for c in candidates if q in c.lower()]
            if query_matches:
                return max(query_matches, key=len)
        return max(candidates, key=len)
    return ""


def _extract_display_text(content: str, query: str, max_len: int = 200) -> str:
    """Extract a display window around the first match of query in content."""
    if not content:
        return ""

    # Find the query in the content (case-insensitive)
    idx = content.lower().find(query.lower())
    if idx == -1:
        return content[:max_len]

    # Show a window centered on the match
    margin = (max_len - len(query)) // 2
    start = max(0, idx - margin)
    end = start + max_len
    snippet = content[start:end]

    if start > 0:
        snippet = "..." + snippet[3:]
    if end < len(content):
        snippet = snippet[: max_len - 3] + "..."

    return snippet


def _extract_codex_session_id(file_path: str) -> str:
    """Extract the session UUID from a Codex filename."""
    stem = Path(file_path).stem
    matches = _UUID_RE.findall(stem)
    return matches[-1] if matches else stem


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

    from sesh.providers.gemini import read_session_id, resolve_chats_project_path

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

        display_text = _extract_display_text(matched_text, query)
        if not display_text:
            display_text = matched_text[:200]

        results.append(SearchResult(
            session_id=session_id,
            project_path=project_path,
            provider=Provider.GEMINI,
            matched_line=display_text,
            file_path=file_path,
            host=host,
        ))

    return results


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
    like_pattern = f"%{_escape_like(query)}%"
    query_lower = query.lower()

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
                    "SELECT data FROM blobs WHERE data LIKE ? ESCAPE '!'",
                    (like_pattern,),
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

                        if content_text and query_lower in content_text.lower():
                            matched_text = _extract_display_text(
                                content_text, query
                            )
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
    query_lower = query.lower()

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
        seen.add(session_id)

        # Resolve the project path from the session info file.
        project_path = ""
        info_path = (
            fp if kind == "session"
            else _opencode_session_info_path(storage, session_id)
        )
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

        candidates = _opencode_part_candidates(obj)
        content_text = ""
        if candidates:
            matches = [c for c in candidates if query_lower in c.lower()]
            content_text = max(matches, key=len) if matches else max(candidates, key=len)
        display_text = _extract_display_text(content_text, query)
        if not display_text or query_lower not in display_text.lower():
            raw_display = _extract_display_text(matched_text, query)
            if raw_display and query_lower in raw_display.lower():
                display_text = raw_display
            elif not display_text:
                display_text = matched_text[:200]

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
    """Search opencode SQLite databases (part content) via LIKE."""
    if not opencode_data.is_dir():
        return []

    results: list[SearchResult] = []
    like_pattern = f"%{_escape_like(query)}%"
    query_lower = query.lower()

    for db_path in sorted(opencode_data.glob("opencode*.db")):
        if not db_path.is_file():
            continue
        seen: set[str] = set()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT p.session_id, p.data, s.directory FROM part p"
                " LEFT JOIN session s ON s.id = p.session_id"
                " WHERE p.data LIKE ? ESCAPE '!'",
                (like_pattern,),
            )
            for session_id, part_data, directory in cur:
                if not session_id or session_id in seen:
                    continue
                try:
                    obj = json.loads(part_data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                candidates = _opencode_part_candidates(obj)
                matches = [c for c in candidates if query_lower in c.lower()]
                if not matches:
                    continue
                seen.add(session_id)
                results.append(SearchResult(
                    session_id=session_id,
                    project_path=directory or "",
                    provider=Provider.OPENCODE,
                    matched_line=_extract_display_text(max(matches, key=len), query),
                    file_path=str(db_path),
                    host=host,
                ))
            conn.close()
        except (sqlite3.Error, OSError):
            continue

    return results


def _search_one_host(
    rg: str,
    query: str,
    roots: _SearchRoots,
    cwd_lookup: dict[tuple[str, str], str] | None = None,
) -> list[SearchResult]:
    """Run all per-host searches and return tagged SearchResults."""
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

                # Try to extract sessionId from the matched JSONL line
                session_id = ""
                entry = {}
                try:
                    entry = json.loads(matched_text)
                    session_id = entry.get("sessionId", "") or ""
                    if not session_id:
                        payload_id = entry.get("payload", {}).get("id", "")
                        # Only use payload.id from session_meta entries (not message IDs)
                        if payload_id and entry.get("type") == "session_meta":
                            session_id = payload_id
                except (json.JSONDecodeError, AttributeError):
                    pass

                # For Codex files, fall back to extracting UUID from filename
                if not session_id and provider == Provider.CODEX:
                    session_id = _extract_codex_session_id(file_path)

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
                    try:
                        with open(file_path) as f:
                            first = json.loads(f.readline())
                            project_path = first.get("payload", {}).get("cwd", "") or ""
                        if project_path:
                            file_cwd_cache[file_path] = project_path
                    except (OSError, json.JSONDecodeError, AttributeError):
                        pass

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

                # Extract readable display text
                content_text = _extract_content_text(entry, query) if entry else ""
                display_text = _extract_display_text(content_text, query)
                if not display_text or query.lower() not in display_text.lower():
                    # Content didn't contain the query (match was in metadata/paths);
                    # fall back to a window around the match in the raw JSONL line
                    raw_display = _extract_display_text(matched_text, query)
                    if raw_display and query.lower() in raw_display.lower():
                        display_text = raw_display
                    elif not display_text:
                        display_text = matched_text[:200]

                results.append(SearchResult(
                    session_id=session_id,
                    project_path=project_path,
                    provider=provider,
                    matched_line=display_text,
                    file_path=file_path,
                    host=roots.host,
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
