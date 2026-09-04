---
Status: done (implemented 2026-09-03)
Type: feature
Owner: David
Branch: feature/cline-provider
Created: 2026-09-03
Updated: 2026-09-03
---

# Cline provider (`next` bundle session store)

## Goal

Add a read-only `cline` provider so sesh can browse, search, export, and
delete sessions written by the Cline VS Code extension's **`next` (SDK)
bundle** under `~/.cline/data/`. No resume (Cline has no resume-by-id CLI).
Follows the Gemini provider as the template: one JSON document per session,
`json.load` on demand.

## Scope

**In scope:** the `next` bundle's store at `~/.cline/data/sessions/`.

**Explicitly out of scope:** the `legacy` bundle's store at
`~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/tasks/`
plus its `state/taskHistory.json` index. A user still on the legacy bundle
gets **no** Cline sessions in sesh, and that is the intended behavior — the
provider must not fall back to the legacy tree, and no legacy parsing code
should be written. Rationale:

-   Cline 4.1.17 ships both bundles behind a staged rollout (`extension.js`
    dispatches on the `ext-sdk-bundle-rollout` flag, `CLINE_BUNDLE_OVERRIDE`,
    or the `cline.rollout.bundleOverride` setting). `next` is the direction of
    travel; legacy is the fallback path.
-   The two formats share nothing: different root, different index, different
    id shape, different message schema, different tool-result encoding.
    Supporting both doubles the provider for a format that is on its way out.
-   Legacy drops Ollama thinking traces entirely (see Findings), so legacy
    transcripts are lossy in a way we cannot repair by parsing harder.

A later plan can add legacy as a second format if it turns out people are
pinned there.

## On-disk format (verified on Cline 4.1.17 / `next` bundle, 2026-09-03)

```
~/.cline/data/                        # $CLINE_DATA_DIR, else $CLINE_DIR/data, else ~/.cline/data
  globalState.json                    # settings (also written by legacy — not an aggregation signal)
  settings/{providers.json, global-settings.json, cline_mcp_settings.json}
  db/sessions.db (+ -wal, -shm)       # SQLite index over the same session records
  sessions/{sessionId}/
    {sessionId}.json                  # session record
    {sessionId}.messages.json         # transcript
  workspaces/{hash8}/workspaceState.json
  workspaces/chat/                    # scratch workspace used by chat-mode sessions
```

Data dir resolution (from `KNn()` in `next/dist/extension.js`):
`CLINE_DATA_DIR` if set and non-empty, else `${CLINE_DIR || ~/.cline}/data`.

Session ids are `{msEpoch}_{suffix}`, e.g. `1788435477952_eibi9` — **not**
bare epochs like the legacy task ids.

### `{sessionId}.json`

