---
Status: completed
Type: bugfix
Owner: pi
Branch: bugfix/branches
Created: 2026-07-12
Updated: 2026-07-13
---

# Active conversation reconstruction after rewind/rollback

## Problem

Sesh currently treats provider JSONL files as linear append-only transcripts. Pi, Claude Code, and Codex CLI preserve records removed by a user rewind, so chronological parsing displays abandoned conversation history as if it remained active.

Verified examples:

- Pi `019f58b4-8a6f-7ca1-80a1-90f4e6a70c86`: entries form an `id` / `parentId` tree with two children at the rewind point.
- Claude `81b143c5-c595-458d-8873-8a9b3aef98c0`: records form a `uuid` / `parentUuid` tree and `type: last-prompt` identifies the active `leafUuid`.
- Codex `019f58c6-658c-70b1-9295-0847fe26a955`: the linear event log contains `thread_rolled_back` with `num_turns: 2`, followed by replacement turns.

This affects the TUI, `messages`, Markdown/JSON/HTML export, static/live browser views, discovery metadata, and potentially raw-file search results because all consume provider-normalized data or metadata.

## Goals

- Present only the provider's active logical conversation after one or more rewinds.
- Preserve existing tool/thinking fan-out, stable transcript identity, sub-agent handling, lazy message loading, and streaming file I/O.
- Use provider-specific replay semantics behind small pure helpers rather than forcing incompatible formats into one schema.
- Keep usage accounting explicit: transcript counts describe the active branch, while cumulative token/cost fields continue to describe API work actually incurred when the provider records it cumulatively.
- Fail safely on malformed or unfamiliar lineage data without hiding an entire valid session.

## Non-goals

- Expose abandoned branches or add a branch-selection UI in this bugfix.
- Rewrite provider files or alter provider resume behavior.
- Treat Claude/Codex native sub-agents as rewind branches; their existing separate-thread behavior remains intact.
- Invent a universal persistent branch model for providers whose storage is already linear.

## Design

### 1. Separate history reconstruction from message conversion

Add pure, provider-local reconstruction helpers that accept parsed JSON objects (or an iterator materialized only where lineage requires it) and return the active raw records/turns. Run reconstruction before existing content-block conversion.

Do not add branching fields to the public `Message` model: abandoned records should never reach the normalized transcript. Shared code is appropriate only for generic parent-chain traversal and defensive validation.

### 2. Pi: project the active parent chain

- Index entries with valid `id` values and traverse `parentId` from the selected active leaf.
- Keep all record types on that chain, not only messages, because custom/configuration records connect message nodes.
- Select the final valid appended node as the persisted active leaf. Verified with Pi 0.80.6: navigating back to an older branch without adding a record is ephemeral, and resuming the session returns to the final appended branch.
- If the selected leaf or chain is malformed (missing parent, cycle, duplicate id), retain the valid reachable suffix and log/fall back conservatively rather than returning no transcript.

### 3. Claude: project `last-prompt` ancestry

- For the selected `sessionId`, use the latest applicable `type: last-prompt` record's `leafUuid`.
- Traverse `uuid` / `parentUuid` and retain active ancestors before `_blocks_to_messages` runs.
- Include non-message connector records in traversal, while continuing to suppress system/internal content at conversion time.
- Define fallbacks for older files without `last-prompt`: prefer a unique terminal node/newest valid leaf; if lineage is ambiguous, preserve current linear behavior rather than silently choosing the wrong branch.
- Keep sidechain/sub-agent files governed by their existing loaders; do not apply a parent session's leaf to an `agent-*.jsonl` transcript.

### 4. Codex: replay turn rollback events

- Parse root rollout records into completed logical turns using `task_started` / `task_complete` and `turn_id` where available.
- On `thread_rolled_back`, remove the last `num_turns` active turns before accepting subsequent records.
- Flatten surviving turns through the existing response/event conversion logic.
- Support multiple rollbacks, rollback-to-empty, tool-heavy turns, interrupted turns, and older rollouts lacking complete boundary markers.
- Keep session-level setup records available as needed, but never count them as user turns.
- Apply rollback replay to root and child rollout parsing without disturbing the existing Codex child boundary that suppresses copied parent context before `NEW_TASK`.

### 5. Metadata and usage semantics

