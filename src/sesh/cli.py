"""CLI entry point for sesh.

Provides JSON subcommands for programmatic access alongside the TUI.

Workflow:
    sesh refresh          # discover sessions and build the index
    sesh projects         # list projects (from index)
    sesh sessions         # list sessions (from index)
    sesh messages <id>    # read messages for a session
    sesh search <query>   # full-text search via ripgrep
    sesh clean <query>    # delete sessions matching a query
    sesh                  # launch the TUI (default)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone


def _require_index():
    """Load the index or exit with an error."""
    from sesh.cache import load_index

    index = load_index()
    if index is None:
        print("No index found. Run 'sesh refresh' first.", file=sys.stderr)
        raise SystemExit(1)
    return index


def _json_out(obj) -> None:
    """Print JSON to stdout."""
    json.dump(obj, sys.stdout, indent=2)
    print()


def cmd_refresh(args: argparse.Namespace) -> None:
    """Run full discovery and save the index."""
    from sesh.cache import save_index
    from sesh.discovery import discover_all

    projects, sessions = discover_all()
    save_index(projects, sessions)

    total_sessions = sum(len(s) for s in sessions.values())
    providers = set()
    for sess_list in sessions.values():
        for s in sess_list:
            providers.add(s.provider.value)

    summary = {
        "projects": len(projects),
        "sessions": total_sessions,
        "providers": sorted(providers),
        "refreshed_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    _json_out(summary)


def cmd_projects(args: argparse.Namespace) -> None:
    """List projects from the index."""
    index = _require_index()
    _json_out(index["projects"])


def cmd_sessions(args: argparse.Namespace) -> None:
    """List sessions from the index, with optional filters."""
    index = _require_index()
    sessions = index["sessions"]

    if args.project:
        sessions = [s for s in sessions if s["project_path"] == args.project]
    if args.provider:
        sessions = [s for s in sessions if s["provider"] == args.provider]

    # Strip source_path from output (internal detail)
    out = []
    for s in sessions:
        out.append({
            "id": s["id"],
            "project_path": s["project_path"],
            "provider": s["provider"],
            "summary": s["summary"],
            "timestamp": s["timestamp"],
            "message_count": s["message_count"],
            "model": s["model"],
        })

    _json_out(out)


def cmd_messages(args: argparse.Namespace) -> None:
    """Load and print messages for a session."""
    index = _require_index()

    # Find the session in the index
    matches = [s for s in index["sessions"] if s["id"] == args.session_id]
    if args.provider:
        matches = [s for s in matches if s["provider"] == args.provider]

    if not matches:
        print(
            f"Session '{args.session_id}' not found. "
            "Run 'sesh refresh' to update the index.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    session_data = matches[0]

    # Load messages via provider
    from sesh.cache import _dict_to_session
    session = _dict_to_session(session_data)

    from sesh.models import Provider
    if session.provider == Provider.CLAUDE:
        from sesh.providers.claude import ClaudeProvider
        messages = ClaudeProvider().get_messages(session)
    elif session.provider == Provider.CODEX:
        from sesh.providers.codex import CodexProvider
        messages = CodexProvider().get_messages(session)
    elif session.provider == Provider.CURSOR:
        from sesh.providers.cursor import CursorProvider
        messages = CursorProvider().get_messages(session)
    else:
        messages = []

    # Filter system messages
    messages = [m for m in messages if not m.is_system]

    # --summary: only user messages
    if args.summary:
        messages = [m for m in messages if m.role == "user"]

    total = len(messages)

    # Apply offset and limit
    messages = messages[args.offset : args.offset + args.limit]

    out_messages = []
    for m in messages:
        entry = {
            "role": m.role,
            "content": m.content,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        }
        if m.tool_name:
            entry["tool_name"] = m.tool_name
        out_messages.append(entry)

    _json_out({
        "total": total,
        "offset": args.offset,
        "limit": args.limit,
        "messages": out_messages,
    })


def cmd_search(args: argparse.Namespace) -> None:
    """Full-text search via ripgrep."""
    from sesh.search import ripgrep_search

    results = ripgrep_search(args.query)

    out = []
    for r in results:
        out.append({
            "session_id": r.session_id,
            "provider": r.provider.value,
            "matched_line": r.matched_line,
            "file_path": r.file_path,
        })

    _json_out(out)


def cmd_clean(args: argparse.Namespace) -> None:
    """Delete sessions matching a search query."""
    from pathlib import Path

    from sesh.models import Provider, SessionMeta
    from sesh.search import ripgrep_search

    results = ripgrep_search(args.query)

    if not results:
        _json_out({"deleted": [], "total": 0, "dry_run": args.dry_run})
        return

    from sesh.providers.claude import ClaudeProvider
    from sesh.providers.codex import CodexProvider

    providers_map = {
        Provider.CLAUDE: ClaudeProvider(),
        Provider.CODEX: CodexProvider(),
    }

    deleted = []
    errors = []

    for r in results:
        if r.provider == Provider.CLAUDE:
            source_path = str(Path(r.file_path).parent)
        elif r.provider == Provider.CODEX:
            source_path = r.file_path
        else:
            continue

        entry = {
            "session_id": r.session_id,
            "provider": r.provider.value,
            "file_path": r.file_path,
            "matched_line": r.matched_line,
        }

        if args.dry_run:
            deleted.append(entry)
            continue

        session = SessionMeta(
            id=r.session_id,
            project_path=r.project_path,
            provider=r.provider,
            summary="",
            timestamp=datetime.now(tz=timezone.utc),
            source_path=source_path,
        )

        provider = providers_map.get(r.provider)
        if provider is None:
            continue

        try:
            provider.delete_session(session)
            deleted.append(entry)
        except Exception as exc:
            entry["error"] = str(exc)
            errors.append(entry)

    out: dict = {
        "deleted": deleted,
        "total": len(deleted),
        "dry_run": args.dry_run,
    }
    if errors:
        out["errors"] = errors

    _json_out(out)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sesh",
        description=(
            "Browse and search LLM coding sessions (Claude Code, Codex, Cursor).\n\n"
            "With no subcommand, launches the interactive TUI.\n"
            "Use subcommands for JSON output suitable for scripts and LLM agents.\n\n"
            "Typical workflow:\n"
            "  sesh refresh            # discover sessions and build the index\n"
            "  sesh projects           # list all projects\n"
            "  sesh sessions           # list all sessions\n"
            "  sesh messages <id>      # read a session's messages\n"
            "  sesh search <query>     # full-text search across sessions\n"
            "  sesh clean <query>      # delete sessions matching a query"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command")

    # refresh
    sub.add_parser(
        "refresh",
        help="Run full discovery across all providers and rebuild the index",
        description=(
            "Scan Claude Code, Codex, and Cursor session directories, "
            "then write ~/.cache/sesh/index.json for fast querying. "
            "Run this before other commands, or to pick up new sessions."
        ),
    )

    # projects
    sub.add_parser(
        "projects",
        help="List discovered projects as JSON",
        description="Print all projects from the index as a JSON array.",
    )

    # sessions
    p_sessions = sub.add_parser(
        "sessions",
        help="List sessions as JSON, with optional filters",
        description=(
            "Print sessions from the index as a JSON array. "
            "Use --project or --provider to narrow results."
        ),
    )
    p_sessions.add_argument(
        "--project",
        metavar="PATH",
        help="Filter to sessions for this project path",
    )
    p_sessions.add_argument(
        "--provider",
        metavar="NAME",
        choices=["claude", "codex", "cursor"],
        help="Filter to sessions from this provider (claude, codex, cursor)",
    )

    # messages
    p_messages = sub.add_parser(
        "messages",
        help="Load messages for a session as JSON",
        description=(
            "Load and print messages for a given session ID. "
            "System messages are always excluded. "
            "Use --summary to see only user messages."
        ),
    )
    p_messages.add_argument(
        "session_id",
        help="The session ID to load messages for",
    )
    p_messages.add_argument(
        "--provider",
        metavar="NAME",
        choices=["claude", "codex", "cursor"],
        help="Disambiguate if the same ID exists in multiple providers",
    )
    p_messages.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max messages to return (default: 50)",
    )
    p_messages.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip the first N messages (default: 0)",
    )
    p_messages.add_argument(
        "--summary",
        action="store_true",
        help="Only return user messages (skip assistant and tool messages)",
    )

    # search
    p_search = sub.add_parser(
        "search",
        help="Full-text search across session files via ripgrep",
        description=(
            "Search session files using ripgrep (rg must be on PATH). "
            "Returns matching lines with session and file metadata as JSON."
        ),
    )
    p_search.add_argument(
        "query",
        help="The search term or regex pattern",
    )

    # clean
    p_clean = sub.add_parser(
        "clean",
        help="Delete sessions matching a search query",
        description=(
            "Search for sessions using ripgrep and delete all matches. "
            "Use --dry-run to preview what would be deleted without making changes. "
            "Currently supports Claude and Codex sessions (same scope as 'sesh search')."
        ),
    )
    p_clean.add_argument(
        "query",
        help="The search term or regex pattern to match sessions for deletion",
    )
    p_clean.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )

    args = parser.parse_args()

    if args.command is None:
        # No subcommand — launch the TUI
        from sesh.app import tui_main
        tui_main()
    elif args.command == "refresh":
        cmd_refresh(args)
    elif args.command == "projects":
        cmd_projects(args)
    elif args.command == "sessions":
        cmd_sessions(args)
    elif args.command == "messages":
        cmd_messages(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "clean":
        cmd_clean(args)


if __name__ == "__main__":
    main()
