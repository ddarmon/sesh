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

Version bumps happen **once per release, not per PR**. Individual
feature/fix PRs should NOT bump the version on their own — the
in-progress version on `main` stays put until a release is cut. When
cutting a release, bump the version to cover everything merged to `main`
since the last `vX.Y.Z` tag, then tag it:

-   patch: only bug fixes since the last release
-   minor: at least one feature since the last release

This keeps each released version meaningful and avoids churn (and merge
conflicts) from every PR touching the version line.

### Commit prefixes

PR titles / squash-merge commits on `main` use a bracketed prefix. The
release bump is derived from the prefixes of all commits since the last
`vX.Y.Z` tag:

-   `[FEATURE]` — a feature → the next release is a **minor** bump
-   `[BUGFIX]` — a bug fix → **patch** bump (if no features in the range)
-   `[CHORE]` / `[DOCS]` — chores and docs → no bump on their own

So a release is **minor** if the range contains any `[FEATURE]`,
otherwise **patch** if it contains any `[BUGFIX]`. The bump and the
release tag are produced together at release time (a `/release` workflow
can automate deriving the bump, editing both version files, running the
tests, tagging, and drafting the GitHub release).

## Architecture

The app has three layers:

1.  **Providers** (`src/sesh/providers/`) -- each provider discovers
    projects, lists sessions, and loads messages on demand. All I/O is
    synchronous (run in Textual thread workers).
2.  **App** (`src/sesh/app.py`) -- Textual TUI with a `Tree` widget
    (left pane) and a card-per-message `TranscriptView`
    (`src/sesh/transcript_view.py`) in the right pane, above which a
    compact per-session details header (`app.format_session_header`) is
    shown. Message identity/composition is a pure layer in
    `src/sesh/transcript.py`, shared with the HTML viewer. Background
    discovery runs in a thread via `run_worker(thread=True)`.
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

-   cache files (`sessions.json`, `index.json`, `project_paths.json`)
    and the `views/` HTML view cache (see HTML rendering below):
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
CLI to resume the session. Pressing `v` opens the selected session in the
stable HTML browser viewer; `L` toggles a private live-updating browser view
(see HTML rendering below). Per-provider commands:

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
Press `a` to toggle Claude sub-agent threads (see below); the status bar
shows `Agents:ON` when active. Press `F` (shift-f) to toggle fullscreen
mode for the message pane; the status bar shows `Full:ON` when active.
Press `?` to open the keyboard shortcuts help modal (press `?` or Escape
again to close).

CLI equivalents: `--include-tools`, `--include-thinking`, `--full`
(both) on the `messages` and `export` subcommands; `--no-agents` on
`view`/`export` suppresses sub-agent sections. These toggles (including
`show_agents`), along with the provider filter and sort mode, persist
across launches in `~/.config/sesh/preferences.json` by default (or
`$XDG_CONFIG_HOME/sesh/preferences.json`) (managed by `preferences.py`).

## Transcript reading and navigation

The message pane is `transcript_view.TranscriptView` (a card-per-message
`VerticalScroll`), not a `RichLog`, so nothing is silently truncated.
Identity/composition is provider-neutral and pure in `transcript.py`:

-   **Stable keys** (`transcript.py`). Each message key is
    `{namespace}-{digest}-{occurrence}` — a short SHA-1 over role, content
    type, tool name, timestamp, system flag, and full content, plus a
    per-digest occurrence counter; the visible-list index is never part of
    identity. Keys are stable across appends, tool/thinking/agent toggles,
    chronological sub-agent insertion, and duplicate content. Sub-agent
    interior keys are namespaced by `agent_id`; a sub-agent container's own
    anchor is `agent_anchor(agent_id)` = `agent-{id}`. `compose_transcript`
    splices sub-agent containers in at their spawn timestamp.
    `export.session_html_payload` reuses these exact keys, so TUI and HTML
    share one identity model. Keys are internal viewer ids, not a public
    schema.
-   **Preview / expansion** (`transcript_view.py`). A body longer than
    `PREVIEW_CHARS` (1600) renders a bounded preview plus an omission marker
    (`omission_marker`, e.g. `… 4,280 more characters`). `Enter` on the
    selected card expands it to the complete body (or expands a sub-agent
    container); `Enter` again collapses. The full body always lives in the
    model — only the preview renders until expansion — so a huge transcript
    never renders every full body eagerly. `C` copies the **complete** body
    (`copy_active`), never the preview. Expansion state and the selection
    cursor are keyed by stable key, so they survive `t`/`T`/`a` toggles and
    live rerenders.
