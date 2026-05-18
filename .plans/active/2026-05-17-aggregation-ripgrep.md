---
Status: active
Type: feature
Owner: David
Branch: feature/aggregation-ripgrep
Created: 2026-05-17
Updated: 2026-05-17
---

------------------------------------------------------------------------

# Aggregation-aware `sesh search`

## Context

PR #28 (commit `1a37f03`) shipped read-only cross-machine aggregation:
`SESH_AGGREGATION_ROOT=/path` (or `--aggregation-root`) points sesh at a
tree of per-host mirrored `$HOME`s. Discovery, JSON output, the TUI
tree, and destructive-op refusal were all made aggregation-aware. The
one v1 caveat (CLAUDE.md, PR description):

> `sesh search` (ripgrep) still scans local `$HOME` and is not
> aggregation-aware in v1.

`src/sesh/search.py` hard-coded `Path.home() / ".claude" / "projects"`
(plus four siblings) at module level and `ripgrep_search(query)` took
only a query. Under aggregation mode that silently scanned the
aggregator's own `$HOME` --- wrong host, often empty, never the mirrored
content the user wanted.

## Approach

-   One `rg` invocation per host (mirrors
    `discovery._discover_aggregated` and lets us tag each result with
    the known host without parsing the file path back out).
-   `SearchResult.host: str | None` --- `None` in local mode (matches
    `Project.host` and `SessionMeta.host`).
-   Module-level path constants kept as local-mode defaults so the
    existing `tmp_search_dirs` fixture and unit tests that monkeypatch
    them keep working.

## Files changed

-   `src/sesh/models.py` --- add `host` field (Optional, default `None`)
    to `SearchResult`.
-   `src/sesh/search.py` --- new `_SearchRoots` dataclass;
    `_local_roots()` / `_aggregated_roots()` generators;
    `_search_one_host()` runs one rg per host;
    `ripgrep_search(query, aggregation_root=None)`; cursor helpers take
    per-host paths;
    `_decode_cursor_projects_path(encoded, *, validate_locally=True)`
    skips the local `is_dir()` probe in aggregation mode.
-   `src/sesh/cli.py` --- `cmd_search` passes `_aggregation_root(args)`
    and emits `host` in the JSON output.
-   `src/sesh/app.py` --- `_fulltext_search` passes
    `self._aggregation_root`; `_show_search_results` uses `r.host` for
    the project lookup and prefixes labels with `[host]`.
-   `CLAUDE.md` --- removed the v1 caveat; added a one-paragraph note
    that search is aggregation-aware.
-   `tests/conftest.py` --- new `tmp_aggregation_search_dirs` fixture
    (two-host tree).
-   `tests/integration/test_search_with_real_rg.py` --- two new tests
    (per-host JSONL + per-host Cursor transcripts/store.db).
-   `tests/unit/test_cli_commands.py` --- extended
    `test_cmd_search_outputs_json`; new
    `test_cmd_search_passes_aggregation_root`.
-   `tests/unit/test_search_ripgrep.py` --- pre-existing tests adapted
    to new cursor-helper signatures.

`snapshots/core.py:422` left untouched --- defaults to local (the
Terminal.app capture path doesn't carry an `aggregation_root`).

## Risks / safety

-   Cursor `_decode_cursor_projects_path` previously probed
    `Path(decoded).is_dir()` against the live FS. In aggregation mode
    the source-host filesystem isn't reachable; the probe was suppressed
    rather than left to falsely fall back to the encoded name.
-   Dedup keys are inherently host-distinct in aggregation mode because
    `file_path` differs per host root. No change to dedup logic needed.
-   Path-based provider detection (`/.claude/` substring) still works on
    aggregated paths --- `$ROOT/laptop/.claude/...` still contains
    `/.claude/`.

## Versioning

No bump. 0.10.0 (the original aggregation feature in PR #28) has not
been released yet, so this work ships under the same `0.10.0` tag rather
than bumping to `0.11.0`.

## Validation

-   Unit + integration suite: 395 passing (392 previous + 3 new).
-   CLI local mode: `sesh search 'term'` returns `host: null` for every
    row; result count matches pre-change behavior.
-   CLI aggregation mode:
    `SESH_AGGREGATION_ROOT=$HOME/sesh-agg sesh   search 'oMLX' | jq '[.[].host] | sort | unique'`
    returned `["mba", "mbp"]` (verified end-to-end with rsynced data
    between MacBook Air and MacBook Pro via the personal
    `~/.local/bin/sesh-agg-sync` script).
-   TUI: `/` search in aggregation mode shows `[host]` prefixes and
    selecting a result navigates to the correct `[host]` project node.
-   Snapshot subsystem: `tests/unit/test_snapshots_resolve.py` passes
    unchanged (production callers stay one-arg).

## Decision log

-   **2026-05-17** --- one-rg-per-host over one-rg-over-all-hosts.
    Cleaner attribution; matches the discovery pattern; minor subprocess
    overhead is acceptable for human-scale host counts.
-   **2026-05-17** --- `SearchResult.host` optional with `None` in local
    mode (rather than always-populated with `"local"` or hostname).
    Matches the `Project.host` / `SessionMeta.host` convention.
-   **2026-05-17** --- kept module-level path constants as local-mode
    defaults rather than removing them. Avoids churning the
    `tmp_search_dirs` fixture and seven unit tests that monkeypatch
    those names.
