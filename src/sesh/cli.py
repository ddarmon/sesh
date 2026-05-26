"""CLI entry point for sesh.

Provides JSON subcommands for programmatic access alongside the TUI.

All sesh.* imports are lazy (inside functions) so that ``sesh --help``
and argument parsing stay fast.  Only the stdlib modules needed by the
arg parser are imported at module level.

Workflow:
    sesh refresh          # discover sessions and build the index
    sesh projects         # list projects (from index)
    sesh sessions         # list sessions (from index)
    sesh messages <id>    # read messages for a session
    sesh search <query>   # full-text search via ripgrep
    sesh delete <id>      # delete a single session by ID
    sesh clean <query>    # delete sessions matching a query
    sesh                  # launch the TUI (default)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def _refuse_in_aggregation(args: argparse.Namespace, what: str) -> None:
    """Exit with an error if aggregation mode is active for a destructive op."""
    if _aggregation_root(args) is not None:
        print(
            f"{what} is disabled in aggregation mode. "
            "Run on the source host instead — aggregator changes would be "
            "overwritten by the next sync.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _aggregation_root(args: argparse.Namespace | None = None) -> Path | None:
    """Return the active aggregation root, if any.

    --aggregation-root on the command line wins over SESH_AGGREGATION_ROOT.
    Returns a Path or None; never raises if the path doesn't exist (the
    underlying discovery just yields nothing).
    """
    if args is not None:
        explicit = getattr(args, "aggregation_root", None)
        if explicit:
            return Path(explicit)
    env = os.environ.get("SESH_AGGREGATION_ROOT")
    if env:
        return Path(env)
    return None


def _provider_for_session(session, agg_root: Path | None):
    """Build a provider instance pointed at the right base_dir for a session.

    In local mode (agg_root is None), constructs a default provider.
    In aggregation mode, points the provider at the right per-host subtree
    using session.host.
    """
    from sesh.models import Provider
    from sesh.providers.claude import ClaudeProvider
    from sesh.providers.codex import CodexProvider
    from sesh.providers.copilot import CopilotProvider
    from sesh.providers.cursor import CursorProvider
    from sesh.providers.pi import PiProvider

    base_dir = None
    host = None
    if agg_root is not None and session.host:
        base_dir = agg_root / session.host
        host = session.host

    cls_map = {
        Provider.CLAUDE: ClaudeProvider,
        Provider.CODEX: CodexProvider,
        Provider.CURSOR: CursorProvider,
        Provider.COPILOT: CopilotProvider,
        Provider.PI: PiProvider,
    }
    cls = cls_map.get(session.provider)
    if cls is None:
        return None
    # Codex / Copilot / pi accept cache=; the others don't. Keep it simple:
    # we don't need the cache for one-off message loads / deletes.
    if cls in (CodexProvider, CopilotProvider, PiProvider):
        return cls(base_dir=base_dir, host=host)
    return cls(base_dir=base_dir, host=host)


def _require_index(args: argparse.Namespace | None = None):
    """Load the index or exit with an error.

    In aggregation mode the index is rebuilt fresh on every call rather
    than read from disk — the on-disk index is owned by local mode.
    """
    from sesh.cache import load_index

    if _aggregation_root(args) is not None:
        return _refresh_index(args)

    index = load_index()
    if index is None:
        print("No index found. Run 'sesh refresh' first.", file=sys.stderr)
        raise SystemExit(1)
    return index


def _confirm_destructive(message: str, *, force: bool) -> None:
    """Guard destructive commands behind TTY confirmation or --force."""
    if force:
        return
    if not sys.stdin.isatty():
        print(
            "Refusing to delete in non-interactive mode. "
            "Use --force to bypass confirmation.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        answer = input(f"{message} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        raise SystemExit(1)
    if answer.strip().lower() not in ("y", "yes"):
        print("Aborted.", file=sys.stderr)
        raise SystemExit(1)


def _json_out(obj) -> None:
    """Print JSON to stdout."""
    json.dump(obj, sys.stdout, indent=2)
    print()


def _refresh_index(args: argparse.Namespace | None = None):
    """Run discovery, save the cache and index, and return the index dict.

    In aggregation mode the on-disk index (which is owned by local mode)
    is NOT overwritten — discovery results are returned in-memory only.
    """
    from sesh.cache import SessionCache, load_index, save_index

    from sesh.discovery import discover_all

    agg_root = _aggregation_root(args)
    cache = SessionCache()
    projects, sessions = discover_all(cache=cache, aggregation_root=agg_root)
    cache.save()
    if agg_root is None:
        save_index(projects, sessions)
        return load_index()
    return _build_in_memory_index(projects, sessions)


def _build_in_memory_index(projects, sessions) -> dict:
    """Return the same shape as load_index() without touching disk."""
    from sesh.cache import _session_to_dict

    proj_list = []
    for path, proj in sorted(projects.items()):
        proj_list.append({
            "path": proj.path,
            "display_name": proj.display_name,
            "providers": sorted(p.value for p in proj.providers),
            "session_count": proj.session_count,
            "latest_activity": proj.latest_activity.isoformat() if proj.latest_activity else None,
            "host": proj.host,
        })
    sess_list = []
    for path, sess in sessions.items():
        for s in sess:
            sess_list.append(_session_to_dict(s))
    return {
        "refreshed_at": datetime.now(tz=timezone.utc).isoformat(),
        "projects": proj_list,
        "sessions": sess_list,
    }


def cmd_refresh(args: argparse.Namespace) -> None:
    """Run full discovery and save the index."""
    from sesh.cache import SessionCache, save_index
    from sesh.discovery import discover_all

    cache = SessionCache()
    projects, sessions = discover_all(cache=cache, aggregation_root=_aggregation_root(args))
    cache.save()
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
    index = _require_index(args)
    _json_out(index["projects"])


def cmd_sessions(args: argparse.Namespace) -> None:
    """List sessions from the index, with optional filters."""
    index = _require_index(args)
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
            "input_tokens": s.get("input_tokens"),
            "output_tokens": s.get("output_tokens"),
            "cumulative_input_tokens": s.get("cumulative_input_tokens"),
            "host": s.get("host"),
        })

    _json_out(out)


def _load_session_messages(session_data: dict, args: argparse.Namespace | None = None):
    """Look up a session from index data and load its messages via the provider."""
    from sesh.cache import _dict_to_session

    session = _dict_to_session(session_data)
    agg_root = _aggregation_root(args)
    provider = _provider_for_session(session, agg_root)
    messages = provider.get_messages(session) if provider is not None else []
    return session, messages


def cmd_messages(args: argparse.Namespace) -> None:
    """Load and print messages for a session."""
    index = _require_index(args)

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

    from sesh.models import filter_messages

    _session, messages = _load_session_messages(matches[0], args)

    include_tools = getattr(args, "include_tools", False) or getattr(args, "full", False)
    include_thinking = getattr(args, "include_thinking", False) or getattr(args, "full", False)

    # --summary: only user text messages
    if args.summary:
        messages = [m for m in messages if not m.is_system and m.role == "user" and m.content_type == "text"]
    else:
        messages = filter_messages(
            messages,
            include_tools=include_tools,
            include_thinking=include_thinking,
        )

    total = len(messages)

    # Apply offset and limit
    messages = messages[args.offset : args.offset + args.limit]

    out_messages = []
    for m in messages:
        entry = {
            "role": m.role,
            "content": m.content,
            "content_type": m.content_type,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        }
        if m.tool_name:
            entry["tool_name"] = m.tool_name
        if m.tool_input:
            entry["tool_input"] = m.tool_input
        if m.tool_output:
            entry["tool_output"] = m.tool_output
        if m.thinking:
            entry["thinking"] = m.thinking
        out_messages.append(entry)

    _json_out({
        "total": total,
        "offset": args.offset,
        "limit": args.limit,
        "messages": out_messages,
    })


def _build_cwd_lookup() -> dict[tuple[str, str], str] | None:
    """Build a (session_id, provider) → project_path lookup from the index."""
    from sesh.cache import load_index

    index = load_index()
    if not index:
        return None
    lookup: dict[tuple[str, str], str] = {}
    for s in index.get("sessions", []):
        sid = s.get("id", "")
        prov = s.get("provider", "")
        pp = s.get("project_path", "")
        if sid and prov and pp:
            lookup[(sid, prov)] = pp
    return lookup or None


def cmd_search(args: argparse.Namespace) -> None:
    """Full-text search via ripgrep."""
    from sesh.search import ripgrep_search

    cwd_lookup = _build_cwd_lookup()
    results = ripgrep_search(
        args.query,
        aggregation_root=_aggregation_root(args),
        cwd_lookup=cwd_lookup,
    )

    out = []
    for r in results:
        out.append({
            "session_id": r.session_id,
            "provider": r.provider.value,
            "project_path": r.project_path,
            "matched_line": r.matched_line,
            "file_path": r.file_path,
            "host": r.host,
        })

    _json_out(out)


def cmd_clean(args: argparse.Namespace) -> None:
    """Delete sessions matching a search query."""
    _refuse_in_aggregation(args, "clean")

    from sesh.models import Provider, SessionMeta
    from sesh.search import ripgrep_search

    results = ripgrep_search(args.query, cwd_lookup=_build_cwd_lookup())

    if not results:
        _json_out({"deleted": [], "total": 0, "dry_run": args.dry_run})
        return

    # Deduplicate targets before confirmation so the count is accurate.
    targets: list[tuple[dict, str]] = []
    seen_targets: set[tuple[str, str, str]] = set()

    for r in results:
        if r.provider == Provider.CLAUDE:
            source_path = str(Path(r.file_path).parent)
        elif r.provider == Provider.CODEX:
            source_path = r.file_path
        elif r.provider == Provider.CURSOR:
            source_path = r.file_path
        elif r.provider == Provider.COPILOT:
            source_path = str(Path(r.file_path).parent)
        elif r.provider == Provider.PI:
            source_path = r.file_path
        else:
            continue

        dedup_key = (r.provider.value, r.session_id, source_path)
        if dedup_key in seen_targets:
            continue
        seen_targets.add(dedup_key)

        entry = {
            "session_id": r.session_id,
            "provider": r.provider.value,
            "file_path": r.file_path,
            "matched_line": r.matched_line,
            "project_path": r.project_path,
            "source_path": source_path,
        }
        targets.append((entry, source_path))

    if not args.dry_run:
        n = len(targets)
        _confirm_destructive(
            f"Delete {n} session(s) matching '{args.query}'?",
            force=args.force,
        )

    from sesh.providers.claude import ClaudeProvider
    from sesh.providers.codex import CodexProvider
    from sesh.providers.copilot import CopilotProvider
    from sesh.providers.cursor import CursorProvider
    from sesh.providers.pi import PiProvider

    providers_map = {
        Provider.CLAUDE: ClaudeProvider(),
        Provider.CODEX: CodexProvider(),
        Provider.CURSOR: CursorProvider(),
        Provider.COPILOT: CopilotProvider(),
        Provider.PI: PiProvider(),
    }

    deleted = []
    errors = []

    for entry, source_path in targets:
        out_entry = {
            "session_id": entry["session_id"],
            "provider": entry["provider"],
            "file_path": entry["file_path"],
            "matched_line": entry["matched_line"],
        }

        if args.dry_run:
            deleted.append(out_entry)
            continue

        provider_enum = Provider(entry["provider"])
        session = SessionMeta(
            id=entry["session_id"],
            project_path=entry["project_path"],
            provider=provider_enum,
            summary="",
            timestamp=datetime.now(tz=timezone.utc),
            source_path=source_path,
        )

        provider = providers_map.get(provider_enum)
        if provider is None:
            continue

        try:
            provider.delete_session(session)
            deleted.append(out_entry)
        except Exception as exc:
            out_entry["error"] = str(exc)
            errors.append(out_entry)

    out: dict = {
        "deleted": deleted,
        "total": len(deleted),
        "dry_run": args.dry_run,
    }
    if errors:
        out["errors"] = errors

    _json_out(out)


def cmd_delete(args: argparse.Namespace) -> None:
    """Delete a single session by ID."""
    _refuse_in_aggregation(args, "delete")
    index = _refresh_index(args)

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

    if len(matches) > 1:
        providers = ", ".join(sorted(set(m["provider"] for m in matches)))
        print(
            f"Session '{args.session_id}' exists in multiple providers: {providers}. "
            "Use --provider to disambiguate.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    session_data = matches[0]

    info = {
        "session_id": session_data["id"],
        "provider": session_data["provider"],
        "project_path": session_data["project_path"],
        "summary": session_data["summary"],
        "timestamp": session_data["timestamp"],
    }

    if args.dry_run:
        _json_out({"would_delete": info, "dry_run": True})
        return

    _confirm_destructive(
        f"About to delete session:\n"
        f"  Provider:  {session_data['provider']}\n"
        f"  Project:   {session_data['project_path']}\n"
        f"  Summary:   {session_data['summary']}\n"
        f"  Timestamp: {session_data['timestamp']}\n"
        f"Delete this session?",
        force=args.force,
    )

    from sesh.cache import _dict_to_session
    from sesh.models import Provider
    from sesh.providers.claude import ClaudeProvider
    from sesh.providers.codex import CodexProvider
    from sesh.providers.copilot import CopilotProvider
    from sesh.providers.cursor import CursorProvider
    from sesh.providers.pi import PiProvider

    providers_map = {
        Provider.CLAUDE: ClaudeProvider(),
        Provider.CODEX: CodexProvider(),
        Provider.CURSOR: CursorProvider(),
        Provider.COPILOT: CopilotProvider(),
        Provider.PI: PiProvider(),
    }

    session = _dict_to_session(session_data)
    provider = providers_map.get(session.provider)

    if provider is None:
        print(
            f"Unknown provider '{session.provider.value}'.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        provider.delete_session(session)
    except Exception as exc:
        print(f"Delete failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

    _json_out({"deleted": info})


def cmd_resume(args: argparse.Namespace) -> None:
    """Resume a session in its provider's CLI."""
    index = _require_index(args)

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

    from sesh.cache import _dict_to_session
    from sesh.resume import is_resumable, resume_argv, resume_binary_name
    session = _dict_to_session(session_data)

    if not is_resumable(session):
        if session.host is not None:
            print(
                f"Session from host '{session.host}' is not resumable locally "
                "(run on the source host instead).",
                file=sys.stderr,
            )
        else:
            print(
                "Cursor IDE sessions cannot be resumed from the CLI.",
                file=sys.stderr,
            )
        raise SystemExit(1)

    cmd_args = resume_argv(session.provider, session.id)
    binary = resume_binary_name(session.provider)
    binary_path = shutil.which(binary)
    if binary_path is None:
        print(
            f"'{binary}' not found on PATH. "
            f"Install it to resume {session.provider.value} sessions.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    os.chdir(session.project_path)
    os.execvp(binary_path, cmd_args)


def cmd_export(args: argparse.Namespace) -> None:
    """Export a session to Markdown or JSON."""
    index = _require_index(args)

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

    from sesh.models import filter_messages

    session, messages = _load_session_messages(matches[0], args)

    include_tools = getattr(args, "include_tools", False) or getattr(args, "full", False)
    include_thinking = getattr(args, "include_thinking", False) or getattr(args, "full", False)

    messages = filter_messages(
        messages,
        include_tools=include_tools,
        include_thinking=include_thinking,
    )

    if args.output_format == "json":
        out_messages = []
        for m in messages:
            entry = {
                "role": m.role,
                "content": m.content,
                "content_type": m.content_type,
                "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                "tool_name": m.tool_name,
            }
            if m.tool_input:
                entry["tool_input"] = m.tool_input
            if m.tool_output:
                entry["tool_output"] = m.tool_output
            if m.thinking:
                entry["thinking"] = m.thinking
            out_messages.append(entry)

        _json_out({
            "session_id": session.id,
            "provider": session.provider.value,
            "project_path": session.project_path,
            "model": session.model,
            "timestamp": session.timestamp.isoformat(),
            "messages": out_messages,
        })
    else:
        from sesh.export import format_session_markdown

        print(format_session_markdown(session, messages))


def cmd_snapshot_save(args: argparse.Namespace) -> None:
    """Capture the current Terminal state and save it to disk."""
    from sesh import snapshots

    try:
        snap = snapshots.capture()
    except snapshots.SnapshotsUnsupportedError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    path = snapshots.save(snap)
    resumable = sum(1 for t in snap.tabs if t.resume is not None)
    _json_out({
        "id": snap.id,
        "path": str(path),
        "tab_count": len(snap.tabs),
        "resumable": resumable,
    })


def cmd_snapshot_list(args: argparse.Namespace) -> None:
    """Print all stored snapshots as JSON, newest first."""
    from sesh import snapshots

    summaries = snapshots.list_snapshots()
    _json_out([s.to_dict() for s in summaries])


def cmd_snapshot_show(args: argparse.Namespace) -> None:
    """Print the full JSON for one snapshot."""
    from sesh import snapshots

    try:
        snap = snapshots.load(args.snapshot_id)
    except snapshots.SnapshotsNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except snapshots.SnapshotsSchemaError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    _json_out(snap.to_dict())


def cmd_snapshot_reopen(args: argparse.Namespace) -> None:
    """Reopen tabs from a stored snapshot."""
    from sesh import snapshots

    try:
        snap = snapshots.load(args.snapshot_id)
    except snapshots.SnapshotsNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    try:
        report = snapshots.restore(
            snap,
            include_shells=args.all,
            dry_run=args.dry_run,
        )
    except snapshots.SnapshotsUnsupportedError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    _json_out(report.to_dict())


def cmd_snapshot_delete(args: argparse.Namespace) -> None:
    """Remove a stored snapshot."""
    from sesh import snapshots

    try:
        snap = snapshots.load(args.snapshot_id)
    except snapshots.SnapshotsNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    info = {
        "id": snap.id,
        "created_at": snap.created_at,
        "host": snap.host,
        "tab_count": len(snap.tabs),
    }

    if args.dry_run:
        _json_out({"would_delete": info, "dry_run": True})
        return

    _confirm_destructive(
        f"Delete snapshot '{snap.id}' ({len(snap.tabs)} tabs)?",
        force=args.force,
    )

    snapshots.delete(args.snapshot_id)
    _json_out({"deleted": info})


def cmd_move(args: argparse.Namespace) -> None:
    """Move a project and rewrite provider metadata."""
    _refuse_in_aggregation(args, "move")
    from sesh.move import move_project

    old_path = os.path.abspath(os.path.expanduser(args.old_path))
    new_path = os.path.abspath(os.path.expanduser(args.new_path))

    try:
        reports = move_project(
            old_path=old_path,
            new_path=new_path,
            full_move=not args.metadata_only,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    out_reports = []
    had_errors = False
    for report in reports:
        if not report.success:
            had_errors = True
        out_reports.append({
            "provider": report.provider.value,
            "success": report.success,
            "files_modified": report.files_modified,
            "dirs_renamed": report.dirs_renamed,
            "error": report.error,
        })

    _json_out({
        "old_path": old_path,
        "new_path": new_path,
        "full_move": not args.metadata_only,
        "dry_run": args.dry_run,
        "reports": out_reports,
    })

    if had_errors:
        print("One or more providers reported move errors.", file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sesh",
        description=(
            "Browse and search LLM coding sessions (Claude Code, Codex, Cursor, Copilot).\n\n"
            "With no subcommand, launches the interactive TUI.\n"
            "Use subcommands for JSON output suitable for scripts and LLM agents.\n\n"
            "Typical workflow:\n"
            "  sesh refresh            # discover sessions and build the index\n"
            "  sesh projects           # list all projects\n"
            "  sesh sessions           # list all sessions\n"
            "  sesh messages <id>      # read a session's messages\n"
            "  sesh search <query>     # full-text search across sessions\n"
            "  sesh delete <id>        # delete a single session by ID\n"
            "  sesh clean <query>      # delete sessions matching a query\n"
            "  sesh resume <id>        # resume a session in its provider's CLI\n"
            "  sesh export <id>        # export a session to Markdown or JSON\n"
            "  sesh move <old> <new>   # move project path + update metadata\n"
            "  sesh snapshot save      # capture Terminal.app tabs (macOS only)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--aggregation-root",
        metavar="PATH",
        default=None,
        help=(
            "Browse sessions from a multi-host aggregation root instead of $HOME. "
            "Each immediate subdirectory is treated as one host's mirrored "
            "$HOME (.claude/, .codex/, .pi/, ...). Read-only: resume, delete, "
            "clean, and move are disabled. Defaults to $SESH_AGGREGATION_ROOT."
        ),
    )

    sub = parser.add_subparsers(dest="command")

    # refresh
    sub.add_parser(
        "refresh",
        help="Run full discovery across all providers and rebuild the index",
        description=(
            "Scan Claude Code, Codex, Cursor, and Copilot session directories, "
            "then write the index (default: ~/.cache/sesh/index.json, "
            "or $XDG_CACHE_HOME/sesh/index.json) for fast querying. "
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
        choices=["claude", "codex", "cursor", "copilot", "pi"],
        help="Filter to sessions from this provider (claude, codex, cursor, copilot)",
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
        choices=["claude", "codex", "cursor", "copilot", "pi"],
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
    p_messages.add_argument(
        "--include-tools",
        action="store_true",
        help="Include tool call and result messages",
    )
    p_messages.add_argument(
        "--include-thinking",
        action="store_true",
        help="Include thinking/reasoning messages",
    )
    p_messages.add_argument(
        "--full",
        action="store_true",
        help="Include all message types (tools + thinking)",
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
            "Supports Claude, Codex, Cursor, and Copilot sessions."
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
    p_clean.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt (required for non-interactive use)",
    )

    # delete
    p_delete = sub.add_parser(
        "delete",
        help="Delete a single session by ID",
        description=(
            "Delete a session by its ID. Shows a confirmation prompt in interactive "
            "terminals. Non-interactive invocations (piped stdin) are refused unless "
            "--force is passed. Use --dry-run to preview without deleting."
        ),
    )
    p_delete.add_argument(
        "session_id",
        help="The session ID to delete",
    )
    p_delete.add_argument(
        "--provider",
        metavar="NAME",
        choices=["claude", "codex", "cursor", "copilot", "pi"],
        help="Disambiguate if the same ID exists in multiple providers",
    )
    p_delete.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt (required for non-interactive use)",
    )
    p_delete.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )

    # resume
    p_resume = sub.add_parser(
        "resume",
        help="Resume a session in its provider's CLI",
        description=(
            "Look up a session by ID and launch the provider's CLI to resume it. "
            "Replaces the sesh process with the provider CLI (claude, codex, agent, or copilot)."
        ),
    )
    p_resume.add_argument(
        "session_id",
        help="The session ID to resume",
    )
    p_resume.add_argument(
        "--provider",
        metavar="NAME",
        choices=["claude", "codex", "cursor", "copilot", "pi"],
        help="Disambiguate if the same ID exists in multiple providers",
    )

    # export
    p_export = sub.add_parser(
        "export",
        help="Export a session to Markdown or JSON",
        description=(
            "Export all messages from a session to stdout. "
            "System messages are excluded. "
            "Default format is Markdown; use --format json for JSON."
        ),
    )
    p_export.add_argument(
        "session_id",
        help="The session ID to export",
    )
    p_export.add_argument(
        "--provider",
        metavar="NAME",
        choices=["claude", "codex", "cursor", "copilot", "pi"],
        help="Disambiguate if the same ID exists in multiple providers",
    )
    p_export.add_argument(
        "--format",
        dest="output_format",
        choices=["md", "json"],
        default="md",
        help="Output format: md (Markdown, default) or json",
    )
    p_export.add_argument(
        "--include-tools",
        action="store_true",
        help="Include tool call and result messages",
    )
    p_export.add_argument(
        "--include-thinking",
        action="store_true",
        help="Include thinking/reasoning messages",
    )
    p_export.add_argument(
        "--full",
        action="store_true",
        help="Include all message types (tools + thinking)",
    )

    # move
    p_move = sub.add_parser(
        "move",
        help="Move a project and update provider metadata",
        description=(
            "Move a project directory and update Claude, Codex, and Cursor metadata "
            "to point to the new path. Use --metadata-only if files were moved manually. "
            "Use --dry-run to preview changes without writing anything."
        ),
    )
    p_move.add_argument(
        "old_path",
        help="Project path before the move",
    )
    p_move.add_argument(
        "new_path",
        help="Project path after the move",
    )
    p_move.add_argument(
        "--metadata-only",
        action="store_true",
        help="Only rewrite metadata (do not move files on disk)",
    )
    p_move.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying anything",
    )

    # snapshot
    p_snapshot = sub.add_parser(
        "snapshot",
        help="Manage Terminal.app tab snapshots (macOS only)",
        description=(
            "Capture and reopen Terminal.app tabs running coding-agent sessions. "
            "Resume metadata is resolved at capture time so reopens are deterministic."
        ),
    )
    snap_sub = p_snapshot.add_subparsers(dest="snapshot_action", required=True)

    snap_sub.add_parser(
        "save",
        help="Capture a new snapshot of the current Terminal state",
    )

    snap_sub.add_parser(
        "list",
        help="List stored snapshots as JSON (newest first)",
    )

    p_snap_show = snap_sub.add_parser(
        "show",
        help="Print the full JSON for a snapshot",
    )
    p_snap_show.add_argument("snapshot_id", help="Snapshot ID to show")

    p_snap_reopen = snap_sub.add_parser(
        "reopen",
        help="Reopen Terminal tabs from a snapshot",
    )
    p_snap_reopen.add_argument("snapshot_id", help="Snapshot ID to reopen")
    p_snap_reopen.add_argument(
        "--all",
        action="store_true",
        help="Also reopen plain shell tabs (no resumable session)",
    )
    p_snap_reopen.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the restore plan without spawning any tabs",
    )

    p_snap_delete = snap_sub.add_parser(
        "delete",
        help="Delete a stored snapshot",
    )
    p_snap_delete.add_argument("snapshot_id", help="Snapshot ID to delete")
    p_snap_delete.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt (required for non-interactive use)",
    )
    p_snap_delete.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )

    args = parser.parse_args()

    if args.command is None:
        # No subcommand — launch the TUI
        from sesh.app import tui_main
        tui_main(aggregation_root=_aggregation_root(args))
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
    elif args.command == "delete":
        cmd_delete(args)
    elif args.command == "resume":
        cmd_resume(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "move":
        cmd_move(args)
    elif args.command == "snapshot":
        {
            "save": cmd_snapshot_save,
            "list": cmd_snapshot_list,
            "show": cmd_snapshot_show,
            "reopen": cmd_snapshot_reopen,
            "delete": cmd_snapshot_delete,
        }[args.snapshot_action](args)


if __name__ == "__main__":
    main()
