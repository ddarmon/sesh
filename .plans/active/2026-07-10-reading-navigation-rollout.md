---
Status: active (planned)
Type: rollout
Owner: David
Branch: bugfix/aggregation-cli-consistency → feature/reading-navigation
Created: 2026-07-10
Updated: 2026-07-10
---

# Aggregation consistency, then transcript reading and navigation

## Goal

Ship the next work in two intentionally separate PRs:

1. A small `[BUGFIX]` PR that restores aggregation-index isolation and makes
   CLI provider filters consistent with the seven supported providers.
2. A `[FEATURE]` PR that makes the TUI and HTML viewer complete, navigable
   transcript readers: no silent truncation, stable message anchors, proper
   find navigation, and explicit controls for the live browser.

The feature work should start only after PR #47 (static/live browser views) and
PR 1 below are merged to `main`, so it builds on the final live-view API rather
than duplicating or rebasing around it.

## Product direction

Keep both readers capable, with different emphasis:

- The TUI remains a fast, complete transcript reader. Long bodies may be
  collapsed for performance, but the user must always see that content was
  omitted and be able to expand/copy the full message.
- The HTML viewer is the richest reading experience: stable anchors, transcript
  find, copy/link actions, and clear live/follow/pause state.
- Provider parsing stays behind the existing normalized `SessionMeta` /
  `Message` API. All reader features must work for Claude, Codex, Cursor,
  Copilot, pi, Gemini, and opencode; Claude sub-agent blocks are the only
  provider-specific presentation extension.

---

# PR 1 — aggregation and CLI consistency

## Branch and title

- Branch: `bugfix/aggregation-cli-consistency`
- PR title: `[BUGFIX] Fix aggregation index isolation and provider filters`
- Start from fresh `main` after PR #47 is merged.

## Scope

### 1. Preserve the local index in aggregation mode

`cli.cmd_refresh()` currently writes `index.json` unconditionally even though
aggregation mode promises never to overwrite the local-mode index.

- Make `cmd_refresh()` match `_refresh_index()`: discover and return its JSON
  summary in aggregation mode, but call `save_index()` only in local mode.
- Continuing to save the source-file metadata cache is acceptable because its
  keys are absolute source paths; the user-facing local index must remain
  untouched.
- Add a regression test that places a sentinel local index, runs aggregation
  refresh, and proves the sentinel was not replaced.

### 2. Do not flash local sessions during aggregation TUI startup

`SeshApp.on_mount()` currently calls `_load_from_index()` before aggregated
background discovery, which can briefly expose unrelated local sessions.

- Make `_load_from_index()` return `False` without reading `index.json` when
  `self._aggregation_root` is active.
- Add a Textual/unit regression test proving aggregation startup does not call
  `load_index()` or populate local projects.

### 3. Centralize CLI provider choices

The `stats` and `search` parser choices lag behind runtime support and omit
Gemini/opencode.

- Define one lightweight module-level tuple of provider string values in
  `cli.py` (do not import provider implementations at parser startup).
- Use it for every `--provider` argument.
- Update stale help descriptions that enumerate only the original providers.
- Update the `pyproject.toml` project description, which still names only four
  providers.
- Do not bump the package version; version bumps happen only at release time.

## Validation

```bash
uv run pytest -q tests/unit/test_cli_commands.py \
  tests/unit/test_cli_main_dispatch.py \
  tests/unit/test_discovery_aggregation.py \
  tests/integration/test_cli_aggregation.py \
  tests/integration/test_textual_app_smoke.py
uv run pytest -q tests
```

Manual check:

1. Build a local index and record its checksum.
2. Run `sesh --aggregation-root <mirror> refresh`.
3. Confirm the checksum is unchanged.
4. Launch the aggregation TUI and confirm no local-only tree appears before the
   mirrored hosts load.
5. Confirm `sesh stats --provider gemini` and
   `sesh search term --provider opencode` parse successfully.

---

# PR 2 — reading and navigation

## Branch and title

- Branch: `feature/reading-navigation`
- Suggested PR title: `[FEATURE] Add complete transcript reading and navigation`
- Start from fresh `main` after PR 1 is merged.

## Scope and rollout order

### Phase 1 — stable transcript identity

Create a provider-neutral identity layer before changing either renderer.

- Add a pure helper/model outside `app.py` that composes the visible transcript
  into stable items for main messages and Claude sub-agent containers.
- Derive a deterministic message key from session/thread namespace,
  timestamp, role, content type, tool name, full content digest, and a duplicate
  occurrence counter. Never use the current visible-list index as identity.
