# sesh

Browse and search Claude Code, Codex, and Cursor sessions in the
terminal.

`sesh` is a TUI that discovers session logs from multiple LLM coding
assistants, lets you browse them by project, read message threads, and
full-text search across all of them.

It also has a CLI that outputs JSON, so LLM agents can query session
history programmatically.

Inspired by [Claude Code UI](https://github.com/siteboon/claudecodeui).

## Install

```
uv tool install /path/to/this/repo
```

To reinstall after changes:

```
uv tool install --force /path/to/this/repo
```

Or run without installing:

```
uv run --directory /path/to/this/repo sesh
```

Requires Python 3.10+ and
[ripgrep](https://github.com/BurntSushi/ripgrep) for full-text search.

## Usage

### TUI

Launch with `sesh`. The TUI loads sessions in the background and
populates a project tree on the left. Select a session to view its
messages on the right.

#### Keybindings

| Key      | Action                                          |
| -------- | ----------------------------------------------- |
| `/`      | Focus the search bar                            |
| `Escape` | Clear search and return to full tree            |
| `f`      | Cycle provider filter (All/Claude/Codex/Cursor) |
| `o`      | Open/resume the selected session in its CLI     |
| `d`      | Delete the selected session (with confirmation) |
| `q`      | Quit                                            |

#### Search

-   **Filter-as-you-type**: Typing in the search bar instantly filters
    the tree by project name and session summary.
-   **Full-text search**: Press `Enter` to run a ripgrep search across
    all session JSONL files. Results appear in the tree.

#### Provider badges

Each project in the tree shows which providers have sessions for it:

-   `C` -- Claude Code
-   `X` -- Codex
-   `U` -- Cursor

Example: `myproject [C,X:12]` means 12 sessions from Claude and Codex.

### CLI

All subcommands output JSON to stdout. Run `sesh refresh` first to build
an index, then query it.

```
sesh refresh                              # discover sessions, build index
sesh projects                             # list all projects
sesh sessions                             # list all sessions
sesh sessions --project /path/to/project  # filter by project
sesh sessions --provider claude           # filter by provider
sesh messages <session-id>                # read messages
sesh messages <session-id> --summary      # user messages only
sesh messages <session-id> --limit 10     # first 10 messages
sesh search "some query"                  # full-text search via ripgrep
```

Run `sesh --help` or `sesh <command> --help` for full details.

## Providers

### Claude Code

Reads `~/.claude/projects/` JSONL files. Resolves project paths from the
`cwd` field in session entries. Groups sessions by conversation thread
(first user message UUID) and shows only the latest from each group.
Extracts summaries from `type: "summary"` entries, falling back to the
last user message.

### Codex

Reads `~/.codex/sessions/YYYY/MM/DD/*.jsonl`. Supports two formats:

-   **New format**: First line has `type: "session_meta"` with `cwd` in
    the payload.
-   **Legacy format**: Extracts `<cwd>` from `<environment_context>` XML
    in response items.

### Cursor

Reads `~/.cursor/chats/{md5(project_path)}/*/store.db` SQLite databases.
Discovers sessions by computing MD5 hashes of known project paths from
other providers. Returns empty gracefully if `~/.cursor/chats/` doesn't
exist.

## Cache

Parsed session metadata is cached at `~/.cache/sesh/sessions.json`,
keyed by file path with mtime/size for invalidation. The CLI index is
stored at `~/.cache/sesh/index.json`.

## Project structure

```
src/sesh/
  __init__.py        # version
  __main__.py        # python -m sesh
  cli.py             # argparse CLI with JSON subcommands
  app.py             # Textual TUI, layout, keybindings
  discovery.py       # shared discovery logic (used by TUI and CLI)
  models.py          # Project, SessionMeta, Message, SearchResult
  cache.py           # JSON metadata cache + index
  search.py          # ripgrep full-text search
  providers/
    __init__.py      # SessionProvider base class
    claude.py        # Claude Code JSONL parser
    codex.py         # Codex JSONL parser
    cursor.py        # Cursor SQLite parser
```

