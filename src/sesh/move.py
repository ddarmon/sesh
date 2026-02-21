"""Project move orchestration across providers."""

from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
from pathlib import Path

from sesh.cache import CACHE_FILE, INDEX_FILE, PROJECT_PATHS_FILE
from sesh.models import MoveReport, Provider, encode_project_path, workspace_uri
from sesh.providers.claude import ClaudeProvider, PROJECTS_DIR
from sesh.providers.codex import CODEX_DIR, CodexProvider
from sesh.providers.cursor import (
    CURSOR_CHATS_DIR,
    CURSOR_PROJECTS_DIR,
    WORKSPACE_STORAGE,
    CursorProvider,
)



def _validate_paths(old_path: str, new_path: str, full_move: bool) -> None:
    old = Path(old_path)
    new = Path(new_path)

    if old_path == new_path:
        raise ValueError("Old path and new path must be different.")

    if full_move:
        if not old.exists():
            raise ValueError(f"Old path does not exist: {old_path}")
        if new.exists():
            raise ValueError(f"New path already exists: {new_path}")
    else:
        if not new.exists():
            raise ValueError(f"New path does not exist (required for metadata-only move): {new_path}")


def _invalidate_caches() -> None:
    for cache_file in (INDEX_FILE, PROJECT_PATHS_FILE, CACHE_FILE):
        cache_file.unlink(missing_ok=True)


def _claude_file_needs_cwd_rewrite(jsonl_file: Path, old_path: str) -> bool:
    try:
        with open(jsonl_file) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if entry.get("cwd") == old_path:
                    return True
    except OSError:
        return False
    return False


def _codex_file_needs_rewrite(jsonl_file: Path, old_path: str) -> bool:
    old_cwd_tag = f"<cwd>{old_path}</cwd>"
    first_nonempty = None

    try:
        with open(jsonl_file) as f:
            for line in f:
                if first_nonempty is None and line.strip():
                    first_nonempty = line.strip()
                if old_cwd_tag in line:
                    return True
    except OSError:
        return False

    if not first_nonempty:
        return False

    try:
        first_entry = json.loads(first_nonempty)
    except json.JSONDecodeError:
        return False

    if first_entry.get("type") != "session_meta":
        return False
    payload = first_entry.get("payload")
    return isinstance(payload, dict) and payload.get("cwd") == old_path


def _cursor_store_db_needs_rewrite(store_db: Path, old_path: str) -> bool:
    try:
        conn = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return False

    try:
        cur = conn.cursor()
        cur.execute("SELECT data FROM blobs")
        for (blob_data,) in cur.fetchall():
            if not blob_data:
                continue
            if isinstance(blob_data, bytes):
                try:
                    text = blob_data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            else:
                text = str(blob_data)
            if old_path in text:
                return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()

    return False


def _dry_run_claude(old_path: str, new_path: str) -> MoveReport:
    old_encoded = encode_project_path(old_path)
    new_encoded = encode_project_path(new_path)
    old_dir = PROJECTS_DIR / old_encoded
    new_dir = PROJECTS_DIR / new_encoded

    if old_dir.is_dir() and new_dir.exists():
        return MoveReport(
            provider=Provider.CLAUDE,
            success=False,
            error=f"Target Claude project directory already exists: {new_dir}",
        )

    dirs_renamed = 1 if old_dir.is_dir() else 0
    scan_dir: Path | None = None
    if old_dir.is_dir():
        scan_dir = old_dir
    elif new_dir.is_dir():
        scan_dir = new_dir

    files_modified = 0
    if scan_dir is not None:
        for jsonl_file in scan_dir.glob("*.jsonl"):
            if jsonl_file.name.startswith("agent-"):
                continue
            if _claude_file_needs_cwd_rewrite(jsonl_file, old_path):
                files_modified += 1

    return MoveReport(
        provider=Provider.CLAUDE,
        success=True,
        files_modified=files_modified,
        dirs_renamed=dirs_renamed,
    )