-   **Cursor / focus.** The `TranscriptView` container is the single focus
    target (cards are not tab stops); `Tab`/`Shift+Tab` move focus
    tree↔transcript. Within it, `↑`/`↓` (or `j`/`k`), `Home`, and `End`
    move the selection cursor.
-   **Transcript find** (`TranscriptFinder` + `compute_matches`, pure).
    `n` opens/advances find, `N` steps back; in the find input, `Enter`/`↓`
    = next and `Up`/`Shift+Enter` = previous; all wrap around. A `3 / 17`
    counter shows position; the active match is painted distinctly from
    other highlights and scrolled into view. Matching runs over complete
    bodies (tool, thinking, sub-agent included), so a hit past a card's
    preview boundary or inside a collapsed sub-agent is revealed
    (`reveal_match`) rather than hidden. `Esc` closes find and restores
    transcript focus without touching the unrelated session-tree search.
    The active match is preserved by stable key across toggles and live
    appends, and resets gracefully when its card disappears.

### Session details header

`app.format_session_header` (pure) composes the line shown in the
`#message-header` `Static` above the transcript: provider, model, host
(aggregation), full session id, start/end + duration
(`export.format_time_range`), visible message count, sub-agent count,
context/cumulative token totals (`export.token_summary_parts`), and the
resume command when `resume.is_resumable` and the CLI is on PATH. Empty
fields are omitted. The HTML viewer's `export.format_meta_header_html`
mirrors the same fields (with a **Copy ID** button reusing the viewer's
clipboard helper); `export.format_duration` and the two `export`
token/time helpers are shared so both readers agree. `app._format_duration`
is an alias of `export.format_duration`.

## Claude sub-agent transcripts

Claude Code writes each sub-agent (Task/Agent tool) run to a separate
`agent-{id}.jsonl` file. Only the Claude provider handles these.

-   **Discovery API** (`providers/claude.py`): `discover_subagents(session)`
    returns `SubagentMeta` list across four on-disk layouts — current
    per-session `{project}/{sessionId}/subagents/agent-*.jsonl` (with an
    optional `agent-{id}.meta.json` sidecar for type/fork/description/
    toolUseId), Workflow-tool agents one level deeper
    `{project}/{sessionId}/subagents/workflows/{workflowId}/agent-*.jsonl`
    (same sidecar; the `SubagentMeta.workflow_id` field is set, labels carry a
    shortened `[wf_…]` marker), legacy `{project}/subagents/agent-*.jsonl`, and
    oldest `{project}/agent-*.jsonl` (the last two attribute a file to a session
    by the internal parent `sessionId`, probed cheaply from the file head —
    a non-matching legacy file is skipped without a full read). Workflow agents
    exist only under the current per-session layout (no legacy variants); they
    are ordered after the top-level agents, grouped by workflow id, and each
    workflow dir name is gated on the same traversal-safe allowlist as the
    session/agent ids. Agent-file parsing is defensive (non-dict lines / string
    `message` / non-dict `usage` are skipped) and never filters by `sessionId`
    (an agent file is a single-thread transcript, so forks with no internal
    `sessionId` still load). All id-derived filesystem paths are gated on a
    traversal-safe id. `count_subagents` is a cheap directory glob (current
    layout only — top-level plus `workflows/*/agent-*.jsonl`, no file reads)
    used to populate `SessionMeta.subagent_count` during `_parse_sessions` —
    discovery stays lazy, so no agent files are read during index refresh.
-   **Rendering**: sub-agents are turns, not tool calls. `format_session_html`
    / `format_session_markdown` splice each thread in at its spawn timestamp
    (`export._compose_thread`); the TUI message pane does the same via the
    pure `app.splice_subagent_threads` helper (anchor before the first
    visible main-thread message with a later timestamp, else trailing).
    Loading is single-pass and lazy: the provider's
    `load_subagents(session)` reads each agent file once, building meta +
    messages together (`discover_subagents` shares that parse for the
    meta-only path). The TUI renders the main thread immediately on select
    and defers ALL agent-file I/O until the `a` toggle first reveals them
    (`_load_subagents` runs in a worker, then re-renders); a large session
    never blocks on agent parsing while agents are hidden. The collapsed
    block shows regardless of tool/thinking toggles; those toggles govern
    the interior. Tree labels get a `⑂N` badge.
