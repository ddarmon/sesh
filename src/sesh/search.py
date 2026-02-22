"""Full-text search via ripgrep."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from sesh.models import Provider, SearchResult

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CURSOR_PROJECTS = Path.home() / ".cursor" / "projects"
CURSOR_CHATS = Path.home() / ".cursor" / "chats"

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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


def _decode_cursor_projects_path(encoded: str) -> str:
    """Reverse the Cursor path encoding: 'Users-foo-bar' -> '/Users/foo/bar'.

    Falls back to the raw encoded name if the decoded path doesn't exist.
    """
    decoded = "/" + encoded.replace("-", "/")
    if Path(decoded).is_dir():
        return decoded
    return encoded


def _search_cursor_transcripts(rg: str, query: str) -> list[SearchResult]:
    """Search .txt transcript files in ~/.cursor/projects/ via ripgrep."""
    if not CURSOR_PROJECTS.is_dir():
        return []

    cmd = [
        rg, "--json", "-i",
        "--glob", "*.txt",
        query,
        str(CURSOR_PROJECTS),
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
        # Path structure: ~/.cursor/projects/{encoded}/agent-transcripts/{id}.txt
        encoded_name = fp.parent.parent.name
        project_path = _decode_cursor_projects_path(encoded_name)

        display_text = _extract_display_text(matched_text, query)
        if not display_text:
            display_text = matched_text[:200]

        results.append(SearchResult(
            session_id=session_id,
            project_path=project_path,
            provider=Provider.CURSOR,
            matched_line=display_text,
            file_path=file_path,
        ))

    return results


def _search_cursor_stores(query: str) -> list[SearchResult]:
    """Search store.db files in ~/.cursor/chats/ via SQLite."""
    if not CURSOR_CHATS.is_dir():
        return []

    results: list[SearchResult] = []
    query_lower = query.lower()

    for hash_dir in CURSOR_CHATS.iterdir():
        if not hash_dir.is_dir():
            continue
        for session_dir in hash_dir.iterdir():
            store_db = session_dir / "store.db"
            if not store_db.is_file():
                continue

            try:
                conn = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT data FROM blobs")

                project_path = ""
                matched_text = ""

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

                        # Extract project path from system content
                        content = obj.get("content", "")
                        if isinstance(content, str):
                            m = re.search(r"Workspace Path: ([^\n]+)", content)
                            if m and not project_path:
                                project_path = m.group(1).strip()

                        # Extract text content for matching
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
                            if project_path:
                                break  # Have both match + path
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
                    ))

            except (sqlite3.Error, OSError):
                continue

    return results


def ripgrep_search(query: str) -> list[SearchResult]:
    """Run ripgrep across session files and return search results."""
    rg = shutil.which("rg")
    if not rg:
        return []

    search_paths = []
    if CLAUDE_PROJECTS.is_dir():
        search_paths.append(str(CLAUDE_PROJECTS))
    if CODEX_SESSIONS.is_dir():
        search_paths.append(str(CODEX_SESSIONS))

    results: list[SearchResult] = []
    seen_sessions: set[str] = set()

    if search_paths:
        cmd = [
            rg, "--json", "-i",
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

                # Extract project_path (cwd) for session resume
                project_path = ""
                if entry:
                    project_path = entry.get("cwd", "") or ""
                    if not project_path:
                        project_path = entry.get("payload", {}).get("cwd", "") or ""
                # For Codex, cwd is only in the session_meta (first line); read it
                if not project_path and provider == Provider.CODEX:
                    try:
                        with open(file_path) as f:
                            first = json.loads(f.readline())
                            project_path = first.get("payload", {}).get("cwd", "") or ""
                    except (OSError, json.JSONDecodeError, AttributeError):
                        pass

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

                # Deduplicate by session
                dedup_key = f"{session_id}:{file_path}" if session_id else file_path
                if dedup_key in seen_sessions:
                    continue
                seen_sessions.add(dedup_key)

                results.append(SearchResult(
                    session_id=session_id,
                    project_path=project_path,
                    provider=provider,
                    matched_line=display_text,
                    file_path=file_path,
                ))

    # Cursor search: transcripts (.txt) and store.db files
    cursor_seen: set[str] = set()

    cursor_transcripts = _search_cursor_transcripts(rg, query) if rg else []
    for r in cursor_transcripts:
        cursor_seen.add(r.session_id)
        results.append(r)

    cursor_stores = _search_cursor_stores(query)
    for r in cursor_stores:
        if r.session_id not in cursor_seen:
            results.append(r)

    return results