| field                     | sesh use                                          |
| ------------------------- | ------------------------------------------------- |
| `session_id`              | `SessionMeta.id`                                  |
| `workspace_root`          | `project_path` (fall back to `cwd`)               |
| `model`                   | `model`                                           |
| `started_at` (ISO)        | `start_timestamp`                                 |
| `updated_at` (ISO)        | `timestamp`                                       |
| `metadata.title`          | `summary` (fall back to `prompt`)                 |
| `metadata.usage`          | token totals (see below)                          |
| `metadata.size`           | part of the cache fingerprint                     |
| `is_subagent`             | filter (see Sub-agents)                           |
| `parent_session_id`       | sub-agent attribution                             |
| `messages_path`           | **ignore** — recompute from the dir (see Design)  |
| `metadata.isFavorited`    | ignore (sesh has its own bookmarks)               |
| `provider` / `source`     | ignore (that is Cline's model provider, not ours) |

Also present and unused in v1: `pid`, `exit_code`, `status`, `status_lock`,
`interactive`, `team_name`, `enable_tools`, `enable_spawn`, `enable_teams`,
`conversation_id`, `agent_id`, `transcript_path`, `hook_path`.

### `{sessionId}.messages.json`

```json
{
  "version": 1,
  "updated_at": "2026-09-03T11:41:07.061Z",
  "agent": "lead",
  "sessionId": "1788435477952_eibi9",
  "origin": {"source": "vscode", "mode": "user", "sessionId": "…", "version": "4.1.17"},
  "messages": [
    {"id": "msg_…", "role": "user", "content": [blocks], "ts": 1788435478090},
    {"id": "msg_…", "role": "assistant", "content": [blocks], "ts": 1788435548065,
     "modelInfo": {"id": "qwen3.8:27b-mlx", "provider": "ollama"},
     "metrics": {"inputTokens": 3088, "outputTokens": 228,
                 "cacheReadTokens": 0, "cacheWriteTokens": 0}}
  ],
  "system_prompt": "You are Cline, an AI coding agent. …"
}
```

Stored block vocabulary, taken from the codec (`M9o` in
`next/dist/extension.js`) rather than guessed from samples:

| block               | shape                                              | sesh `content_type` |
| ------------------- | -------------------------------------------------- | ------------------- |
| `text`              | `{text}`                                            | `text`              |
| `thinking`          | `{thinking, signature?, details?}`                  | `thinking`          |
| `redacted_thinking` | `{data}`                                            | `thinking` (placeholder) |
| `tool_use`          | `{id, name, input, signature?}`                     | `tool_use`          |
| `tool_result`       | `{tool_use_id, name, content, is_error}`            | `tool_result`       |
| `image`             | `{data, mediaType}`                                 | `text` (`[image]`)  |
| `file`              | `{path, content}`                                   | `text`              |
| `media`             | `{media}`                                           | `text` (`[media]`)  |

Two things this format gets right that legacy did not: tool results are real
`tool_result` blocks (no `[tool for 'arg'] Result:` text heuristic), and
`thinking` blocks are actually persisted.

`tool_result.content` is **arbitrary JSON, not necessarily a string** — e.g.
`run_commands` stores `[{query, result, success}, …]`. Render with
`json.dumps` when it is not a `str`.

User turns wrap the prompt: `<user_input mode="act">…</user_input>`. Strip the
wrapper. `[TASK RESUMPTION] Please continue where you left off.` appears
inside that wrapper and should be marked `is_system=True`.

### Token semantics (arithmetic verified against a real session)

For session `1788435477952_eibi9`, per-message `metrics.inputTokens` were
3088 / 3336 / 4936 and `outputTokens` 228 / 667 / 478.

-   `metadata.usage.inputTokens` = 11360 = 3088+3336+4936 → **cumulative**
-   `metadata.usage.outputTokens` = 1373 = 228+667+478 → **cumulative**
-   `metadata.tokensIn` = 8024 = 3088+4936 and `metadata.tokensOut` = 706 =
    228+478 — both **skip the tool-call turn**. Do not use them.

So:

-   `cumulative_input_tokens` = `metadata.usage.inputTokens + cacheReadTokens + cacheWriteTokens`
-   `output_tokens` = `metadata.usage.outputTokens`
-   `input_tokens` (last-turn context) = last assistant message's
    `metrics.inputTokens + cacheReadTokens + cacheWriteTokens` (requires the
    messages file; take from cache when the fingerprint matches, else `None`)

### `db/sessions.db`

One `sessions` table whose columns mirror the `{sessionId}.json` fields
(`session_id`, `source`, `pid`, `started_at`, `ended_at`, `exit_code`,
`status`, `status_lock`, `interactive`, `provider`, `model`, `cwd`,
`workspace_root`, `team_name`, `enable_*`, `parent_session_id`,
`parent_agent_id`, `agent_id`, `conversation_id`, `is_subagent`, `prompt`,
`metadata_json`, `transcript_path`, `hook_path`, `messages_path`,
`updated_at`), plus `subagent_spawn_queue`, `schedules`, and
`schedule_executions`.

**Discovery does not read this database.** It is redundant with the per-session
JSON, it is in WAL mode with a live writer (VS Code holds it open), and the
JSON path keeps discovery to plain `stat`+`json.load` like Gemini. The DB is
touched only by `delete_session`, which must remove the row so the session
does not reappear in Cline's own UI.

## Findings

### Thinking traces (Ollama) — resolved

The `next` bundle records reasoning end to end; verified live, not just read
from source:

1.  Ollama 0.32.15 returns `message.thinking` **by default** for a
    thinking-capable model (`qwen3.8:27b-mlx` reports
    `capabilities: [completion, vision, tools, thinking]`). `think:false`
    suppresses it; the parameter does not need to be sent.
2.  `next`'s stream handler (`processThinking`) turns `message.thinking` into
    `reasoning-start` / `reasoning-delta` with no model-capability gate, which
    becomes `say:"reasoning"` in the UI stream and a stored `thinking` block
    via the codec.
3.  Confirmed on disk: session `1788435477952_eibi9` contains
    `Counter({'text': 6, 'thinking': 3, 'tool_use': 1, 'tool_result': 1})`.

The `legacy` bundle's `OllamaHandler` reads only `message.tool_calls` and
`message.content` in its stream loop, so thinking is discarded before storage
and is unrecoverable from legacy transcripts. This is a second reason legacy
is out of scope.

Two host-side prerequisites, worth recording because they cost an afternoon:
the Ollama base URL must carry a scheme (`http://host:11434` — `next`'s
normalizer appends `/api` but will not add a scheme, unlike ollama-js, so a
bare `host:11434` yields a session with user turns and no assistant output),
and the bundle must actually be `next` (pin with
`cline.rollout.bundleOverride: "next"` if the rollout flag matters).

## Design

### `src/sesh/providers/cline.py`

-   **Root resolution.** `CLINE_DATA_DIR` → `${CLINE_DIR}/data` → `~/.cline/data`,
    matching Cline's own precedence. Constructor takes
    `cache=None, base_dir=None, host=None` like Gemini; in aggregation mode the
    root is `{base_dir}/.cline/data` and the env overrides are **not** consulted
    (they describe this machine, not the mirrored host). Expose `_data_dir` so
    the base `diagnostic_paths()` picks it up for `sesh doctor`.
-   **Session records.** Glob `sessions/*/`, and for each dir read
    `{dirname}.json`. Skip dirs whose name fails a traversal-safe check
    (`^[0-9]+_[A-Za-z0-9]+$`) and dirs whose record is missing, unparseable, or
    carries an unrecognized `version`. Never trust the stored absolute
    `messages_path` — recompute it as `{dir}/{dirname}.messages.json`, because
    in aggregation mode the recorded path points at the source host's
    filesystem.
-   `discover_projects()` — group records by `workspace_root` (fall back to
    `cwd`); yield `(path, basename)`. Sessions whose root is the chat scratch
    workspace (`{data_dir}/workspaces/chat`) are grouped under that path with
    display name `cline:chat`, mirroring Gemini's unresolved-path fallback —
    they are real sessions with no real project.
-   `get_sessions(project_path, cache=None)` — build `SessionMeta` from the
    session record alone. `source_path` = the recomputed messages path.
    `message_count` and `input_tokens` need the messages file; take them from
    the cache when the fingerprint (`updated_at`, `metadata.size`, messages-file
    mtime/size) matches, else parse once.
-   `get_messages(session)` — `json.load` the messages file, iterate
    `messages[*].content` blocks per the table above:
    -   strip the `<user_input mode="…">` wrapper from user text; mark
        `[TASK RESUMPTION] …` blocks `is_system=True`
    -   `tool_use` → `tool_input` = `json.dumps(input)`, `tool_name` = `name`
    -   `tool_result` → `tool_output` = `content` if `str` else
        `json.dumps(content)`; `tool_name` = `name`; prefix `is_error` results
    -   `thinking` → `content_type="thinking"`; `redacted_thinking` renders a
        placeholder
    -   `image` / `media` / `file` → text placeholders
    -   timestamps from per-message `ts` (ms → aware UTC datetime)
    -   `system_prompt` is not emitted as a message in v1
-   `delete_session(session)` — `shutil.rmtree(sessions/{id})`, then
    `DELETE FROM sessions WHERE session_id = ?` plus
    `DELETE FROM sessions WHERE parent_session_id = ?` (Cline's own store does
    both). Open the DB read-write with `timeout=5` like `cursor.py`. If the DB
    is absent or locked, still remove the directory and report the row deletion
    as failed rather than raising.
-   `move_project(old, new)` — rewrite `workspace_root` and `cwd` in each
    matching `{sessionId}.json` (prefix match, same rule as other providers)
    **and** in the corresponding DB rows, which are the index Cline reads.
    Report `files_modified`. The old path also appears inside `system_prompt`
    (`Working Directory: …`) and inside `tool_result` output; leave those, as
    the Claude provider already tolerates embedded stale paths.
-   `diagnostic_paths()` — inherited via `_data_dir`.

### Sub-agents

The schema anticipates them (`is_subagent`, `parent_session_id`,
`parent_agent_id`, `agent_id`, `subagent_spawn_queue`, the `spawn_agent` tool),
but no local session has `enable_spawn` set, so there is nothing to test
against. v1: **exclude** records with `is_subagent` truthy from
`get_sessions`, and set `SessionMeta.subagent_count` on the parent by counting
children with that `parent_session_id`. Do not implement `discover_subagents` /
`load_subagents` until a real spawned session exists to verify against.

### Wiring (mechanical, one line each unless noted)

-   `models.py` — `Provider.CLINE = "cline"`.
-   `discovery.py` — `PROVIDER_NAMES` + `class_names` + add `"cline"` to the
    set that receives `cache=`.
-   `cli.py` — `PROVIDER_CHOICES`, both `--provider` help strings
    (~L1442, ~L1501), the three provider-class dicts (~L99, ~L732, ~L921), and
    the `source_path` branch in the search-target loop (~L699: `r.file_path`).
    The resume error message needs no change (absent from `RESUME_COMMANDS`
    ⇒ falls into the existing "cannot be resumed" path).
-   `app.py` — `filter_cycle` (~L936), badge letter `"L"` in both badge spots
    (~L1294, ~L1841), provider-class dicts (~L1588, ~L2460), and the live-view
    source validation (~L2067: parse as a JSON dict with a `messages` list).
-   `resume.py` — nothing. Cline has no resume-by-id CLI; the `--session` /
    `resume` strings in the bundle belong to the vendored Claude Code SDK, not
    to Cline itself.
-   `search.py` — add `cline_sessions` to `_SearchRoots` (local + aggregated),
    `_search_cline()`: scan `sessions/*/*.messages.json`, session id = parent
    dir name, project path from the sibling `{id}.json`. Mirrors
    `_search_gemini`.
-   `cache.py` — no version bump needed (new provider, no changed semantics)
    unless the fingerprint helper needs a new shape.
-   `diagnostics.py` — verify the new provider appears with its root; no
    resume-CLI row (there is no Cline CLI).
-   `CLAUDE.md` / `README.md` — data-locations row (`~/.cline/data/`,
    `JSON+SQLite`), resume section ("Cline: not resumable"), delete/move
    sections, aggregation note (`{host}/.cline/data/` — a plain top-level
    dotdir, so no Cursor-style `Library/` caveat), and an explicit note that
    only the `next` bundle's store is read.

## Rollout order

1.  Provider module + unit tests (metadata, messages, tokens, delete, move)
    with a synthetic fixture factory in `tests/helpers.py`
    (`write_cline_session`, writing both JSON files and a matching DB row).
2.  `Provider.CLINE` + discovery/CLI/TUI wiring; run the full suite.
3.  Search support + `tmp_search_dirs` fixture entry + integration test
    (`requires_rg`).
4.  Docs (CLAUDE.md, README) and a `sesh doctor` run against the real dir.
5.  Manual check: `uv run sesh` shows the local sessions under the
    `cline:chat` pseudo-project; `sesh export 1788435477952_eibi9 --format html`
    renders `tool_use` + `tool_result` cards and a thinking block under `T`.

## Risks / open decisions

-   **Bundle-dependent visibility.** A user on the `legacy` bundle has no
    `~/.cline/data/sessions/`, so the provider yields nothing. This is
    intended, but `sesh doctor` should make it legible — report the root as
    present-but-empty rather than silently returning zero sessions.
    `~/.cline/data/globalState.json` exists under *both* bundles, so presence
    of the data dir is not evidence that `next` ran; key off `sessions/`.
-   **Chat-scratch sessions.** Chat-mode sessions have `workspace_root` =
    `{data_dir}/workspaces/chat` rather than a real project. Grouping them
    under a `cline:chat` pseudo-project is the v1 call; revisit if it clutters
    the tree.
-   **`metadata.tokensIn` / `tokensOut` are unreliable** (they skip tool-call
    turns — verified). Use `metadata.usage.*` and per-message `metrics.*`.
-   **DB writes on delete/move.** VS Code holds `sessions.db` open in WAL mode.
    Deletes and moves must tolerate `SQLITE_BUSY` and degrade to a clear error
    rather than a partial mutation.
-   **`version: 1` on both files.** Guard on it and skip unknown versions
    rather than parsing optimistically; the format is young.
-   **Sub-agent support is deferred** and untested by construction (no local
    session spawns agents).

## Validation

```bash
uv sync --extra dev
uv run pytest -q tests
uv run pytest -q tests/unit/test_provider_cline_*.py tests/unit/test_search*.py
uv run sesh doctor --provider cline --human
uv run sesh refresh && uv run sesh sessions --provider cline
uv run sesh search "Cline" --provider cline
```

## Decision log

-   2026-09-03 — First draft written against the legacy VS Code
    `globalStorage/…/tasks/` layout, before the `next` bundle activated.
-   2026-09-03 — Investigating missing Ollama thinking traces showed Cline
    4.1.17 ships two bundles; `next` activated mid-session and began writing a
    completely different store at `~/.cline/data/sessions/`. Verified the
    reasoning chain end to end (Ollama returns `message.thinking` by default;
    `next` persists it as `thinking` blocks; `legacy` discards it in the stream
    loop).
-   2026-09-03 — Plan rewritten for the `next` layout only; the legacy store is
    now an explicit non-goal (per David). Discovery reads the per-session JSON
    rather than `db/sessions.db` to avoid the live WAL writer; the DB is touched
    only for delete and move.
-   2026-09-03 — Implemented on `feature/cline-provider`. Deviations from the
    plan as written, all verified against the real store:
    -   The session record has **no `updated_at` field** on disk (that column
        exists only in `db/sessions.db`). `SessionMeta.timestamp` therefore
        falls back `updated_at` → `ended_at` → the transcript's `updated_at`
        → `started_at`, so a still-running session still sorts correctly.
    -   `_sessions_dir` (rather than `_sessions_root`) is the property name, so
        the base `diagnostic_paths()` reports `sessions/` **separately** from
        `data/`. That is what makes the legacy-bundle case legible in
        `sesh doctor`: a data dir with no `sessions/` warns `missing root`
        instead of silently scanning zero sessions.
    -   `move_project` was also wired into `move.py`'s orchestration (a
        `_dry_run_cline` plus an entry in the provider list), which the wiring
        list did not name but which `sesh move` needs to reach the provider.
    -   `search.CLINE_SESSIONS` is derived from the provider's
        `resolve_data_dir()` so search and discovery honour `CLINE_DATA_DIR` /
        `CLINE_DIR` identically.
    -   Prefix matching in `move_project` is boundary-aware, so `/old/repo`
        does not swallow `/old/repo-backup`.

    Verified end to end on the four real local sessions: `sesh doctor`
    (1 project, 4 sessions), `sesh sessions --provider cline` (session
    `1788435477952_eibi9` reports `input_tokens` 4936 / `output_tokens` 1373 /
    `cumulative_input_tokens` 11360, exactly the arithmetic predicted above),
    `sesh search`, `sesh export --format html` (tool and thinking cards
    render), and the TUI tree showing `cline:chat [L:4]`.
