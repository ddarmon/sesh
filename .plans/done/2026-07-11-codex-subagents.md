---
Status: completed
Type: feature
Owner: pi
Branch: feature/codex-subagents
Created: 2026-07-11
Updated: 2026-07-11
---

# Native Codex subagent transcripts

## Rationale

Current Codex releases persist every native subagent as a normal rollout JSONL with explicit `thread_source: subagent`, root `session_id`, `parent_thread_id`, agent path/nickname, and spawn metadata. Sesh currently lists those rollouts as standalone sessions and does not attach them to their root session, despite already having provider-neutral subagent rendering used by Claude.

## Scope

- Classify Codex root and subagent rollout files from `session_meta`.
- Hide subagent rollouts from the ordinary session list and count them on the root session.
- Lazily load Codex child transcripts through the existing `load_subagents` API.
- Suppress forked parent history copied into child rollouts.
- Reuse existing TUI/export/live-view agent toggles and composition.
- Generalize Claude-specific model/transcript wording.
- Add focused provider tests and run the full suite.

## Rollout order

1. Extend Codex metadata parsing and in-memory child index.
2. Add lazy child loading and transcript-boundary handling.
3. Add fixtures/tests for discovery, linkage, ordering, and message filtering.
4. Update architecture documentation and validate all tests.

## Risks

- Codex's schema is evolving; use redundant linkage fields defensively and fall back safely.
- Cached root metadata does not carry child linkage; classify every file from its cheap first-line header before consulting the session cache.
- Nested children currently render as a flat chronological set under the root because sesh's transcript containers are one level deep.
- Child rollouts fork parent context; loading from the `NEW_TASK` handoff boundary avoids duplicated parent messages.

## Validation

```bash
uv run pytest -q tests/unit/test_provider_codex_indexing.py
uv run pytest -q tests
```

## Outcome

Implemented native Codex child discovery, lazy transcript loading, inherited-context suppression, root deletion hygiene, generic CLI export/view support, child-aware search attribution, current `custom_tool_call` parsing, documentation, and provider tests. The live Codex 0.144.1 probe renders both child rollouts in Markdown export. Full suite validation completed after implementation and review fixes.

## Decision log

- 2026-07-11: Treat `thread_source == "subagent"` or a `source.subagent` object as authoritative child classification.
- 2026-07-11: Attach all descendants by root `session_id`, while preserving each child's direct parent only as source metadata; render descendants flat in v1.
- 2026-07-11: Review follow-up keeps discovery header-only for children, maps child search hits to root rollouts, and supports Codex 0.144.1 custom tool records.
