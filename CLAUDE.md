# sesh

A Python + Textual TUI for browsing LLM coding sessions. Installed via
`uv tool install`.

## Build and run

```bash
# Dev mode
uv run sesh

# Install globally
uv tool install . && sesh
```

## Architecture

The app has three layers:

1.  **Providers** (`src/sesh/providers/`) -- each provider discovers
    projects, lists sessions, and loads messages on demand. All I/O is
    synchronous (run in Textual thread workers).
2.  **App** (`src/sesh/app.py`) -- Textual TUI with a `Tree` widget
    (left pane) and `RichLog` (right pane). Background discovery runs in
    a thread via `run_worker(thread=True)`.
3.  **Cache** (`src/sesh/cache.py`) -- JSON file at
    `~/.cache/sesh/sessions.json` keyed by source file path +
    mtime/size.

## Key conventions

-   Providers are plain classes (not registered in a global list at
    import time). The app instantiates them directly in `_discover_all`.
-   All file I/O in providers uses `open()` and line-by-line iteration.
    No file is loaded fully into memory.
-   Session messages are loaded on demand when a tree node is selected,
    never during discovery.
-   The Claude provider resolves project paths from `cwd` fields in
    JSONL entries, not from the encoded folder name.
-   System messages (commands, reminders, warmup) are tagged
    `is_system=True` and hidden in the message viewer.
-   Each content block in a provider response becomes a separate
    `Message` with a `content_type` field: `"text"`, `"tool_use"`,
    `"tool_result"`, or `"thinking"`. Tool and thinking messages are
    hidden by default; the TUI toggles them with `t`/`T`.

## Data locations

| Provider | Path                               | Format |
| -------- | ---------------------------------- | ------ |
| Claude   | `~/.claude/projects/{encoded}/`    | JSONL  |
| Codex    | `~/.codex/sessions/YYYY/MM/DD/`    | JSONL  |
| Cursor   | `~/.cursor/chats/{md5}/*/store.db` | SQLite |

## Adding a provider

1.  Create `src/sesh/providers/yourprovider.py`.
2.  Subclass `SessionProvider` and implement `discover_projects`,
    `get_sessions`, `get_messages`.
3.  Import and instantiate it in `discovery.discover_all()`.

## Session resume

Pressing `o` on a session node suspends sesh and launches the provider's
CLI to resume the session. Per-provider commands:

-   **Claude**: `claude --resume <session-id>` (runs in the project
    directory)
-   **Codex**: `codex resume <session-id>`
-   **Cursor**: `agent --resume=<session-id>`

If the CLI binary isn't on PATH, the status bar shows an error.

## Tool and thinking visibility

Press `t` to toggle tool call/result messages in the message viewer.
Press `T` (shift-t) to toggle thinking/reasoning blocks. Both are hidden
by default. The status bar shows `Tools:ON` / `Think:ON` when active.

CLI equivalents: `--include-tools`, `--include-thinking`, `--full`
(both) on the `messages` and `export` subcommands.

## Bookmarks

Pressing `b` on a session node toggles a bookmark. Bookmarked sessions
show a star in the tree and appear in a dedicated Bookmarks section at
the top. Bookmarks persist across sessions in
`~/.cache/sesh/bookmarks.json`.

## Session deletion

Pressing `d` on a session node shows a confirmation dialog. On confirm,
the session is deleted via the provider's `delete_session` method:

-   **Claude**: removes matching `sessionId` lines from JSONL files
-   **Codex**: deletes the session JSONL file
-   **Cursor**: removes the session directory (parent of `store.db`)

## Project move

Press `m` on a project or session node to move a project path. The move
dialog supports:

-   **Full Move**: move files on disk and rewrite provider metadata
-   **Metadata Only**: rewrite provider metadata only (for already-moved
    files)

CLI equivalent:

-   `sesh move <old-path> <new-path>`
-   `sesh move <old-path> <new-path> --metadata-only`
-   `sesh move <old-path> <new-path> --dry-run`

## CLI subcommands (JSON output)

All subcommands output JSON to stdout. Run `sesh refresh` first to build
the index, then query it.

| Command                                                                                                   | Description                              |
| --------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `sesh`                                                                                                    | Launch the TUI (default, no subcommand)  |
| `sesh refresh`                                                                                            | Discover sessions and rebuild the index  |
| `sesh projects`                                                                                           | List projects from the index             |
| `sesh sessions [--project PATH] [--provider NAME]`                                                        | List sessions with optional filters      |
| `sesh messages <id> [--limit N] [--offset N] [--summary] [--include-tools] [--include-thinking] [--full]` | Load messages for a session              |
| `sesh search <query>`                                                                                     | Full-text search (Claude, Codex, Cursor) |
| `sesh clean <query> [--dry-run]`                                                                          | Delete sessions matching a search query  |
| `sesh resume <id> [--provider NAME]`                                                                      | Resume a session in its provider's CLI   |
| `sesh export <id> [--provider NAME] [--format md/json] [--include-tools] [--include-thinking] [--full]`   | Export a session to Markdown or JSON     |
| `sesh move <old> <new> [--metadata-only] [--dry-run]`                                                     | Move project path and update metadata    |

The index is stored at `~/.cache/sesh/index.json`.

## Plans

Lightweight execution plans live in `.plans/` to capture the logic of
features, bugfixes, and rollouts. See `.plans/README.md` for full
conventions.

-   `.plans/active/` -- current or paused work
-   `.plans/done/` -- completed work kept for reference
-   Files use sortable names: `YYYY-MM-DD-short-topic.md`
-   Plans include scope, rationale, rollout order, risks, and validation
    commands
-   Put metadata (Status / Type / Owner / Branch / Created / Updated) in
    YAML front matter
-   Prefer importing and normalizing Claude Code plans over hand-writing
    from scratch
-   Update the plan file as decisions change; move to `.plans/done/`
    when complete

## Dependencies

-   `textual` -- TUI framework (the only runtime dependency)
-   `ripgrep` (`rg`) -- for full-text search (must be on PATH)
