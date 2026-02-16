"""Full-text search via ripgrep."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from sesh.models import Provider, SearchResult

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


def ripgrep_search(query: str, max_results: int = 100) -> list[SearchResult]:
    """Run ripgrep across session files and return search results."""
    rg = shutil.which("rg")
    if not rg:
        return []

    search_paths = []
    if CLAUDE_PROJECTS.is_dir():
        search_paths.append(str(CLAUDE_PROJECTS))
    if CODEX_SESSIONS.is_dir():
        search_paths.append(str(CODEX_SESSIONS))

    if not search_paths:
        return []

    cmd = [
        rg, "--json", "-i",
        "--glob", "*.jsonl",
        "-m", str(max_results),
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
        return []

    results: list[SearchResult] = []
    seen_sessions: set[str] = set()

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

        # Try to extract sessionId from the matched JSONL line
        session_id = ""
        try:
            entry = json.loads(matched_text)
            session_id = entry.get("sessionId", "") or entry.get("payload", {}).get("id", "")
        except (json.JSONDecodeError, AttributeError):
            pass

        # Determine provider from path
        if "/.claude/" in file_path:
            provider = Provider.CLAUDE
        elif "/.codex/" in file_path:
            provider = Provider.CODEX
        else:
            provider = Provider.CLAUDE

        # Extract a readable portion of the match
        display_text = matched_text[:200]
        # Try to extract just the message content for readability
        try:
            entry = json.loads(matched_text)
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        display_text = part.get("text", "")[:200]
                        break
            elif isinstance(content, str):
                display_text = content[:200]
        except (json.JSONDecodeError, AttributeError):
            pass

        # Deduplicate by session
        dedup_key = f"{session_id}:{file_path}" if session_id else file_path
        if dedup_key in seen_sessions:
            continue
        seen_sessions.add(dedup_key)

        results.append(SearchResult(
            session_id=session_id,
            project_path="",
            provider=provider,
            matched_line=display_text,
            file_path=file_path,
        ))

        if len(results) >= max_results:
            break

    return results