- Namespace sub-agent interior keys by `agent_id`; use `agent_id` for the
  container anchor.
- Keys must remain stable when:
  - a new message is appended;
  - tools/thinking are hidden or revealed;
  - a sub-agent block is inserted chronologically;
  - the same content appears more than once.
- Reuse these keys in `export.session_html_payload()` instead of maintaining a
  separate browser-only key algorithm.
- Treat keys as internal viewer identifiers, not permanent provider IDs or a
  public storage schema.

Suggested location: `src/sesh/transcript.py`. Keep identity/composition helpers
pure and heavily unit tested rather than adding more logic to `app.py`.

### Phase 2 — complete TUI messages without silent truncation

The current RichLog silently slices user, assistant, thinking, and tool bodies.
Replace that behavior with an explicit preview/expansion model.

- Introduce a transcript viewer/card abstraction in a separate widget module;
  avoid growing `app.py` with all rendering state.
- Every collapsed long message shows an omission marker such as
  `… 4,280 more characters`.
- Expanding displays the complete normalized content; collapsing restores the
  preview.
- Copying a message always copies the complete body, never the preview.
- Preserve expansion state by stable key across tool/thinking/agent toggles and
  live rerenders.
- Keep short messages visually close to the current RichLog presentation.
- Make the selected/focused message visually clear and keyboard reachable.

Proposed controls (confirm against Textual key behavior during implementation):

- `Enter`: expand/collapse the focused message or sub-agent block.
- `C`: copy the complete focused message.
- `Tab` / `Shift+Tab`: move focus between tree, transcript, and active search
  inputs using normal Textual focus behavior.

Performance gate:

- Do not render every full long body eagerly.
- Benchmark a synthetic transcript with at least 1,000 messages and several
  multi-megabyte tool results.
- If a card-per-message widget tree is too expensive, retain a lightweight
  virtualized/collapsed representation and open a focused full-message modal;
  silent truncation is not an acceptable fallback.

### Phase 3 — transcript find and navigation

Upgrade the existing highlight-only `n` search into deterministic navigation.

- Display a match counter (`3 / 17`).
- `Enter` moves to the next match while the transcript search input is focused;
  `Shift+Enter` moves to the previous match.
- `n` reopens/focuses transcript find and advances when a query already exists;
  `N` moves backward.
- Scroll the active match into view and distinguish it from non-active
  highlights.
- Search full message bodies, not collapsed previews.
- Include tool, thinking, and sub-agent bodies in matching. When the active
  match is hidden by a collapsed card/block, expand it temporarily or reveal
  the containing block so the hit is visible.
- Escape closes transcript find and restores normal focus without discarding
  unrelated session-tree search state.
- Preserve the active result by stable key when the transcript receives a live
  append; reset gracefully if the matching message disappears.

This phase is in-transcript navigation only. Global Search 2.0 ranking/filtering
is explicitly deferred, but its future results should be able to target these
stable anchors.

### Phase 4 — HTML anchors and reader controls

Use the same transcript keys in static and live HTML.

For every message and sub-agent container:

- Emit a DOM `id`/anchor derived from the stable key.
- Add unobtrusive actions to copy the full message and copy its `#anchor` link.
- Highlight a message when loaded through its fragment.
- Preserve open `<details>` state by stable key rather than list position.

Add a sticky browser reader toolbar:

- Transcript find input with next/previous controls and match count.
- Live status: live, paused, retrying, or disconnected.
- Pause/resume polling.
- Follow-output toggle, independent of pause.
- Last successful update time.
- Manual refresh.
- Current message count and a new-message indicator when follow is off.

Live behavior:

- Keep the existing polling transport; this bundle does not introduce
  filesystem watchers, SSE, or WebSockets.
- Never overlap polls from one page.
- Append/reconcile by stable keys where possible; rerender only when structural
  changes require it.
- Auto-follow only when enabled and the reader is already at/near the bottom.
- A transient provider error retains the last good transcript and shows a
  retrying state.
- Pausing is browser-side and does not stop the private server; pressing `L` in
  the TUI still owns server lifecycle.

Static-view behavior:

- Browser find/anchors/copy work without a server from `file://`.
- Live-only controls are hidden in static exports.
- The exported payload continues to contain only content allowed by the
  caller's tools/thinking/agents options; browser controls must not silently
  bypass export visibility choices.

### Phase 5 — session metadata and documentation

Add a compact, consistent details header where practical:

