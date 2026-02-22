---
Status: done
Type: test
Owner: David
Branch: feature/tests-rollout
Created: 2026-02-22
Updated: 2026-02-22
---

------------------------------------------------------------------------

# Test Suite Rollout for `sesh`

## Context

The repo started with a single large parsing test file
(`tests/test_provider_parsing.py`) and major test coverage gaps across:

-   cache/indexing
-   discovery orchestration
-   search helpers + `rg` integration
-   move orchestration
-   provider metadata/discovery/move/delete behavior
-   CLI commands and dispatch
-   TUI helper logic / delete behavior

The original external plan also identified three concrete regressions to
codify with tests:

-   `src/sesh/search.py`: Cursor-only search path broken by early return
    in `ripgrep_search()`
-   `src/sesh/app.py`: `_delete_session()` removed by `id` only instead
    of `(provider, id)`
-   `src/sesh/cli.py`: `cmd_clean` could double-delete the same Claude
    session due to search result granularity

## Goal

Build a structured test suite (unit + integration) that:

-   covers core pure modules and providers
-   validates CLI JSON workflows
-   codifies known regressions with tests
-   provides a path to add TUI/Textual smoke tests later

## Non-goals

-   Exhaustive UI snapshot coverage
-   Full Textual pilot coverage in this pass (environment/runtime
    dependent)
-   Perfect line coverage for all modules

## Approach

Use a phased rollout to stabilize the foundation first, then add
provider, orchestration, and CLI coverage.

Key choices:

-   Add pytest config in `pyproject.toml` (`pythonpath = ["src"]`) to
    remove ad hoc `PYTHONPATH=src` usage.
-   Use shared fixtures in `tests/conftest.py` to patch home-directory
    constants to `tmp_path`.
-   Add `tests/helpers.py` for JSONL/SQLite fixtures and model
    factories.
-   Prefer deterministic synthetic fixtures over real user data.
-   Codify regressions as tests and fix them in the same branch.

## Rollout Plan (Normalized)

1.  Test infra + core pure modules (`models`, `bookmarks`, `cache`)
2.  Provider metadata/discovery/move tests (Claude/Codex/Cursor)
3.  Search layer + discovery + move orchestration tests
4.  CLI command + dispatch tests
5.  TUI helper/delete logic tests
6.  Integration tests (CLI JSON endpoints + real `rg`)
7.  Optional: Textual pilot smoke tests

## Progress

-   `done` Phase 1--2: test infrastructure and core module coverage
-   `done` Phase 3: provider metadata/indexing/move/delete coverage
-   `done` Phase 4--5: discovery/search/move orchestration coverage
-   `done` Phase 6: CLI commands and `main()` dispatch coverage
-   `done` Phase 7: app helper/delete tests (version guard removed, runs
    on Python 3.13)
-   `done` Phase 8: CLI + `rg` integration tests and Textual pilot smoke
    tests

## Implemented Changes (Current Branch)

### Test Infrastructure

-   Added pytest config in `pyproject.toml`
-   Added `tests/conftest.py` shared fixtures (path monkeypatching)
-   Added `tests/helpers.py` fixture factories/utilities
-   Refactored `tests/test_provider_parsing.py` to import shared helpers

### Unit Test Coverage Added

-   Core: `test_models.py`, `test_bookmarks.py`, `test_cache.py`
-   Providers:
    -   `test_provider_claude_metadata.py`
    -   `test_provider_claude_move.py`
    -   `test_provider_codex_indexing.py`
    -   `test_provider_codex_move.py`
    -   `test_provider_cursor_metadata.py`
    -   `test_provider_cursor_ide_sessions.py`
    -   `test_provider_cursor_move.py`
-   Orchestration/search:
    -   `test_discovery.py`
    -   `test_search_helpers.py`
    -   `test_search_ripgrep.py`
    -   `test_move_orchestrator.py`
-   CLI:
    -   `test_cli_commands.py`
    -   `test_cli_main_dispatch.py`
-   App/TUI helper logic:
    -   `test_app_helpers.py`
    -   `test_app_delete_logic.py`

### Integration Tests Added

-   `tests/integration/test_cli_json_endpoints.py`
-   `tests/integration/test_search_with_real_rg.py`
-   `tests/integration/test_textual_app_smoke.py`

## Regressions Codified and Fixed

-   `src/sesh/search.py`
    -   Fixed `ripgrep_search()` so Cursor search still runs when no
        Claude/Codex directories exist.
-   `src/sesh/cli.py`
    -   Added session-level dedup in `cmd_clean()` by
        `(provider, session_id, source_path)`.
-   `src/sesh/app.py`
    -   Fixed `_delete_session()` in-memory removal to match on
        `(provider, id)` instead of `id` only.

## Additional Adjustments

-   `src/sesh/cache.py`
    -   `_dir_fingerprint()` now returns `None` for missing directories
        (supports cache invalidation semantics tested in unit tests).
-   `tests/conftest.py`
    -   Added lightweight `textual` stubs so test modules can import
        app-related code paths when Textual is not installed.
-   `pyproject.toml`
    -   Added `[project.optional-dependencies] dev` group with `pytest`
        and `pytest-asyncio` so `uv run pytest` uses the project Python
        (3.13) instead of the system Python (3.9).
    -   Removed Python 3.10 version guards from `test_app_helpers.py`
        and `test_app_delete_logic.py` since the dev environment now
        guarantees Python 3.10+.

## Validation

Latest full suite run on this branch:

```bash
uv run pytest -q tests
```

Result:

-   `213 passed` (Python 3.13.5, 0 skipped)

## Risks / Remaining Work

-   None. All planned phases are complete.

## Decision Log

-   2026-02-22: Kept plan files in repo-local `.plans/` for future
    feature/bugfix traceability.
-   2026-02-22: Chose phased rollout and atomic commits by area (infra,
    providers, search/orchestration, CLI, app, integration).
-   2026-02-22: Fixed regressions in the same branch as tests to keep
    the suite green and executable.
-   2026-02-22: Deferred Textual pilot smoke tests to a later pass due
    environment/runtime constraints.
-   2026-02-22: Added `pytest` and `pytest-asyncio` as dev dependencies
    to fix pytest running under system Python 3.9 instead of project
    Python 3.13. Removed Python version guards from app tests.
-   2026-02-22: Added 9 Textual pilot smoke tests covering widget
    mounting, key bindings, tree population, and bookmark toggling.

## References

-   Original external Claude Code plan (imported and normalized into
    this file)
-   Branch: `feature/tests-rollout`
-   Commits:
    -   `437b570` Add test infrastructure and core module coverage
    -   `b915d30` Add provider metadata and move unit tests
    -   `ca5d5f7` Add discovery and search coverage with rg regression
        test
    -   `4c24ba5` Add CLI command and dispatch tests
    -   `27afd40` Add app helper tests and fix session delete collision
    -   `b9db20c` Add CLI and rg integration tests