Refactor each provider so discovery and lazy loading share the same reconstruction rules without duplicating subtly different branch logic.

- `message_count`, summary fallback, last visible user message, and active model/context fields should derive from active history.
- Session start/end timestamps should describe the session's persisted activity unless UI semantics clearly require active-message bounds; document the choice in tests.
- Preserve cumulative token/output accounting for abandoned requests when it represents actual consumed API usage. Do not erase billed work merely because it left the active context.
- For last-turn context usage, select the latest active assistant turn rather than an abandoned turn.
- Avoid extra discovery passes. Pi and Codex already scan each file for metadata; Claude's directory/session aggregation should collect enough lineage during the existing pass.
- Bump cache compatibility/schema if necessary so stale cached counts and summaries are not retained after release.

### 6. Search correctness

Audit Claude and Codex ripgrep result construction. Raw ripgrep can match abandoned records even after transcript loading is fixed.

- Retain source line identity while reconstructing active records, or expose a provider helper that decides whether a matched JSONL line is active.
- Suppress abandoned-branch hits without converting search into an eager full-message scan of every session.
- Preserve Claude sub-agent and Codex child attribution behavior.
- Pi search is currently unsupported, so no new Pi search implementation is required.

If active-line filtering would materially expand this fix, split it into a clearly linked follow-up rather than claiming search is corrected; document the remaining inconsistency in the outcome.

## Rollout order

1. Add synthetic rewind fixtures reproducing the three verified schemas and failing assertions for current output.
2. Implement/test a small defensive parent-chain utility if sharing it reduces duplication.
3. Implement Pi active-chain reconstruction and verify the active-leaf rule against local Pi source/files.
4. Implement Claude `last-prompt` reconstruction plus old-file fallback.
5. Implement Codex turn replay and integrate it with root and child parsing.
6. Align discovery metadata and token semantics with active history.
7. Audit and, if feasible within scope, filter abandoned search hits.
8. Update `CLAUDE.md` architecture/provider notes and cache compatibility as needed.
9. Run focused tests, then the full suite, and manually export the three observed sessions.

## Tests

### Pure reconstruction cases

- No rewind preserves the existing transcript exactly.
- One branch/rollback removes abandoned messages.
- Multiple/nested rewinds select the final active history.
- Rewind to the beginning yields only replacement turns.
- Malformed parent links, duplicate ids, cycles, missing leaves, invalid/oversized rollback counts, and incomplete final turns fail conservatively.

### Provider regressions

- Pi: custom records between messages remain traversable; tool calls/results and thinking on the active chain still fan out correctly.
- Claude: `last-prompt.leafUuid` selects the active chain; abandoned tools/results are excluded; files without `last-prompt` retain safe behavior; sub-agents are unchanged.
- Codex: `thread_rolled_back.num_turns` removes whole turns regardless of the number of reasoning/tool/result records; repeated rollback works; copied child context suppression still works.
- Discovery: active message counts and summaries are correct; cumulative usage includes abandoned paid work where intended; last-context usage comes from the active leaf.
- Search: abandoned Claude/Codex hits are absent if search filtering lands in this change.
- End-to-end CLI/TUI fixtures confirm `messages`, export, view payloads, and live reload all inherit provider-correct transcripts without feature-specific branching code.

## Validation

```bash
uv run pytest -q tests/unit/test_provider_pi_metadata.py
uv run pytest -q tests/unit/test_provider_claude_metadata.py tests/unit/test_provider_claude_subagents.py
uv run pytest -q tests/unit/test_provider_codex_indexing.py
uv run pytest -q tests/unit/test_search.py tests/integration
uv run pytest -q tests

uv run sesh export 019f58b4-8a6f-7ca1-80a1-90f4e6a70c86 --format json
uv run sesh export 81b143c5-c595-458d-8873-8a9b3aef98c0 --format json
uv run sesh export 019f58c6-658c-70b1-9295-0847fe26a955 --format json
```

Expected manual results:

- Pi excludes “Bold the Reference.” and its response, retaining the replacement request that bolds both labels.
- Claude excludes “Hi there!” / “What are you up to?” and retains “New stuff.”
- Codex excludes the two rolled-back turns and retains “Hi there! Oops.”

## Risks