def _dry_run_codex(old_path: str) -> MoveReport:
    if not CODEX_DIR.is_dir():
        return MoveReport(provider=Provider.CODEX, success=True)

    files_modified = 0
    for jsonl_file in CODEX_DIR.rglob("*.jsonl"):
        if _codex_file_needs_rewrite(jsonl_file, old_path):
            files_modified += 1

    return MoveReport(
        provider=Provider.CODEX,
        success=True,
        files_modified=files_modified,
    )


def _dry_run_cursor(old_path: str, new_path: str) -> MoveReport:
    old_md5 = hashlib.md5(old_path.encode()).hexdigest()
    new_md5 = hashlib.md5(new_path.encode()).hexdigest()
    old_chats_dir = CURSOR_CHATS_DIR / old_md5
    new_chats_dir = CURSOR_CHATS_DIR / new_md5

    old_encoded = encode_project_path(old_path)
    new_encoded = encode_project_path(new_path)
    old_projects_dir = CURSOR_PROJECTS_DIR / old_encoded
    new_projects_dir = CURSOR_PROJECTS_DIR / new_encoded

    conflicts = []
    if old_chats_dir.is_dir() and new_chats_dir.exists():
        conflicts.append(f"target chats directory exists: {new_chats_dir}")
    if old_projects_dir.is_dir() and new_projects_dir.exists():
        conflicts.append(f"target projects directory exists: {new_projects_dir}")
    if conflicts:
        return MoveReport(
            provider=Provider.CURSOR,
            success=False,
            error="; ".join(conflicts),
        )

    dirs_renamed = 0
    if old_chats_dir.is_dir():
        dirs_renamed += 1
    if old_projects_dir.is_dir():
        dirs_renamed += 1

    files_modified = 0
    warnings: list[str] = []

    old_uri = workspace_uri(old_path)
    if WORKSPACE_STORAGE.is_dir():
        for ws_dir in WORKSPACE_STORAGE.iterdir():
            workspace_json = ws_dir / "workspace.json"
            if not workspace_json.is_file():
                continue
            try:
                data = json.loads(workspace_json.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("folder") == old_uri:
                files_modified += 1

    scan_chats_dir: Path | None = None
    if old_chats_dir.is_dir():
        scan_chats_dir = old_chats_dir
    elif new_chats_dir.is_dir():
        scan_chats_dir = new_chats_dir

    if scan_chats_dir and scan_chats_dir.is_dir():
        for store_db in scan_chats_dir.rglob("store.db"):
            try:
                if _cursor_store_db_needs_rewrite(store_db, old_path):
                    files_modified += 1
            except OSError as exc:
                warnings.append(f"{store_db}: {exc}")

    warning_msg = None
    if warnings:
        snippet = "; ".join(warnings[:3])
        if len(warnings) > 3:
            snippet += "; ..."
        warning_msg = f"Best-effort store.db scan had errors: {snippet}"

    return MoveReport(
        provider=Provider.CURSOR,
        success=True,
        files_modified=files_modified,
        dirs_renamed=dirs_renamed,
        error=warning_msg,
    )


def move_project(
    old_path: str,
    new_path: str,
    full_move: bool = True,
    dry_run: bool = False,
) -> list[MoveReport]:
    """Move a project and update provider metadata."""
    _validate_paths(old_path, new_path, full_move=full_move)

    if dry_run:
        return [
            _dry_run_claude(old_path, new_path),
            _dry_run_codex(old_path),
            _dry_run_cursor(old_path, new_path),
        ]

    if full_move:
        new_parent = Path(new_path).parent
        new_parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(old_path, new_path)
        except OSError as exc:
            raise ValueError(f"Failed moving project files: {exc}") from exc

    providers = [
        (Provider.CLAUDE, ClaudeProvider()),
        (Provider.CODEX, CodexProvider()),
        (Provider.CURSOR, CursorProvider()),
    ]

    reports: list[MoveReport] = []
    for provider_name, provider in providers:
        try:
            reports.append(provider.move_project(old_path, new_path))
        except Exception as exc:
            reports.append(MoveReport(
                provider=provider_name,
                success=False,
                error=str(exc),
            ))

    _invalidate_caches()
    return reports
