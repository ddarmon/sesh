---
Status: done
Type: feature
Owner: David Darmon
Branch: feature/claude-branch-sessions
Created: 2026-10-17
Updated: 2026-10-17
---

# List Claude Code `/branch` sessions separately

## Problem

`ClaudeProvider._parse_sessions` grouped sessions by the uuid of their root
user record (`parentUuid: null`) and kept only the newest per group. Claude
Code's `/branch` copies the conversation into a new file under a new
`sessionId`, preserving every `uuid`, so a branch and its original share a root
and one of them silently disappears from sesh (whichever was touched last).

On-disk evidence (Claude Code 2.1.201 and 2.1.301):

-   every copied record carries `forkedFrom: {sessionId, messageUuid}`
    (`messageUuid` equals the record's own `uuid`); records appended after the
    branch do not
-   the branch gets `custom-title` / `agent-name` records
    (`"<first prompt> (Branch)"`) and a `/branch` local-command record
-   the original file gets no back-reference
-   copied assistant records keep their `usage`, so summing usage over the
    branch re-counts every token already incurred in the original

A scan of the author's `~/.claude/projects` found exactly two shared-root
groups, both `/branch` pairs with `forkedFrom`; no unmarked shared-root group
(the presumed legacy resume-copy case) exists there.

## Rule

A session is hidden only when it is wholly visible elsewhere and was not made
deliberately. A branch is deliberate, so it and its original are always
listed.

## Changes

1.  Merge rule: sessions carrying `forkedFrom`, and sessions named by one, are
    never merged. Unmarked sessions sharing a root merge only when one's uuid
    set is contained in another's (the larger, or for identical sets the
    newer, is kept), so the legacy fallback can never hide unique content.
2.  `SessionMeta.forked_from` (parent session id) and `forked_at` (timestamp of
    the last copied record), carried in the cache, index, and
    `sesh sessions` JSON.
3.  A branch's `output_tokens` / `cumulative_input_tokens` sum only records
    without `forkedFrom`. `input_tokens` and `message_count` are unchanged.
4.  Titles: latest `custom-title` > `summary` record > latest `ai-title` >
    last prompt (existing fallbacks after that).
5.  `⎇` tree badge; "branched from" line in the TUI header (parent title, or
    "deleted session") and in the HTML header (parent id).
6.  `CACHE_VERSION` 7 -> 8.

## Deferred

-   Transcript divider at the branch point.
-   Sub-agents spawned before the branch live under the original's sidecar
    dir and are not shown in the branch; borrowing them would make deleting
    the original destructive to the branch.

## Validation

```bash
uv run pytest -q tests
uv run sesh refresh && uv run sesh sessions --project ~/workspace/clef
```

Expect both `be1decc5` and `20cefa03` (and `d774ddd2` / `f7a1915a` under
`~/workspace`), and `sesh stats` not to grow by the copied tokens.

## Outcome

Implemented in PR #70. Found and fixed along the way: per-directory sessions
cache entries ignored `CACHE_VERSION`, so earlier version bumps never
invalidated Claude metadata. The deferred sub-agent gap is confirmed on real
data (an original with three sub-agents whose branch shows none). A planned
switch from the last to the first root user record was dropped: no test could
distinguish the two under the new merge rule.
