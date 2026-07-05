---
Status: active
Type: feature
Owner: ddarmon
Branch: feature/claude-code-agents
Created: 2026-07-05
Updated: 2026-07-05
---

# Claude Code sub-agent transcripts: discovery, view, search, TUI

## Problem

Claude Code stores sub-agent (Task/Agent tool) transcripts in separate
`agent-{agentId}.jsonl` files that sesh currently skips everywhere
(`startswith("agent-")` filters in `providers/claude.py`). Consequences:

- `sesh view` / `export` / `messages` / TUI show none of the sub-agent
  activity of a session.
- Full-text search **already matches** inside agent files (rg's
  `*.jsonl` glob recurses into `subagents/` dirs) and attributes hits to
  the parent session via the internal `sessionId` field — but the opened
  session view cannot show the matched content ("phantom matches").
- Session `message_count` / token stats silently exclude sub-agent work.
- `delete_session` orphans the `{project}/{sessionId}/` sidecar dir
  (subagents + tool-results) when deleting a session.

## On-disk format (verified 2026-07-05, Claude Code ~mid-2026)

Three generations of layout coexist:

1. **Current**: main session `{project}/{sessionId}.jsonl` plus sibling
   dir `{project}/{sessionId}/subagents/` containing
   `agent-{agentId}.jsonl` and `agent-{agentId}.meta.json`. The sidecar
   has `{agentType, isFork, description, toolUseId}` — `toolUseId`
   matches the `id` of the spawning `Agent` tool_use block in the parent
   transcript (exact anchor). Fork transcripts may start with a
   `type: "fork-context-ref"` record (`parentSessionId`,
   `parentLastUuid`, `contextLength`) that references, not duplicates,
   inherited context.
2. **Older**: `{project}/subagents/agent-*.jsonl` (project level), no
   sidecar; records carry the parent `sessionId` internally.
3. **Oldest**: `agent-*.jsonl` directly in the project dir.

Agent JSONL records: `sessionId` = **parent** session id, `agentId`,
`isSidechain: true`, standard `message`/`timestamp` fields. The spawning
tool is named `Agent` in current transcripts (`Task` historically); its
tool_result text contains `agentId: <id>`.

## Design

New provider surface (Claude only for now):

- `SubagentMeta` / `SubagentThread` dataclasses in `models.py`:
  `agent_id`, `description`, `agent_type`, `is_fork`, `tool_use_id`,
  `file_path`, plus loaded `messages`, `message_count`, `output_tokens`.
- `ClaudeProvider.discover_subagents(session) -> list[SubagentMeta]`
  checking layouts 1 → 2 → 3 with graceful degradation (sidecar when
  present; else internal `sessionId` probe of the first record(s)).
- Message loading reuses the existing `get_messages` record parsing
  pointed at the agent file (records pass the `sessionId == session.id`
  filter already).

Presentation ("sub-agents are turns, not tool calls"):

- `sesh view` / `export --format html`: each sub-agent renders as a
  collapsed `<details>` thread anchored at its spawn point (via
  `toolUseId` when available, else appended in a trailing "Sub-agents"
  section). Shown regardless of tool visibility; `--include-tools` /
  `--include-thinking` govern the *interior* of the nested thread.
  Summary line: `⑂ {agent_type} — {description} · N msgs`.
- Markdown export: `## Sub-agent: {description} ({agent_id})` sections,
  interior headings demoted. JSON export: `subagents` array.
- Default ON when sub-agents exist; `--no-agents` suppresses.
- Search: `SearchResult` gains `agent_id`; hits whose `file_path` is an
  `agent-*.jsonl` get attributed to the parent session and marked `⑂`
  in TUI rows and carried in CLI JSON.
- TUI: `a` toggle (persisted like `t`/`T`) splices collapsed sub-agent
  sections into the message pane at spawn timestamps; session tree
  labels get a `⑂N` suffix. (Folding sub-agent output tokens into stats
  is DEFERRED — see rollout item 3 — to keep discovery lazy.)
- Hygiene: `delete_session` also removes `{project}/{sessionId}/`;
  `move_project` cwd rewrite covers agent files in all three layouts.

## Rollout order

1. **Foundation** — models + `discover_subagents` + loading + hygiene
   fixes + unit tests. (No user-visible change yet.) **DONE.**
2. **View/export** — HTML/md/json rendering + CLI flags. **Search
   attribution** — parallel, disjoint files. **DONE.**
3. **TUI** — `a` toggle (persisted, `Agents:ON` status), tree `⑂N`
   badges, search-row `⑂` markers, splice into the RichLog message pane,
   `e` export includes sub-agents. **DONE.** README + CLAUDE.md docs
   updated in this phase.
   - **DEFERRED:** folding sub-agent output tokens into `sesh stats` (and
     the `⑂N` badge already only counts the current layout). Doing so
     would require reading agent files during discovery, violating the
     lazy-discovery constraint (perf risk above). Left as future work.
4. Full suite, review. (Docs folded into phase 3.) **DONE.** A
   high-effort multi-agent review of the branch produced 10 confirmed
   findings (worst: an unsanitized `shutil.rmtree(source_dir /
   session.id)` path-traversal in `delete_session`; also mixed
   naive/aware timestamp crashes, unhardened agent-file parsing,
   sessionId-filter dropping fork records, TUI toggle/latency issues).
   All 10 fixed with regression tests; a follow-up adversarial pass on
   the fix diff confirmed closure and surfaced two residual toggle
   interactions (`a` vs. the ⑂ auto-show override; `t`/`T` on
   agents-only sessions), both fixed. Suite: 556 (main) → 623.

## Risks

- JSONL schema drift across Claude Code versions → three-layout
  fallback, sidecar optional, never hard-fail on parse errors (match
  existing provider style: swallow `json.JSONDecodeError`/`OSError`).
- Old inline sidechains (`isSidechain: true` records inside the main
  session file, pre-externalization) — group by `agentId` when present;
  do not regress existing rendering when absent.
- Perf: discovery must stay lazy — sub-agent scan happens on session
  open (message load), never during index refresh (except a cheap
  `⑂N` count via directory listing, no file reads).

## Validation

- `uv run pytest -q tests` (green baseline: 556 passed)
- Manual: `sesh view` on a session with new-layout subagents
  (e.g. sheetlink d34628b2), one with project-level legacy subagents
  (excalidraw-tools), and one with none.
- `sesh search` for a string that appears only inside an agent file.
