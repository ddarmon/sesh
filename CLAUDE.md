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

## Versioning

The package version lives in both `pyproject.toml` and
`src/sesh/__init__.py`. Keep them in sync.

Every PR merged to `main` that includes a bug fix or feature should bump
the version:

-   bug fix: patch version
-   feature: minor version

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

| Provider | Path                               | Format     |
| -------- | ---------------------------------- | ---------- |
| Claude   | `~/.claude/projects/{encoded}/`    | JSONL      |
| Codex    | `~/.codex/sessions/YYYY/MM/DD/`    | JSONL      |
| Cursor   | `~/.cursor/chats/{md5}/*/store.db` | SQLite     |
| Copilot  | `~/.copilot/session-state/{uuid}/` | YAML+JSONL |
| pi       | `~/.pi/agent/sessions/{encoded}/`  | JSONL      |
| Gemini   | `~/.gemini/tmp/{dir}/chats/`       | JSON       |
| opencode | `~/.local/share/opencode/`         | SQLite+JSON |

The pi encoded directory wraps the cwd with leading and trailing `--`
(e.g. `/Users/me/proj` -\> `--Users-me-proj--`). Each session is one
JSONL file named `{ISO-timestamp}_{uuid}.jsonl`; the first line is a
`type:"session"` header carrying the cwd. The provider always recovers
the real cwd from that header, never from the encoded folder name.

The Gemini CLI `{dir}` component is either SHA-256 of the project cwd or
a friendly name assigned in `~/.gemini/projects.json` (a `{path: name}`
mapping). Each session is one pretty-printed JSON file named
`session-{YYYY-MM-DDTHH-MM}-{shortid}.json` carrying `sessionId`,
`projectHash`, `startTime`, `lastUpdated`, `messages`, and an optional
`summary`. The hash is not invertible, so the provider resolves real
paths through `projects.json` (by name and by hashing each known path);
unresolvable hash dirs fall back to the tmp directory path with a
`gemini:{hash8}` display name. Because the files are single JSON
documents (not JSONL), they are parsed with `json.load` on demand
rather than line-by-line.

opencode has two on-disk formats, both supported by the provider
(sessions found in SQLite take precedence over the same ID in JSON):

-   **SQLite** (current opencode, 2026+): `opencode.db` /
    `opencode-{channel}.db` in the data dir, with `session`, `message`,
    and `part` tables. `message.data` / `part.data` are JSON columns
    holding the V1 message/part payloads.
-   **Legacy JSON storage** (2025-era):
    `storage/session/{projectID}/{sessionID}.json` (session info),
    `storage/message/{sessionID}/{messageID}.json` (message info), and
    `storage/part/{messageID}/{partID}.json` (content parts; the older
    nested `storage/part/{sessionID}/{messageID}/` layout is also
    read).

The opencode project path comes from the session's `directory` field,
never from project IDs or folder names.

App-managed files follow XDG base directories (absolute `XDG_*` env vars
are honored; empty/relative values fall back to defaults):

-   cache files (`sessions.json`, `index.json`, `project_paths.json`):
    `~/.cache/sesh/` or `$XDG_CACHE_HOME/sesh/`
-   config files (`preferences.json`, `bookmarks.json`):
    `~/.config/sesh/` or `$XDG_CONFIG_HOME/sesh/`

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
-   **Copilot**: `copilot --resume=<session-id>`
-   **pi**: `pi --session <session-id>`
-   **Gemini**: `gemini --resume <session-id>` (runs in the project
    directory --- Gemini's resume is scoped to the cwd's project).
    Requires a recent Gemini CLI (verified on 0.46; 0.29 only accepted
    a per-project index or `latest`). Sessions whose project path could
    not be resolved (`gemini:{hash8}` fallback) are not resumable ---
    there is no real cwd to run the command in
-   **opencode**: `opencode --session <session-id>`

If the CLI binary isn't on PATH, the status bar shows an error.

## Tool and thinking visibility

Press `t` to toggle tool call/result messages in the message viewer.
Press `T` (shift-t) to toggle thinking/reasoning blocks. Both are hidden
by default. The status bar shows `Tools:ON` / `Think:ON` when active.
Press `F` (shift-f) to toggle fullscreen mode for the message pane; the
status bar shows `Full:ON` when active. Press `?` to open the keyboard
shortcuts help modal (press `?` or Escape again to close).

