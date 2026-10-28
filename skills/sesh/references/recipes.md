# sesh recipes

Use explicit IDs from discovery. Check installed subcommand help when options
are uncertain; do not infer support from another command's flags.

## Recover a decision or summarize prior work

```bash
sesh refresh
sesh sessions --project /path/to/project --limit 10
sesh search 'decision keyword' --project /path/to/project
sesh messages SESSION_ID --summary --limit 20
sesh messages SESSION_ID --limit 30
sesh messages SESSION_ID --offset 30 --limit 30 --include-tools
```

Read the relevant assistant responses and results, not only the user-only triage
view. For comparisons, repeat for each selected ID and report agreements,
changes, unresolved issues, and evidence separately. Message offsets apply to
that invocation's filtered message list; changing visibility flags changes the
pagination, so restart or recompute offsets when changing filters.

## Export or render

```bash
sesh export SESSION_ID --format md -o /path/to/transcript.md
sesh export SESSION_ID --format json -o /path/to/transcript.json
sesh export SESSION_ID --format html -o /path/to/transcript.html
sesh export SESSION_ID --include-tools --no-agents -o /path/to/transcript.md
sesh export SESSION_ID --include-thinking -o /path/to/transcript.md
sesh export SESSION_ID --full -o /path/to/transcript.md
sesh export SESSION_ID --full --include-hidden --format html -o /path/to/transcript.html
sesh view SESSION_ID --full --no-open
```

HTML is self-contained with Markdown, highlighted code, and LaTeX; it works
offline. `-o` writes the file and emits a small JSON confirmation. Check the
output exists and report its path. A plain export omits tool calls/results,
thinking, and hidden records: `--include-tools` and `--include-thinking` add
one each, `--full` adds both, and `--include-hidden` (not implied by `--full`)
adds hidden records. Sub-agents are included by default where supported. Tell
the user which content types the export contains. See "Content visibility
flags" in SKILL.md.

For an archived **Claude Code JSONL** that is absent from the index:

```bash
sesh export --file /path/to/archive.jsonl --format html -o /path/to/archive.html
sesh view --file /path/to/archive.jsonl --no-open
```

`--file` is not a generic loader for every provider's format.

## Diagnostics and statistics

```bash
sesh doctor
sesh doctor --provider claude --strict
sesh refresh
sesh stats --project /path/to/project
sesh bookmarks
sesh sessions --bookmarked
```

Use diagnostics for missing providers/dependencies rather than interpreting an
empty result as proof no history exists. Statistics can lack token data for
some providers. Context-size input tokens differ from cumulative usage; do not
sum context sizes and call that total consumption. Sub-agent usage may not be
folded into parent totals.

## Cross-machine history

Check whether `SESH_AGGREGATION_ROOT` is set before assuming local scope.
Use a user-supplied mirror root, not a guessed path:

```bash
sesh --aggregation-root /path/to/mirrors sessions --limit 20
sesh --aggregation-root /path/to/mirrors search 'topic'
sesh --aggregation-root /path/to/mirrors export SESSION_ID --format json -o /path/to/export.json
```

The root contains one mirrored home directory per host. Queries rebuild from
those mirrors without replacing the local index. Preserve `host` in findings;
identical project paths on different hosts are distinct. Mirror freshness is not
source-host liveness. If an ID is ambiguous, inspect help and ask for a narrower
source instead of inventing a host flag. sesh does not sync machines. Resume,
bookmarks, deletion, cleanup, and moves are disabled in aggregation mode.

## Administrative operations: only on request

```bash
sesh delete SESSION_ID --dry-run
sesh clean 'pattern' --dry-run
sesh move /old/path /new/path --dry-run
sesh move /old/path /new/path --metadata-only --dry-run
sesh snapshot list
sesh snapshot show SNAPSHOT_ID
sesh snapshot reopen SNAPSHOT_ID --dry-run
```

Review exact targets with the user before executing destructive changes. A full
move relocates project files as well as rewriting metadata; metadata-only is for
already-moved projects. A search pattern may match many unrelated transcripts.
Deletion changes original provider history, not just the sesh cache.

Terminal snapshots are macOS Terminal.app captures, not proof of current live
sessions. Saving captures tab state and scrollback; reopening spawns terminal
tabs and resumes sessions. Neither should be done as an incidental discovery
step. `sesh resume SESSION_ID` launches an interactive provider CLI: offer the
command rather than nesting it inside an agent's tool execution unless the user
has requested that behavior. `sesh follow SESSION_ID` stays running until stopped;
agree on how it will be launched and stopped before using it.
