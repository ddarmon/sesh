---
Status: done
Type: feature
Owner: David
Branch: feature/live-browser-view
Created: 2026-07-10
Updated: 2026-07-10
---

# Static and live browser views from the TUI

## Goal

Add a `v` action that opens the selected session in the existing browser
renderer and an `L` action that serves a live-updating browser view. Live mode
must consume the normalized provider API so it works for Claude, Codex, Cursor,
Copilot, pi, Gemini, and opencode.

## Design

- `v` renders the current session to the stable view cache, respecting the
  TUI's tools/thinking/agents visibility toggles, and opens it with the default
  browser.
- `L` starts a private loopback HTTP server for the current session. Pressing
  `L` again stops it; starting another live session replaces the first.
- The browser polls a token-protected JSON endpoint. The server reloads through
  `SessionProvider.get_messages`, retains the last good snapshot across
  transient parse/SQLite errors, and increments a revision only when normalized
  content changes.
- The browser rerenders only on a new revision, preserves expanded details and
  reader scroll position, and follows the bottom only when the reader was
  already near it.
- Claude sub-agents are included when the persisted agents toggle is enabled.
  All other providers use the same normalized message path.
- Bind only to `127.0.0.1`, use an unguessable URL token, disable caching and
  permissive CORS, and stop the server with the TUI.

## Rollout

1. Refactor the HTML payload so the static renderer can optionally carry live
   endpoint configuration and rerender in place.
2. Add a standard-library loopback live-view server with unit tests.
3. Add TUI `v` and `L` actions, status/help integration, and shutdown cleanup.
4. Update README and project documentation.
5. Run the complete test suite and perform focused live-server validation.

All rollout items are complete. The implementation shipped as a private
standard-library loopback server, a provider-normalized snapshot loader, live
polling with revision suppression/last-good retention, and TUI lifecycle
management.

## Validation

```bash
uv run pytest -q tests
```

Manual checks:

- `v` opens the current session and honors `t`, `T`, and `a`.
- `L` opens a loopback URL and appends newly written messages without a reload.
- Scroll position and open tool/sub-agent sections survive updates.
- Pressing `L` again stops the endpoint; quitting sesh also stops it.
- A transient malformed Gemini JSON or locked SQLite read leaves the last good
  transcript visible and reports a temporary update error.

Completed validation:

- `uv run pytest -q tests` — 638 passed.
- Ruff on every changed Python/test file — clean.
- Extracted live viewer JavaScript checked with `node --check` — clean.
- Loopback integration tests cover private tokenized URLs, security/no-cache
  headers, revision suppression, changed payloads, transient-error retention,
  clean shutdown, and every `Provider` enum value.
