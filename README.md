# sesh

Browse and search Claude Code, Codex, and Cursor sessions in the
terminal.

`sesh` is a TUI that discovers session logs from multiple LLM coding
assistants, lets you browse them by project, read message threads, and
full-text search across all of them.

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

Launch with `sesh`. The TUI loads sessions in the background and
populates a project tree on the left. Select a session to view its
messages on the right.

### Keybindings

| Key      | Action                                          |
| -------- | ----------------------------------------------- |
| `/`      | Focus the search bar                            |
| `Escape` | Clear search and return to full tree            |
| `f`      | Cycle provider filter (All/Claude/Codex/Cursor) |
| `o`      | Open/resume the selected session in its CLI     |
| `q`      | Quit                                            |

### Search

-   **Filter-as-you-type**: Typing in the search bar instantly filters
    the tree by project name and session summary.
-   **Full-text search**: Press `Enter` to run a ripgrep search across
    all session JSONL files. Results appear in the tree.

### Provider badges

Each project in the tree shows which providers have sessions for it:

-   `C` -- Claude Code
-   `X` -- Codex
-   `U` -- Cursor

Example: `myproject [C,X:12]` means 12 sessions from Claude and Codex.

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
keyed by file path with mtime/size for invalidation.

## Project structure

```
src/sesh/
  __init__.py        # version
  __main__.py        # python -m sesh
  app.py             # Textual app, layout, keybindings
  models.py          # Project, SessionMeta, Message, SearchResult
  cache.py           # JSON metadata cache
  search.py          # ripgrep full-text search
  providers/
    __init__.py      # SessionProvider base class
    claude.py        # Claude Code JSONL parser
    codex.py         # Codex JSONL parser
    cursor.py        # Cursor SQLite parser
```

