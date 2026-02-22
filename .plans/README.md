# `.plans` Usage

Purpose: keep lightweight, repo-local execution plans that capture the
logic of a feature/bugfix, not just the final diff.

## What belongs here

-   Work-item plans for features, bugfixes, refactors, and testing
    rollouts
-   Scope, rationale, rollout order, risks, and validation commands
-   Progress updates and outcome notes while work is in flight

## What does not belong here

-   Long-term architecture decisions (prefer `docs/adr/` if you add
    ADRs)
-   Generated output, logs, or copied command transcripts
-   Duplicate issue/PR descriptions without additional execution detail

## Layout

-   `.plans/active/` --- current or paused work
-   `.plans/done/` --- completed work kept for reference

## File naming

Use sortable, descriptive names:

-   `YYYY-MM-DD-short-topic.md`
-   Example: `2026-02-22-test-suite-rollout.md`

## Recommended workflow

1.  Start from a Claude Code plan (for example from
    `~/.claude/plans/*.md`).
2.  Reformat/normalize it into a repo-local plan in `.plans/active/`
    with a stable filename (for example `YYYY-MM-DD-short-topic.md`).
3.  Add repo-specific context as work proceeds (branch, commits,
    decisions, outcome).
4.  Update the same file as decisions change (append to `Decision Log`).
5.  Move the file to `.plans/done/` when complete.

## Import conventions

-   Preserve the original plan intent and rollout logic.
-   Mark plans as `active (paused)` when work is intentionally paused.
-   Record what was actually implemented vs. what remains.

## Conventions

-   Keep plans short enough to scan, detailed enough to explain
    decisions.
-   Put plan metadata (Status / Type / Owner / Branch / Created /
    Updated) inside a YAML front matter block (`---`) so `md2md.sh`
    preserves line breaks instead of collapsing it into one wrapped
    paragraph.
-   Prefer links/references to files, branches, and commits over pasted
    diffs.
-   Prefer imported-and-normalized Claude Code plans over hand-written
    local templates.
-   Record deviations from the original plan (what changed and why).
-   For small fixes, a short plan is fine.