-   **Search attribution**: `SearchResult.agent_id` is set for hits inside
    an `agent-*.jsonl` (including the deeper `subagents/workflows/{wf}/`
    layout, whose parent session is recovered from the directory tree); the
    hit is attributed to the parent session, marked
    `⑂` in TUI search rows, and carried in `sesh search` JSON. `sesh sessions`
    JSON carries `subagent_count`. Opening a `⑂` search hit while sub-agents
    are hidden sets a session-scoped auto-show override (`_agents_override`,
    not persisted; status bar `Agents:AUTO`) so the matched interior renders;
    the override clears when another session is selected.
-   **Delete/move hygiene**: `delete_session` removes the per-session sidecar
    dir (which contains `subagents/`, including `subagents/workflows/`) plus
    legacy agent files matching the session's parent id; `move_project`
    rewrites `cwd` inside agent files across all layouts, including the deeper
    `subagents/workflows/{wf}/agent-*.jsonl`.
-   **Cache caveat**: the sessions-cache directory fingerprint only globs
    top-level `*.jsonl`, so agent files added under `{sessionId}/subagents/`
    without touching a top-level file would not invalidate the cached
    `subagent_count`. In normal operation the parent transcript is appended
    when a sub-agent spawns, so the badge refreshes. Folding sub-agent output
    tokens into `sesh stats` is deferred (it would require reading agent files
    during discovery, breaking the lazy-discovery constraint).

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
`export.py` emits the whole document; `cmd_view` writes it to a **stable
per-session path** (`viewcache.write_view`), prints the path, and opens it
in the browser (`new=0`) unless `--no-open`. Both honor `--include-tools`
/ `--include-thinking` / `--full`. `cmd_view` discovers fresh via
`_refresh_index` (like `delete`/`clean`) rather than `_require_index`, so
a just-created session — including `last` — is viewable without a manual
`sesh refresh`; discovery is incremental via the on-disk cache so an
unchanged tree costs only stats.

The page has a details header (`format_meta_header_html`) and a **sticky
reader toolbar** (`_toolbar_html`). Every message and sub-agent block
carries a DOM `id` = its stable transcript key (see "Transcript reading
and navigation"), so each has an `#anchor`: fragment highlight + reveal on
load/`hashchange`, and open-`<details>` restoration keyed by identity. (A
live poll re-marks the anchored card via `reapplyAnchor` but never
re-opens its `<details>` or scrolls — only a real navigation reveals.)
There are no per-card copy/link buttons; full-body copy is covered by the
TUI's `C` and `sesh export`.
The toolbar's **transcript find** (input + `i / n` counter + prev/next,
`/` focuses, `Enter`/`Shift+Enter` navigate) and message count render in
**both** static and live documents and work from `file://` with no
server; live-only controls are emitted only for live views, so a static
export can never expose the private polling server.

### TUI browser and live views

The TUI's `v` action reloads the selected session through its provider, applies
the current tools/thinking/agents toggles, writes through `viewcache.write_view`,
and opens the stable `file://` URL. `L` starts/stops `liveview.LiveViewServer`
for the selected session. The server binds only to `127.0.0.1` on an ephemeral
port and uses an unguessable path token; it has no permissive CORS, sends
`no-store`/security headers, and stops when the TUI exits. The browser polls its
same-origin JSON endpoint (default 1.5 seconds). Each request reloads via the
normal provider `get_messages` API, so live main-thread updates work for all
providers; Claude sub-agents are reloaded when agent display is enabled. A
failed loader refresh retains the last good payload and the page shows a
retrying/disconnected status. The live toolbar adds a status indicator (live /
paused / retrying / disconnected), the last-update time, a browser-side
**Pause/Resume** (does not stop the server — `L` still owns server lifecycle),
a **Follow** toggle (independent of pause), a manual **Refresh**, and an
`N new ↓` badge when follow is off. A single self-rescheduling timer means polls
never overlap. Rerenders are reconciled by stable key: when the old top-level
keys are a strict prefix of the new ones it appends only the new nodes,
otherwise it does a full rerender preserving open `<details>` by key. Scroll
position is preserved and the page auto-follows to the bottom only when Follow is
on and the reader is already near the bottom. In aggregation mode, live view
follows changes in the mirror rather than connecting to the source host.

