---
Status: active
Type: feature
Owner: David
Branch: current
Created: 2026-02-22
Updated: 2026-02-22
---

# PR1 First: TUI Improvements (Features 1-3)

## Context

We are intentionally splitting the original four-feature plan into two
steps. PR1 ships the low-risk TUI improvements first:

1. Relative timestamps in the session tree
2. Persistent view preferences
3. Export from the TUI (`e` binding)

Feature 4 (session duration) is deferred to a follow-up PR because it
changes `SessionMeta`, cache serialization, and all three providers.

## Scope (PR1)

### Feature 1: Relative Timestamps in Session Tree

- Add `_relative_time(dt, now=None)` to `src/sesh/app.py`
- Replace the absolute timestamp label in `_session_label()`
- Handle naive datetimes as UTC and clamp future timestamps to `"now"`
- Add unit tests for threshold boundaries and edge cases

### Feature 2: Persistent View Preferences

- Add `src/sesh/preferences.py`
- Persist semantic values (provider filter + sort mode) and booleans
- Load/apply preferences in `SeshApp.__init__` + `on_mount`
- Save preferences on filter/sort/toggle actions
- Add test isolation in `tests/conftest.py` so local prefs do not leak into tests
- Add unit tests for load/save/default/corrupt/unknown-key behavior

### Feature 3: Export from TUI

- Add `src/sesh/export.py` with `format_session_markdown(...)`
- Refactor `src/sesh/cli.py` markdown export path to use shared formatter
- Add `e` binding + `action_export_session()` in `src/sesh/app.py`
- Update Help screen session actions list
- Add unit tests for markdown formatter output (text, tools, thinking, empty)

## Deferred (PR2)

Do not start until PR1 lands:

- `SessionMeta.start_timestamp`
- Cache serialization changes
- Claude/Codex/Cursor provider metadata changes
- Duration formatting in tree labels
- Provider/cache/duration test updates

## Verification (PR1)

Run before merge:

```bash
uv run pytest -q tests
```

Manual TUI checks:

- Tree labels show relative times (`2h ago`, `yesterday`, `3d ago`)
- `t`, `T`, `F`, `f`, `s` settings persist across relaunch
- `e` copies markdown export for the selected session
- `?` help screen shows the `e` binding