CLI equivalents: `--include-tools`, `--include-thinking`, `--full`
(both) on the `messages` and `export` subcommands. These toggles, along
with the provider filter and sort mode, persist across launches in
`~/.config/sesh/preferences.json` by default (or
`$XDG_CONFIG_HOME/sesh/preferences.json`) (managed by `preferences.py`).

## Token usage

Session tree labels show a compact token count (e.g., `18K tok`,
`1.7M tok`) when available. Token data is extracted during session
discovery and cached alongside other metadata. Per-provider sources:

-   **Claude**: last assistant message's `usage` for `input_tokens`
    (context size of the final turn, including cache variants); sums
    `output_tokens` across all turns
-   **Codex**: last `token_count` event ---
    `last_token_usage.input_tokens` for context size;
    `total_token_usage.output_tokens` for cumulative output
-   **Copilot**: `session.shutdown` event's `modelMetrics` (sums
    `inputTokens` + `cacheReadTokens` + `cacheWriteTokens` across all
    models for input; `outputTokens` for output)
-   **pi**: per-assistant-message `usage` blocks. `input_tokens` is the
    LAST turn's `input + cacheRead + cacheWrite`; `output_tokens` sums
    `output` across turns; `cumulative_input_tokens` sums per-turn
    `input + cacheRead + cacheWrite` across the whole session
-   **Gemini**: per-`gemini`-message `tokens` blocks. `input_tokens` is
    the LAST turn's `input` (which already includes cached tokens);
    `output_tokens` sums `output + thoughts` across turns;
    `cumulative_input_tokens` sums per-turn `input` across the session
-   **opencode**: per-assistant-message `tokens` blocks
    (`input`/`output`/`cache.read`/`cache.write`). `input_tokens` is the
    LAST turn's `input + cache.read + cache.write`; `output_tokens` sums
    `output` across turns. In the SQLite format the session row's
    cumulative `tokens_*` columns provide `output_tokens` and
    `cumulative_input_tokens` directly
-   **Cursor**: no token data available

The `sesh sessions` CLI output includes three token fields:
`input_tokens` (last turn context size), `output_tokens` (total output),
and `cumulative_input_tokens` (sum of all turns' inputs, useful for cost
estimation). Markdown exports include both **Context** and
**Cumulative** token lines in the header when data is present. The TUI
tree label shows only the context-size total.

## Session export

Press `e` in the TUI to export the current session as Markdown to the
system clipboard. The export respects the current tool/thinking
visibility toggles. The formatting logic lives in `export.py` and is
shared by the TUI and the `sesh export` CLI subcommand.

On the CLI, `sesh export` writes to stdout by default; pass
`-o/--output FILE` to write the export to a file (UTF-8; `md`, `json`,
and `html` formats). On success the command prints a small JSON
confirmation (`{"exported": {...}}`) to stdout instead of the transcript.

## HTML rendering (`export --format html` + `view`)

`sesh export --format html` and the `sesh view <id>` convenience
subcommand render a session as a **self-contained HTML page** with
Markdown, syntax highlighting, and **LaTeX** math (`$…$`, `$$…$$`,
`\(…\)`, `\[…\]`). `format_session_html(session, messages)` in
`export.py` emits the whole document; `cmd_view` writes it to a secure
temp file (`tempfile.mkstemp`, mode 0600), prints the path, and opens it
in the browser unless `--no-open`. Both honor `--include-tools` /
`--include-thinking` / `--full`. `cmd_view` discovers fresh via
`_refresh_index` (like `delete`/`clean`) rather than `_require_index`, so
a just-created session — including `last` — is viewable without a manual
`sesh refresh`; discovery is incremental via the on-disk cache so an
unchanged tree costs only stats.

The renderer (KaTeX, markdown-it + markdown-it-texmath, highlight.js) is
**vendored** under `src/sesh/viewer_assets/` (so it ships in the wheel —
top-level `assets/` is not packaged) and **inlined** into the output, so
the file works offline from `file://` with no network. KaTeX's woff2
fonts are base64-inlined into `katex.min.css` for the same reason (which
is why that file is ~360 KB vs. ~23 KB upstream). Assets are substituted
into the template in a single `re.sub` pass (not chained `str.replace`)
because a vendored file can itself contain a placeholder token like
`__DATA__`; the embedded message JSON escapes `</` so it cannot terminate
the data `<script>` early. All vendored libs are MIT/BSD-3 licensed;
upstream license texts live in `viewer_assets/LICENSES/` and versions/
sources are tracked in `viewer_assets/README.md`.

## Bookmarks

