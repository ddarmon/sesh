---
Status: done
Type: feature
Owner: David
Branch: feature/full-screen-and-help
Created: 2026-02-22
Updated: 2026-02-22
---

## ---

# Fullscreen Toggle + Help Screen

## Context

The sesh TUI shows a split-screen layout (session tree on the left,
messages on the right). There was no way to expand the message pane to
full width when reading a session. Meanwhile, the status bar listed 10+
keybindings in a single line and was getting crowded. Two features
address both issues: a fullscreen toggle for focused reading, and a
dedicated help screen so the status bar can be simplified.

## Feature 1: Fullscreen Toggle (`F`)

Press `F` to hide the session tree and expand the message pane to full
width. Press `F` again to restore the split view. Fullscreen persists
until explicitly toggled off (not reset by session selection or Escape).

Implementation details:

-   CSS rule `#main.fullscreen #session-tree { display: none; }` uses
    the same class-toggle pattern as `#message-search.visible`
-   `_fullscreen` boolean flag in `SeshApp.__init__`
-   `action_toggle_fullscreen` toggles the CSS class on `#main` and
    moves focus to `#message-view` when entering fullscreen (so the user
    isn't left with focus on a hidden widget)
-   No `priority=True` on the binding --- `F` types into Input widgets
    when focused, matching existing convention
-   Status suffix shows `Full:ON` when active

## Feature 2: Help Screen (`?`)

Modal screen listing all keybindings grouped by category (Navigation,
View, Session Actions, General). Dismiss with Escape or `?`.

Key design decisions:

-   **Screen-local BINDINGS** for both `escape` and `question_mark` that
    call `self.dismiss(None)`. Without these, `?` would bubble to the
    app's `show_help` and push a *second* HelpScreen, and `escape` would
    bubble to the app's `clear_search`.
-   **Hardcoded content** (not dynamic from `BINDINGS`) so keys can be
    logically grouped with readable descriptions. Comment in `compose`
    reminds maintainers to keep in sync.
-   Centered dialog, `width: 60`, `max-height: 80%`, scrollable on small
    terminals.

## Status Bar Simplification

Replaced the long keybinding string in both `_populate_tree_timeline`
and `_populate_tree_grouped` with
`q:Quit /:Search f:Filter o:Open ?:Help`. The suffix indicators
(`Full:ON`, `Tools:ON`, `Think:ON`) continue to appear after.

## Files Modified

| File                                          | What                                                                  |
| --------------------------------------------- | --------------------------------------------------------------------- |
| `src/sesh/app.py`                             | HelpScreen class, fullscreen CSS/flag/binding/action, status bar text |
| `tests/unit/test_app_helpers.py`              | Unit tests for `_format_status_suffix` with fullscreen flag           |
| `tests/integration/test_textual_app_smoke.py` | Integration tests for `F` toggle and `?` help screen                  |
| `CLAUDE.md`                                   | Document `F` and `?` in keybinding docs                               |

## Tests Added

Unit tests (3): `_format_status_suffix` with fullscreen only, all three
flags, and no flags.

Integration tests (5): fullscreen toggle state + CSS class, fullscreen
focus moves to message view, help screen opens on `?`, help dismisses on
Escape, help dismisses on `?` (no nesting).

## Verification

All 221 tests pass (`uv run pytest -q tests`).
