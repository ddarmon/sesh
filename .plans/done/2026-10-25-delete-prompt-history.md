---
Status: done
Type: feature
Owner: David Darmon
Branch: feature/delete-prompt-history
Created: 2026-10-25
Updated: 2026-10-25
---

# Delete prompt-history entries with a session

## Goal

`sesh delete` / `sesh clean` (and the TUI `d` dialog) can optionally also
remove a deleted session's entries from the provider CLIs' prompt-history
files. Default: off.

## Provider survey (verified on disk, 2026-10-25)

| Provider | Store | Session key | In scope |
| --- | --- | --- | --- |
| Claude | `~/.claude/history.jsonl` | `sessionId` (8,711 / 8,792 entries; 81 legacy entries carry only `project` + `timestamp`) | yes |
| Claude | `~/.claude/paste-cache/{contentHash}.txt` | referenced from `history.jsonl` `pastedContents[*].contentHash` | see Q2 |
| Codex | `~/.codex/history.jsonl` | `session_id` (all entries) | yes |
| Codex | `~/.codex/session_index.jsonl` | `id` -> `thread_name` | yes, **always on** |
| Gemini | `~/.gemini/tmp/{dir}/logs.json` (JSON array, sibling of `chats/`) | `sessionId` | yes |
| Copilot | `~/.copilot/command-history-state.json` | none (`commandHistory: [str]`) | no |
| opencode | `~/.local/state/opencode/prompt-history.jsonl` | none | no |
| pi, Cline | no history store found | n/a | nothing to do |
| Cursor | not installed on the survey machine | unverified | no |

Copilot/opencode could only be matched by prompt text, which is ambiguous
across sessions; they are reported as `unsupported` rather than guessed at.

## Decisions

-   Codex `session_index.jsonl` cleanup is part of `delete_session` itself
    (always on), not the history flag: it caches the session title, which
    otherwise outlives the session.
-   The TUI delete dialog gets a "Also delete prompt history" checkbox.

## Design

-   **Provider hook.** `SessionProvider.delete_history(session, dry_run=False)
    -> HistoryReport` (entries removed per file, `supported: bool`). Base
    returns `supported=False`, like `diagnostic_issues()`. Overridden by
    Claude, Codex, Gemini. History matches the root session id only (see
    Decision Log).
-   **Order.** History is touched only after `delete_session` succeeds, so a
    failed session delete never strips history.
-   **Safe rewrite helper** (shared, `src/sesh/prompt_history.py`): filter into a `mkstemp` file in the same directory,
    preserve the original mode (Codex is `0600`), re-stat the original
    (size + mtime_ns) before `os.replace`; if it changed, re-run the filter
    (bounded retries), else give up and report. Narrows, does not close,
    the race with live `claude`/`codex` appenders.
-   **Gemini.** `logs.json` = `Path(source_path).parent.parent / "logs.json"`;
    `json.load` / `json.dump` the array, same re-stat guard.
-   **CLI.** `--history` on `delete` and `clean`. `--dry-run` reports counts
    without writing. JSON gains `history: {removed: {path: n}, unsupported:
    bool}` per session (and `history_error` on failure).
-   **TUI.** `ConfirmDeleteScreen` gains a `Checkbox`; dismiss value becomes
    `(confirmed, include_history)`. Status line reports entries removed.
-   **Aggregation mode.** No change: delete is already refused there.

## Rollout

1.  Codex `session_index.jsonl` cleanup in `delete_session` + tests.
2.  Safe-rewrite helper + tests (including a simulated concurrent append).
3.  `delete_history` for Claude, Codex, Gemini + per-provider tests.
4.  CLI flag for `delete` / `clean` + integration tests.
5.  TUI checkbox + pilot test.
6.  CLAUDE.md "Session deletion" section updated.

## Risks

-   Concurrent appenders (mitigated, not eliminated; see helper).
-   History formats are undocumented and may change; unknown-shape lines are
    kept, never dropped.

## Validation

```bash
uv run pytest -q tests
uv run sesh delete <id> --history --dry-run
```

## Resolved questions

1.  TUI checkbox always starts unchecked; not persisted in `preferences.json`.
2.  With `--history`, Claude `paste-cache/{hash}.txt` files are deleted once no
    remaining history entry references the hash (shared hashes are kept).
3.  The 81 legacy Claude entries without `sessionId` are skipped.
4.  If the history rewrite fails after the session itself was deleted, exit
    non-zero; the JSON still records the session deletion plus
    `history_error`.

## Decision Log

-   2026-10-25: Codex `session_index.jsonl` cleanup always on; TUI checkbox
    wanted (David).
-   2026-10-25: Checkbox not persisted; unreferenced paste-cache files removed;
    legacy unkeyed Claude entries skipped; history failure exits non-zero
    (David).
-   2026-10-25: Codex `delete_history` matches the root id only. Child
    rollouts are deleted by `delete_session` before history runs, so a
    child-id lookup there would make dry-run and real runs disagree; child
    threads are prompted by the parent agent, not typed by the user. Child
    ids are still dropped from `session_index.jsonl` (inside `delete_session`,
    before the files go).
-   2026-10-25: Helper named `prompt_history.py` (not `historyfile.py`) to
    avoid confusion with the existing rewind module `providers/history.py`.
-   2026-10-25: After a real `sesh delete` without `--history`, David expected
    to be asked about history too. Interactive runs now get a second `[y/N]`
    prompt (default no) after the delete confirmation, showing the entry count
    and file; `--force` / non-TTY runs never ask, so the opt-in default holds
    for scripts.

## Progress

-   [x] 1. Codex `session_index.jsonl` cleanup in `delete_session` + tests
-   [x] 2. Safe-rewrite helper + tests (simulated concurrent append, give-up path)
-   [x] 3. `delete_history` for Claude (incl. paste-cache), Codex, Gemini + tests
-   [x] 4. `--history` on `delete` / `clean` (both modes) + tests
-   [x] 5. TUI checkbox + pilot tests
-   [x] 6. CLAUDE.md / README updated
-   [x] 7. Interactive `[y/N]` history prompt for `delete` / `clean` + tests
-   Dry-run against real data: this session (3 Claude entries) and a Codex
    session (3 entries) both matched `grep -c`. No real (non-dry) run yet.