- provider, model, project, and aggregation host;
- full session ID with copy action;
- start/end time and duration;
- message/sub-agent counts;
- context and cumulative token counts;
- resume availability/command in the TUI when applicable.

Documentation:

- Update the README keybinding table and browser-view section.
- Update the in-app `?` help modal.
- Update `CLAUDE.md` architecture and behavior notes.
- Document preview limits, expansion/copy semantics, transcript-find controls,
  stable anchors, and live pause/follow behavior.
- Update screenshots only if the default layout changed materially.

## Testing strategy

### Pure/unit tests

- Stable keys across append, visibility changes, sub-agent insertion, duplicate
  messages, missing timestamps, and naive/aware timestamps.
- Preview length/omitted-count boundaries and full-content copying.
- Match indexing, next/previous wraparound, hidden-content matches, and live
  append preservation.
- HTML anchor uniqueness, fragment-safe escaping, copy controls, static/live
  toolbar differences, and open-state restoration keys.
- Live pause/follow state transitions and unchanged-revision behavior.

### Textual integration tests

- Focus and expand/collapse a long user, assistant, tool, thinking, and
  sub-agent message.
- Confirm the omission marker is visible and expansion shows the tail.
- Copy returns complete content.
- Find next/previous scrolls to and reveals the correct card.
- Expansion and active-match state survive `t`, `T`, `a`, and a live append.
- Short and empty sessions still render cleanly.

### Browser/JavaScript validation

- Extract the final inline viewer script and run `node --check`.
- Keep Python tests for emitted payload/DOM configuration.
- If DOM behavior becomes too complex for string assertions, add a small
  JavaScript DOM test harness rather than relying only on manual testing.

### Full validation

```bash
uv run pytest -q tests
uv run ruff check <all changed Python/test files>
```

Run the full suite after every source change under `src/sesh/`.

## User-testing checklist

Test at least one session from Claude, Codex, pi, Gemini, and a SQLite provider
(Cursor or opencode), plus a Claude session with sub-agents.

1. Open a long transcript in the TUI; verify no content disappears without an
   omission marker.
2. Expand/collapse and copy each content type.
3. Find a term in normal text, a tool result, thinking, and a sub-agent; use
   next/previous navigation.
4. Open static `v`; test anchors, fragment links, copy, and transcript find.
5. Open live `L`; pause, resume, disable follow, append messages, and verify the
   new-message indicator and manual return to the bottom.
6. Scroll upward during live output and confirm the page does not steal the
   reader's position.
7. Stop the TUI live server and confirm the browser reports disconnected while
   retaining the last transcript.
8. Repeat in aggregation mode and confirm updates follow mirror latency.

## Non-goals

- Filesystem watchers, SSE, or WebSockets.
- Global Search 2.0 ranking, grouping, filters, or SQLite FTS.
- Semantic/AI search or generated summaries.
- Tags, notes, saved searches, comparison, or cost analytics.
- Provider-format changes solely to obtain native message IDs.
- Package version bumps outside the release workflow.

## Risks and mitigations

- **TUI performance:** full bodies and many widgets can be expensive. Keep full
  content in the model but render it only on expansion; enforce the synthetic
  performance gate before settling on the widget architecture.
- **Identity collisions:** include content digest plus occurrence and thread
  namespace; test repeated identical records explicitly.
- **Focus/key conflicts:** pilot-test `Enter`, `n`/`N`, `C`, and search inputs;
  keep the help modal synchronized with final bindings.
- **Live scroll theft:** follow must be explicit and conditional on bottom
  proximity; preserve position otherwise.
- **Hidden-match confusion:** search full content and reveal the active
  container, but do not permanently change persisted visibility preferences.
- **HTML injection/privacy:** continue `html:false`, safe text assignment,
  `</script>` escaping, private live-server headers, and caller-controlled
  export visibility.
- **Scope growth:** stable identity, complete reading, in-transcript find, and
  browser controls are in scope; global search and new provider work are not.

## Decision log

- **2026-07-10:** Use two PRs so known aggregation/index correctness issues do
  not hide inside a large feature review.
- **2026-07-10:** Keep polling for live transport. The next bundle improves
  reader behavior rather than reopening the cross-provider transport design.
- **2026-07-10:** Stable transcript identity precedes both TUI and HTML work so
  expansion, matching, anchors, and live reconciliation share one model.
- **2026-07-10:** Keep both TUI and browser complete; make the browser richer,
  but do not treat TUI truncation as an acceptable permanent preview mode.