- Pi 0.80.6 establishes the final appended node as the persisted resume branch, but future schema versions could add an explicit marker; prefer such a marker if one appears and retain a defensive fallback.
- Claude has historical schema variants and records spanning project directories; fallback behavior must favor visibility over speculative filtering.
- Codex `num_turns` semantics and turn-boundary records may vary by version. Fixtures should cover both current and defensively supported older forms.
- Discovery cache entries created before the fix may preserve incorrect counts unless invalidated.
- Token totals have two meanings—active context versus incurred usage—and collapsing them would create a separate accounting bug.
- Search filtering can introduce significant repeated I/O if implemented per hit instead of per source file.

## Outcome

Implemented active-history reconstruction for all three verified formats:

- Pi projects the final appended node's `id` / `parentId` ancestry.
- Claude projects `last-prompt.leafUuid` through `uuid` / `parentUuid`, with linear fallback for incomplete or ambiguous legacy lineage.
- Codex replays `thread_rolled_back.num_turns` over completed task boundaries, removing whole turns including reasoning and tools.

Discovery counts, summaries, models, and final context usage now follow active history while cumulative incurred usage remains physical-history based. Cache version 3 invalidates stale metadata. Added malformed-lineage and provider regression tests.

Review against real local data found four regressions in the initial implementation, fixed in a follow-up commit on this branch:

- **Discovery performance**: the initial `_parse_sessions` re-read every JSONL in the project directory twice per session (~60x slower on a 120 MB dir: 0.10s → 6.2s). Lineage and branch-sensitive metadata are now collected inline during the existing single directory scan and derived by pure helpers with no per-session re-reads (0.14s measured); a bounded-opens regression test pins this.
- **Compaction**: a `compact_boundary` record has `parentUuid: null` and carries the real chain in `logicalParentUuid`; following only `parentUuid` dropped all pre-compaction history (a real session fell from 72 to 18 messages). The boundary is now bridged via the logical parent.
- **Parallel tool calls**: sibling `tool_result` records attach to their own `tool_use` record, so only one sat on the single leaf chain; live tool results were dropped (114 across 47 real sessions). Off-chain tool_results answering an active `tool_use` are now re-admitted.
- **Codex turn bookkeeping**: a `task_started` arriving mid-turn (user interrupt / nested starts) discarded the open turn's dialogue, and out-of-turn dialogue was thrown away (8 of 377 real rollback-free rollouts miscounted, one 3 → 0). Turns are now flushed rather than discarded, out-of-turn dialogue opens an implicit turn, and only records the transcript renders count as dialogue — so the metadata replay and transcript replay segment turns identically (verified equal on all 377 real root rollouts). Codex `message_count` therefore now counts rendered dialogue, excluding empty `user_message` events and blank assistant items.

The full suite passes (773 tests).

Raw ripgrep search filtering was deliberately deferred: current search uses one raw match per file, and correcting abandoned hits requires changing it to collect and replay candidate lines per source file without regressing search latency or sub-agent attribution. Transcript search in the TUI/HTML is corrected because it consumes normalized active messages. This remaining CLI/global-search inconsistency should be handled as a focused follow-up.

## Decision log

- 2026-07-12: Treat this as one cross-provider bugfix with provider-specific history replay, not three unrelated timestamp-filter patches.
- 2026-07-12: Normalize only the active conversation into `Message`; branch browsing is deferred.
- 2026-07-12: Preserve actual cumulative usage from abandoned turns while making transcript-derived metadata active-branch-aware.
- 2026-07-12: Pi 0.80.6 experiment `019f58ce-47ab-742a-b035-d0901e6d0306` confirmed that branch navigation alone writes no selection record; after exiting from an older branch, resume opened the final appended branch. Use the final valid appended node as Pi's persisted active leaf, while allowing a future explicit marker to take precedence.
- 2026-07-13: Real-data review of the initial implementation surfaced the compaction (`logicalParentUuid`), parallel tool-result, discovery-performance, and Codex turn-bookkeeping regressions above; fixes were verified against all local Claude sessions (431) and Codex root rollouts (377) before landing.
- 2026-07-13: Real rollback traces confirmed Codex's `num_turns` counts the aborted in-flight turn — rollback must flush the open turn before removing N turns, otherwise a completed turn is over-removed.