Pressing `b` on a session node toggles a bookmark. Bookmarked sessions
show a star in the tree and appear in a dedicated Bookmarks section at
the top. Bookmarks persist across sessions in
`~/.config/sesh/bookmarks.json` by default (or
`$XDG_CONFIG_HOME/sesh/bookmarks.json`).

CLI equivalents:

-   `sesh bookmarks` --- list bookmarked sessions as JSON, joined
    against the index for metadata; bookmarks whose sessions are no
    longer in the index are still listed with `"in_index": false`
-   `sesh sessions --bookmarked` --- filter the sessions list to
    bookmarked sessions only

Bookmarks are local-mode state, so both are disabled in aggregation
mode (like the TUI's `b` binding).

## Session deletion

Pressing `d` on a session node shows a confirmation dialog. On confirm,
the session is deleted via the provider's `delete_session` method:

-   **Claude**: removes matching `sessionId` lines from JSONL files
-   **Codex**: deletes the session JSONL file
-   **Cursor**: removes the session directory (parent of `store.db`)
-   **Copilot**: removes the session directory
-   **pi**: deletes the session JSONL file
-   **Gemini**: deletes the session JSON file
-   **opencode**: deletes the session/message/part rows from the
    SQLite DB, or the session JSON plus its message/part files in the
    legacy storage tree

CLI equivalents:

-   `sesh delete <session-id>` --- delete a single session by ID
-   `sesh delete last` --- delete the most recently active session (the
    one with the newest `timestamp` in the index); scope it to one
    provider with `--provider`
-   `sesh clean <query>` --- delete all sessions matching a search query

Both commands require interactive confirmation by default. In
non-interactive contexts (piped stdin, LLM agents), they refuse unless
`--force` is passed. Use `--dry-run` to preview without deleting.

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

Gemini sessions are not covered by project move: the format stores the
cwd only as a SHA-256 `projectHash` inside every session file, so a move
would require rewriting whole JSON documents plus `projects.json`.

## CLI subcommands (JSON output)

All subcommands output JSON to stdout. Run `sesh refresh` first to build
the index, then query it.

| Command                                                                                                   | Description                              |
| --------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `sesh`                                                                                                    | Launch the TUI (default, no subcommand)  |
| `sesh refresh`                                                                                            | Discover sessions and rebuild the index  |
| `sesh projects`                                                                                           | List projects from the index             |
| `sesh sessions [--project PATH] [--provider NAME] [--since DATE] [--until DATE] [--limit N] [--bookmarked]` | List sessions with optional filters      |
| `sesh stats [--project PATH] [--provider NAME]`                                                           | Aggregate session statistics from the index |
| `sesh messages <id\|last> [--limit N] [--offset N] [--summary] [--include-tools] [--include-thinking] [--full]` | Load messages for a session              |
| `sesh search <query> [--provider NAME] [--project PATH]`                                                  | Full-text search (Claude, Codex, Cursor) |
| `sesh bookmarks`                                                                                          | List bookmarked sessions (joined with the index) |
| `sesh delete <id\|last> [--provider NAME] [--force] [--dry-run]`                                          | Delete a session by ID, or the most recent with `last` |
| `sesh clean <query> [--force] [--dry-run]`                                                                | Delete sessions matching a search query  |
| `sesh resume <id\|last> [--provider NAME]`                                                                | Resume a session in its provider's CLI   |
| `sesh export <id\|last> [--provider NAME] [--format md/json/html] [-o FILE] [--include-tools] [--include-thinking] [--full]` | Export a session to Markdown, JSON, or HTML |
| `sesh view <id\|last> [--provider NAME] [--include-tools] [--include-thinking] [--full] [--no-open]` | Render a session as HTML and open it in the browser |
| `sesh move <old> <new> [--metadata-only] [--dry-run]`                                                     | Move project path and update metadata    |
| `sesh snapshot save`                                                                                      | Capture Terminal.app tabs (macOS only)   |
| `sesh snapshot list`                                                                                      | List stored snapshots                    |
| `sesh snapshot show <id>`                                                                                 | Print full snapshot JSON                 |
| `sesh snapshot reopen <id> [--all] [--dry-run]`                                                           | Reopen Terminal tabs from a snapshot     |
| `sesh snapshot delete <id> [--force] [--dry-run]`                                                         | Delete a stored snapshot                 |

The index is stored at `~/.cache/sesh/index.json` by default (or
`$XDG_CACHE_HOME/sesh/index.json`).

`messages`, `resume`, `export`, `view`, and `delete` all accept the
literal `last` in place of a session ID, resolving to the most recently active
session (the newest `timestamp` in the index); `--provider` scopes
`last` to one provider. `sesh sessions --since/--until` accept ISO
dates or datetimes (e.g. `2026-06-01`); timezone-naive values are
treated as UTC, and the bounds are inclusive. `--limit N` sorts by
timestamp descending before slicing, so it returns the N newest
sessions.

## Terminal tab snapshots

`Shift+S` in the TUI (or `sesh snapshot save` on the CLI) captures the
state of every open Terminal.app tab --- its window/tab index, working
directory, scrollback tail, and the resume command for any coding-agent
session running in that tab. `sesh snapshot reopen <id>` respawns those
tabs, each one running its resume command in its original CWD. Snapshots
are macOS-only in v1; the gate lives in
`sesh.snapshots.backend.get_backend()` (returns `None` off Darwin).

Layout:

-   `src/sesh/resume.py` -- shared resume-command mapping
    (`RESUME_COMMANDS`, `is_resumable`, `resume_argv`,
    `resume_binary_available`). Used by the CLI, the TUI, and the
    snapshot subsystem. PATH-presence is decoupled from resumability so
    capture can persist `cmd_args` even when the CLI isn't installed at
    snapshot time.
-   `src/sesh/snapshots/core.py` -- backend-agnostic dataclasses and
    JSON I/O (`Snapshot`, `SnapshotTab`, `SnapshotResume`,
    `RestorePlan`, `RestoreReport`), `capture` / `save` / `load` /
    `list_snapshots` / `delete` / `build_restore_plan` / `restore`, and
    resume-info resolution (explicit-line parsing then ripgrep-based
    search recovery).
-   `src/sesh/snapshots/backend.py` -- `TerminalBackend` Protocol,
    `CapturedTab` / `RestoreOutcome` dataclasses, and `get_backend()`.
-   `src/sesh/snapshots/terminal_app.py` -- Darwin/Terminal.app backend.
    `_run_osascript`, `_resolve_cwd` (via `ps` + `lsof`), and
    AppleScript snippets for capture and restore.

Resume metadata is resolved at **save time**, not at restore time:

1.  Scrollback is scanned for the LAST explicit `claude --resume <id>`,
    `codex resume <id>`, `agent --resume=<id>`, `copilot --resume=<id>`,
    `gemini --resume <uuid>` (full UUIDs only --- `latest` and index
    numbers are not session ids), or `pi --session <id>` line
    (`_parse_explicit_resume`).
2.  If no explicit line is found, distinctive scrollback phrases are fed
    to `sesh.search.ripgrep_search` until one returns a result whose
    `project_path` matches the tab's CWD (`_search_recover`).
    Tie-breaker cascade: index mtime → on-disk file mtime → ripgrep
    result order.

Snapshot files live at `$XDG_DATA_HOME/sesh/snapshots/<id>.json`
(default `~/.local/share/sesh/snapshots/`). Files include
`schema_version` (currently `1`); loads of unsupported versions raise
`SnapshotsSchemaError`.

Tests stub `_run_osascript`, `_resolve_cwd`, and `ripgrep_search`; no
real Terminal.app or `osascript` calls happen in the suite. The
`fake_backend` and `tmp_snapshots_dir` fixtures in `tests/conftest.py`
let cross-platform tests exercise the core, CLI, and TUI paths.

## Aggregation mode (cross-machine browsing)

`sesh` can browse sessions mirrored from multiple machines through a
read-only **aggregation mode**. The user maintains a single tree
containing per-host subtrees (one mirrored `$HOME` per host):

```
$SESH_AGGREGATION_ROOT/
  laptop/
    .claude/projects/...
    .codex/sessions/...
    .pi/agent/sessions/...
  desktop/
    .claude/projects/...
    ...
```

Sync is owned by the user (rsync / Syncthing / Dropbox / etc.) ---
`sesh` does not push or pull. Typical setup on the aggregator machine:

```bash
rsync -a --delete laptop:.claude/  $SESH_AGGREGATION_ROOT/laptop/.claude/
rsync -a --delete laptop:.codex/   $SESH_AGGREGATION_ROOT/laptop/.codex/
rsync -a --delete laptop:.pi/      $SESH_AGGREGATION_ROOT/laptop/.pi/
rsync -a --delete laptop:.local/share/opencode/  $SESH_AGGREGATION_ROOT/laptop/.local/share/opencode/
```

Activate aggregation mode with either:

-   `SESH_AGGREGATION_ROOT=/path` environment variable (ambient default,
    intended for pi-pulse / launchd / cron).
-   `sesh --aggregation-root /path ...` CLI flag (per-invocation
    override).

Behavior in aggregation mode:

-   Local-mode providers are **not** scanned (no double-counting).
-   Every `Project` and `SessionMeta` carries a `host` field
    (`"laptop"`, `"desktop"`, etc., derived from the subdirectory name).
-   `sesh sessions`, `sesh projects`, `sesh stats`, `sesh messages`,
    `sesh export` all include `host` in their JSON output.
-   The TUI tree shows `[host] project-name`; the status bar shows
    `Agg:{N hosts}`.
-   The on-disk index (`~/.cache/sesh/index.json`) is **not**
    overwritten in aggregation mode --- it stays owned by local-mode
    runs. Aggregation queries always rebuild fresh from the source tree.
-   Identical project paths on different hosts stay separate. The
    internal project key is `"{host}::{project_path}"`; consumers should
    use the `host` field on `Project` rather than parsing the key.

Disabled in aggregation mode (would only affect the aggregator's mirror,
which the next rsync overwrites --- and resume needs the source host's
local CLI state anyway):

-   `o` / `sesh resume` --- would launch the local CLI but the source
    state is on the other machine.
-   `b` (bookmarks), `d` / `sesh delete`, `d`+match / `sesh clean`, `m`
    / `sesh move` --- mutations to the mirror don't propagate back.
-   `sesh bookmarks` and `sesh sessions --bookmarked` --- bookmarks are
    local-mode state and refer to sessions on this machine, not to the
    mirrored hosts.

Provider entry-points respect aggregation mode via the new constructor
parameters `base_dir` and `host` (see `src/sesh/providers/*.py`). The
multiplexing layer is in `discovery._discover_aggregated()`.

`sesh search` is aggregation-aware: in aggregation mode it runs one
ripgrep per host subtree and every `SearchResult` carries a `host`
field. The local-mode JSON output also includes `host` (always `null`).

**Caveats for v1:**

-   Cursor IDE sessions need the macOS `~/Library/Application Support/`
    workspace storage path mirrored under `{host}/Library/...` to be
    visible; CLI agent sessions in `{host}/.cursor/chats/` work
    out-of-the-box.
-   opencode lives under `.local/share/opencode` rather than a
    top-level dotdir, so the mirror needs that subpath
    (`{host}/.local/share/opencode/`).

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

## Testing

Tests live in `tests/` and use pytest. Install dev dependencies first:

```bash
uv sync --extra dev
```

Run the full suite:

```bash
uv run pytest -q tests
```

Run only unit tests or integration tests:

```bash
uv run pytest tests/unit
uv run pytest tests/integration
```

**When to run tests:** Run the full suite after any change to source
files under `src/sesh/`. Integration tests marked `requires_rg` need
`rg` on PATH; the Textual smoke tests need a working terminal
environment.

### Test layout

-   `tests/conftest.py` -- shared fixtures that redirect all
    provider/cache paths to `tmp_path` so tests never touch real user
    data
-   `tests/helpers.py` -- JSONL/SQLite fixture factories and model
    constructors (`write_jsonl`, `create_store_db`, `make_session`,
    `make_message`, `make_index`)
-   `tests/unit/` -- fast, isolated tests for models, cache, providers,
    search, CLI commands, app helpers, and move orchestration
-   `tests/integration/` -- tests that exercise CLI JSON endpoints
    end-to-end, run real `rg` searches, or use Textual's pilot API

### Conventions

-   All path-dependent modules are monkeypatched via `conftest.py`
    fixtures (`tmp_cache_dir`, `tmp_claude_dir`, `tmp_codex_dir`,
    `tmp_cursor_dirs`, `tmp_gemini_dir`, `tmp_search_dirs`,
    `tmp_move_dirs`). Always use these instead of touching real
    home-directory paths.
-   An autouse `isolate_app_preferences` fixture stubs
    `load_preferences`/`save_preferences` on the app module so tests
    never read or write real preference files.
-   Prefer deterministic synthetic fixtures over real user data.
-   When adding a new provider, add corresponding test files under
    `tests/unit/` following the existing naming pattern
    (`test_provider_{name}_metadata.py`, `test_provider_{name}_move.py`,
    etc.).

## Dependencies

-   `textual` -- TUI framework (the only runtime dependency)
-   `ripgrep` (`rg`) -- for full-text search (must be on PATH)
-   `pytest`, `pytest-asyncio` -- test framework (dev dependency only)