-   2026-09-03 — Post-implementation review round (fresh Opus 5 reviewer against
    the branch diff). Six confirmed defects found and fixed on the same branch,
    each with a regression test:
    -   `move_project`'s DB pass matched descendants with SQL `LIKE`, which is
        case-insensitive for ASCII while the JSON pass is not. Moving
        `/Users/me/repo` against a record stored as `/Users/me/Repo` reported
        `files_modified=0`, left the JSON right, and still rewrote Cline's index
        row. Both passes now match with the same case-sensitive Python helper and
        rewrite each row once.
    -   `preferences._VALID_PROVIDER_FILTERS` was a hand-maintained set that
        omitted `"cline"`, so the Cline filter never persisted and any save made
        while it was active wrote `None`. Now derived from the `Provider` enum,
        with a test that every provider round-trips.
    -   `delete_session` deleted sub-agent DB rows but left their directories, so
        the orphans kept being parsed. It now removes the child directories too.
    -   `delete_session` resolved its path from the record's internal
        `session_id` while search used the directory name; on divergence it
        silently no-opped (`ignore_errors=True` swallowed the miss). Identity is
        now the directory name everywhere, and a failed removal propagates
        instead of letting the index be edited anyway.
    -   The sessions cache was keyed on the transcript but served record-derived
        fields, so a title Cline backfilled into the record alone never
        appeared. The cache now stands in only for `message_count` /
        `input_tokens`; everything else is rebuilt from the record each scan.
        (This supersedes the composite-fingerprint design sketched above, and
        was an undocumented deviation in the entry before this one.)
    -   `_display_name` compared against the local `_chat_workspace`, so a
        mirrored host's chat sessions were named `chat` rather than
        `cline:chat`. Matching moved to the trailing path segments.

    Also fixed while in the same code: `_search_cline` now skips `is_subagent`
    transcripts (a hit on a session discovery never lists could not be opened
    and `sesh clean` would delete it unseen); an empty `metrics` dict reads as
    absent rather than `0`; the move error paths drop the stale record cache;
    `is_valid_session_id` tolerates a non-string; a `tool_result` with explicit
    `null` content renders empty rather than `"null"`. Two weak tests were
    rewritten: the cache round-trip now fails if the cache is bypassed, and the
    delete-failure test's docstring no longer claims the opposite of the
    behavior it locks in. Suite: 853 passing.