### View cache (`viewcache.py`)

The HTML is written to a deterministic path
(`$XDG_CACHE_HOME/sesh/views/{session-id}.html`, default
`~/.cache/sesh/views/`) rather than a random `mkstemp` temp file, so
re-running `sesh view` on the same session reuses the same `file://` URL
and `webbrowser.open(url, new=0)` **refreshes the existing browser tab**
instead of opening a new one. The id is sanitized to a traversal-safe
filename stem (with a hash suffix when sanitizing alters the id or it is
over-long, so distinct ids never collide onto one file).

These files are **pure cache** — always regenerable from the session — so
they can be deleted at any time. The original `mkstemp` security
properties are preserved by *relocating, not randomizing*: the file is
written `0600` via a private `mkstemp` temp file in the views dir (created
`0700` when sesh first makes it) that is then atomically `os.replace`-d
onto the stable path. The atomic rename also makes concurrent same-session
views safe (no truncate/interleave) and means a symlink pre-planted at the
stable path is *replaced*, never written through. Cleanup is opportunistic
and needs no background process:

-   `sweep_view_cache()` runs on every `cmd_view` and deletes any `*.html`
    that is older than `MAX_AGE_DAYS` (7) **or** beyond the `KEEP_NEWEST`
    (50) most-recently-modified files (a file survives only if it is both
    fresh and recent). The two triggers are independent on purpose: age
    ages out view-once sessions, and the count cap *also* bounds the burst
    case (scripting `sesh view` over many sessions leaves many *fresh*
    files no age threshold would touch). Re-viewing rewrites a file (fresh
    mtime), so active sessions survive. `now` is injectable for tests.
-   `remove_view(id)` drops a session's view file when the session itself
    is deleted (wired into `cmd_delete`, `cmd_clean`, and the TUI
    `_delete_session`), so a stale view of a deleted session can't linger.

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
| `sesh doctor [--provider NAME] [--strict] [--human]`                                                       | Read-only provider and dependency diagnostics (JSON or human-friendly text) |
| `sesh projects`                                                                                           | List projects from the index             |
| `sesh sessions [--project PATH] [--provider NAME] [--since DATE] [--until DATE] [--limit N] [--bookmarked]` | List sessions with optional filters      |
| `sesh stats [--project PATH] [--provider NAME]`                                                           | Aggregate session statistics from the index |
| `sesh messages <id\|last> [--limit N] [--offset N] [--summary] [--include-tools] [--include-thinking] [--full]` | Load messages for a session              |
| `sesh search <query> [--provider NAME] [--project PATH]`                                                  | Full-text search (Claude, Codex, Cursor) |
| `sesh bookmarks`                                                                                          | List bookmarked sessions (joined with the index) |
| `sesh delete <id\|last> [--provider NAME] [--force] [--dry-run]`                                          | Delete a session by ID, or the most recent with `last` |
| `sesh clean <query> [--force] [--dry-run]`                                                                | Delete sessions matching a search query  |
| `sesh resume <id\|last> [--provider NAME]`                                                                | Resume a session in its provider's CLI   |
| `sesh export <id\|last> [--provider NAME] [--format md/json/html] [-o FILE] [--include-tools] [--include-thinking] [--full] [--no-agents]` | Export a session to Markdown, JSON, or HTML |
| `sesh view <id\|last> [--provider NAME] [--include-tools] [--include-thinking] [--full] [--no-agents] [--no-open]` | Render a session as HTML and open it in the browser |
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
`last` to one provider. `view`, `messages`, `export`, `delete`, and
`clean` discover fresh via `_refresh_index` before resolving (so a
just-created session — including `last` — needs no manual `sesh
refresh`); discovery is incremental via the on-disk cache. The remaining
read commands (`projects`, `sessions`, `stats`, `bookmarks`) read the
on-disk index via `_require_index` and still require a prior `refresh`. `sesh sessions --since/--until` accept ISO
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
