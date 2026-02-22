"""sesh — Textual TUI for browsing LLM coding sessions."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Header, Input, Label, RichLog, Static, Tree

from sesh.bookmarks import load_bookmarks, save_bookmarks
from sesh.models import Message, Project, Provider, SearchResult, SessionMeta, filter_messages

_DATETIME_MIN = datetime.min.replace(tzinfo=timezone.utc)

# Short display names for common model identifiers.
_MODEL_SHORT: dict[str, str] = {}


def _short_model_name(model: str) -> str:
    """Return a compact display name for a model identifier."""
    if model in _MODEL_SHORT:
        return _MODEL_SHORT[model]
    low = model.lower()
    # Claude models: claude-opus-4-..., claude-sonnet-4-5-..., claude-haiku-...
    for family in ("opus", "sonnet", "haiku"):
        if family in low:
            # Extract version digits after the family name
            idx = low.index(family)
            rest = low[idx + len(family):]
            # Strip leading separators, grab digit segments
            parts = rest.lstrip("-").split("-")
            digits = [p for p in parts if p.isdigit()]
            ver = ".".join(digits[:2]) if digits else ""
            short = f"{family}-{ver}" if ver else family
            _MODEL_SHORT[model] = short
            return short
    # Codex / GPT / other: take last meaningful segment
    parts = model.rsplit("-", 1)
    short = parts[-1] if len(parts) > 1 else model
    # If it's just a date stamp, use the first part instead
    if short.isdigit() and len(short) == 8:
        short = model.split("-")[0]
    _MODEL_SHORT[model] = short
    return short


class SessionTree(Tree):
    """Left pane: project/session tree."""

    BORDER_TITLE = "Sessions"


class MessageView(RichLog):
    """Right pane: message viewer."""

    BORDER_TITLE = "Messages"


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Modal dialog to confirm session deletion."""

    CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }

    #confirm-dialog {
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #confirm-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }

    #confirm-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, summary: str) -> None:
        super().__init__()
        self.summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label("Delete this session?")
            yield Label(f"[dim]{self.summary[:60]}[/dim]")
            with Horizontal(id="confirm-buttons"):
                yield Button("Delete", variant="error", id="confirm-yes")
                yield Button("Cancel", variant="default", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class MoveProjectScreen(ModalScreen[tuple[str, bool] | None]):
    """Modal dialog for moving a project path."""

    CSS = """
    MoveProjectScreen {
        align: center middle;
    }

    #move-dialog {
        width: 80;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #move-path {
        margin-top: 1;
    }

    #move-error {
        color: $error;
        height: 1;
        margin-top: 1;
    }

    #move-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }

    #move-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, current_path: str) -> None:
        super().__init__()
        self.current_path = current_path

    def compose(self) -> ComposeResult:
        with Vertical(id="move-dialog"):
            yield Label("Move project to:")
            yield Input(value=self.current_path, id="move-path")
            yield Label("", id="move-error")
            with Horizontal(id="move-buttons"):
                yield Button("Full Move", variant="primary", id="move-full")
                yield Button("Metadata Only", variant="default", id="move-meta")
                yield Button("Cancel", variant="default", id="move-cancel")

    def on_mount(self) -> None:
        self.query_one("#move-path", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "move-cancel":
            self.dismiss(None)
            return

        new_path = self.query_one("#move-path", Input).value.strip()
        if not new_path:
            self.query_one("#move-error", Label).update("Path is required")
            return

        full_move = event.button.id == "move-full"
        self.dismiss((new_path, full_move))


class SeshApp(App):
    """Main application."""

    TITLE = "sesh"
    CSS = """
    Screen {
        layout: vertical;
    }

    #search-bar {
        height: 3;
        dock: top;
        padding: 0 1;
    }

    #search-input {
        width: 1fr;
    }

    #provider-filter {
        width: 16;
        content-align: center middle;
        text-style: bold;
        padding: 0 1;
    }

    #main {
        height: 1fr;
    }

    #session-tree {
        width: 1fr;
        min-width: 30;
        border: solid $accent;
    }

    #message-pane {
        width: 1fr;
    }

    #message-search {
        display: none;
        dock: top;
        height: 3;
        padding: 0 1;
    }

    #message-search.visible {
        display: block;
    }

    #message-view {
        width: 1fr;
        border: solid $accent;
    }

    #status-bar {
        height: 1;
        dock: bottom;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_search", "Search", key_display="/"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("f", "cycle_filter", "Filter"),
        Binding("o", "open_session", "Open"),
        Binding("d", "delete_session", "Delete"),
        Binding("m", "move_project", "Move"),
        Binding("r", "refresh", "Refresh"),
        Binding("y", "copy_session_id", "Copy ID"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("J", "next_project", "Next Proj", key_display="J"),
        Binding("K", "prev_project", "Prev Proj", key_display="K"),
        Binding("b", "toggle_bookmark", "Bookmark"),
        Binding("n", "search_messages", "Find"),
        Binding("t", "toggle_tools", "Tools"),
        Binding("T", "toggle_thinking", "Thinking", key_display="T"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.projects: dict[str, Project] = {}
        self.sessions: dict[str, list[SessionMeta]] = {}
        self.current_filter: Provider | None = None
        self.filter_cycle = [None, Provider.CLAUDE, Provider.CODEX, Provider.CURSOR]
        self.filter_index = 0
        self.sort_options = ["date", "name", "messages", "timeline"]
        self.sort_index = 0
        self._current_messages: list[Message] = []
        self._current_session: SessionMeta | None = None
        self._bookmarks: set[tuple[str, str]] = set()
        self._show_tools: bool = False
        self._show_thinking: bool = False
        self._status_base: str = "Loading..."

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="search-bar"):
            yield Input(placeholder="Search sessions...", id="search-input")
            yield Static("All", id="provider-filter")
        with Horizontal(id="main"):
            yield SessionTree("Sessions", id="session-tree")
            with Vertical(id="message-pane"):
                yield Input(placeholder="Find in messages...", id="message-search")
                yield MessageView(id="message-view", wrap=True, markup=True)
        yield Static("Loading...", id="status-bar")

    def on_mount(self) -> None:
        self._bookmarks = load_bookmarks()
        tree = self.query_one("#session-tree", SessionTree)
        tree.root.expand()
        tree.show_root = False

        # Tier 1: instant display from cached index
        if self._load_from_index():
            self._populate_tree()
            self._set_status("(refreshing...)")

        # Tier 2: background refresh
        self.run_worker(self._discover_all, thread=True, exclusive=True)

    def _load_from_index(self) -> bool:
        """Load projects and sessions from the cached index for instant display."""
        from sesh.cache import _dict_to_session, load_index

        data = load_index()
        if not data:
            return False

        try:
            projects: dict[str, Project] = {}
            for p in data.get("projects", []):
                la = p.get("latest_activity")
                if la and isinstance(la, str):
                    la = la.replace("Z", "+00:00")
                    la = datetime.fromisoformat(la)
                else:
                    la = None
                projects[p["path"]] = Project(
                    path=p["path"],
                    display_name=p["display_name"],
                    providers={Provider(v) for v in p.get("providers", [])},
                    session_count=p.get("session_count", 0),
                    latest_activity=la,
                )

            sessions: dict[str, list[SessionMeta]] = {}
            for s_dict in data.get("sessions", []):
                s = _dict_to_session(s_dict)
                if s.project_path not in sessions:
                    sessions[s.project_path] = []
                sessions[s.project_path].append(s)

            self.projects = projects
            self.sessions = sessions
            return True
        except (KeyError, ValueError):
            return False

    def action_refresh(self) -> None:
        """Re-discover all sessions."""
        self._set_status("Discovering sessions...")
        self.run_worker(self._discover_all, thread=True, exclusive=True)

    def _discover_all(self) -> None:
        """Background threaded worker: discover projects and sessions."""
        from sesh.cache import SessionCache, save_index
        from sesh.discovery import discover_all

        self.call_from_thread(self._set_status, "Discovering sessions...")

        cache = SessionCache()
        projects, sessions = discover_all(cache=cache)
        self.projects = projects
        self.sessions = sessions

        # Persist per-file cache entries for all sessions
        for path, sess_list in self.sessions.items():
            for s in sess_list:
                if s.source_path:
                    cache.put_sessions(s.source_path, [s])
        cache.save()

        # Save index for Tier 1 instant display on next launch
        try:
            save_index(projects, sessions)
        except Exception:
            pass

        self.call_from_thread(self._refresh_tree)

    def _refresh_tree(self) -> None:
        """Re-populate the tree after background discovery, preserving cursor."""
        tree = self.query_one("#session-tree", SessionTree)
        selected_key = None
        if tree.cursor_node and isinstance(tree.cursor_node.data, SessionMeta):
            s = tree.cursor_node.data
            selected_key = (s.provider.value, s.id)

        search_text = self.query_one("#search-input", Input).value
        self._populate_tree(filter_text=search_text, provider_filter=self.current_filter)

        if selected_key:
            self._reselect_node(tree, selected_key)

    def _reselect_node(self, tree: SessionTree, key: tuple[str, str]) -> None:
        """Find and re-select a session node by (provider, id) key."""
        def _walk(node):
            if isinstance(node.data, SessionMeta):
                if (node.data.provider.value, node.data.id) == key:
                    tree.select_node(node)
                    return True
            for child in node.children:
                if _walk(child):
                    return True
            return False

        _walk(tree.root)

    def _sort_sessions(self, sessions: list[SessionMeta]) -> list[SessionMeta]:
        """Sort sessions based on current sort option."""
        sort_key = self.sort_options[self.sort_index]
        if sort_key == "name":
            return sorted(sessions, key=lambda s: s.summary.lower())
        if sort_key == "messages":
            return sorted(sessions, key=lambda s: s.message_count, reverse=True)
        # Default: date descending
        return sorted(sessions, key=lambda s: s.timestamp, reverse=True)

    def _session_label(self, session: SessionMeta, show_project: bool = False) -> str:
        """Build a display label for a session tree node."""
        star = "\u2605 " if (session.provider.value, session.id) in self._bookmarks else ""
        ts = session.timestamp.strftime("%m-%d %H:%M")
        count = f"({session.message_count}) " if session.message_count else ""
        summary = session.summary[:50]
        model = f" [{_short_model_name(session.model)}]" if session.model else ""
        if show_project:
            proj = self.projects.get(session.project_path)
            proj_name = proj.display_name if proj else session.project_path.rsplit("/", 1)[-1]
            return f"{star}{ts}  {count}{proj_name} — {summary}{model}"
        return f"{star}{ts}  {count}{summary}{model}"

    def _populate_tree(self, filter_text: str = "", provider_filter: Provider | None = None) -> None:
        """Populate tree with projects and sessions."""
        sort_name = self.sort_options[self.sort_index]

        if sort_name == "timeline":
            self._populate_tree_timeline(filter_text, provider_filter)
        else:
            self._populate_tree_grouped(filter_text, provider_filter)

    def _populate_tree_timeline(self, filter_text: str, provider_filter: Provider | None) -> None:
        """Flat timeline view: all sessions sorted by date, no project grouping."""
        tree = self.query_one("#session-tree", SessionTree)
        tree.clear()

        filter_lower = filter_text.lower()
        all_sessions: list[SessionMeta] = []

        for proj_path, sessions in self.sessions.items():
            proj = self.projects.get(proj_path)
            for s in sessions:
                if provider_filter and s.provider != provider_filter:
                    continue
                if filter_lower:
                    proj_name = proj.display_name if proj else ""
                    if not (
                        filter_lower in proj_name.lower()
                        or filter_lower in proj_path.lower()
                        or filter_lower in s.summary.lower()
                    ):
                        continue
                all_sessions.append(s)

        all_sessions.sort(key=lambda s: s.timestamp, reverse=True)

        # Bookmarks section
        if self._bookmarks:
            bookmarked = [
                s for s in all_sessions
                if (s.provider.value, s.id) in self._bookmarks
            ]
            if bookmarked:
                bm_node = tree.root.add("\u2605 Bookmarks", expand=True)
                for s in bookmarked:
                    child = bm_node.add_leaf(self._session_label(s, show_project=True))
                    child.data = s

        for session in all_sessions:
            label = self._session_label(session, show_project=True)
            child = tree.root.add_leaf(label)
            child.data = session

        filter_name = provider_filter.value.title() if provider_filter else "All"
        self._set_status(
            f"{len(all_sessions)} sessions · "
            f"[{filter_name}] · [Sort: timeline] · "
            f"q:Quit /:Search f:Filter s:Sort o:Open d:Delete m:Move r:Refresh"
        )

    def _populate_tree_grouped(self, filter_text: str, provider_filter: Provider | None) -> None:
        """Project-grouped view: sessions nested under projects."""
        tree = self.query_one("#session-tree", SessionTree)
        tree.clear()

        filter_lower = filter_text.lower()

        sorted_projects = sorted(
            self.projects.values(),
            key=lambda p: p.latest_activity or _DATETIME_MIN,
            reverse=True,
        )

        total_sessions = 0
        shown_projects = 0

        # Bookmarks section
        if self._bookmarks:
            bookmarked = []
            for proj_path, sessions in self.sessions.items():
                for s in sessions:
                    if (s.provider.value, s.id) not in self._bookmarks:
                        continue
                    if provider_filter and s.provider != provider_filter:
                        continue
                    if filter_lower:
                        proj = self.projects.get(proj_path)
                        proj_name = proj.display_name if proj else ""
                        if not (
                            filter_lower in proj_name.lower()
                            or filter_lower in proj_path.lower()
                            or filter_lower in s.summary.lower()
                        ):
                            continue
                    bookmarked.append(s)
            if bookmarked:
                bookmarked.sort(key=lambda s: s.timestamp, reverse=True)
                bm_node = tree.root.add("\u2605 Bookmarks", expand=True)
                for s in bookmarked:
                    child = bm_node.add_leaf(self._session_label(s, show_project=True))
                    child.data = s

        for proj in sorted_projects:
            sessions = self.sessions.get(proj.path, [])

            if provider_filter:
                sessions = [s for s in sessions if s.provider == provider_filter]

            if not sessions:
                continue

            if filter_lower:
                proj_match = (
                    filter_lower in proj.display_name.lower()
                    or filter_lower in proj.path.lower()
                )
                if not proj_match:
                    sessions = [
                        s for s in sessions
                        if filter_lower in s.summary.lower()
                    ]
                if not sessions:
                    continue

            # Provider badges
            badges = []
            prov_set = {s.provider for s in sessions}
            if Provider.CLAUDE in prov_set:
                badges.append("C")
            if Provider.CODEX in prov_set:
                badges.append("X")
            if Provider.CURSOR in prov_set:
                badges.append("U")
            badge_str = ",".join(badges)

            label = f"{proj.display_name} [{badge_str}:{len(sessions)}]"
            expand = shown_projects < 5
            project_node = tree.root.add(label, expand=expand)
            project_node.data = proj
            shown_projects += 1

            for session in self._sort_sessions(sessions):
                child = project_node.add_leaf(self._session_label(session))
                child.data = session
                total_sessions += 1

        filter_name = provider_filter.value.title() if provider_filter else "All"
        sort_name = self.sort_options[self.sort_index]
        self._set_status(
            f"{shown_projects} projects · {total_sessions} sessions · "
            f"[{filter_name}] · [Sort: {sort_name}] · "
            f"q:Quit /:Search f:Filter s:Sort o:Open d:Delete m:Move r:Refresh"
        )

    @staticmethod
    def _session_from_search_result(result: SearchResult) -> SessionMeta | None:
        """Convert a SearchResult into a SessionMeta for message loading."""
        if not result.session_id:
            return None

        source_path = result.file_path

        # For Claude, source_path must be the project directory (not a file).
        # ClaudeProvider.get_messages expects a directory to glob *.jsonl from.
        if result.provider == Provider.CLAUDE:
            marker = "/.claude/projects/"
            idx = result.file_path.find(marker)
            if idx != -1:
                # Path after marker: {encoded_name}/... — take first component
                after = result.file_path[idx + len(marker):]
                encoded_name = after.split("/")[0]
                source_path = result.file_path[:idx + len(marker)] + encoded_name

        return SessionMeta(
            id=result.session_id,
            project_path=result.project_path,
            provider=result.provider,
            summary="",
            timestamp=_DATETIME_MIN,
            source_path=source_path,
        )

    def _format_status_suffix(self) -> str:
        parts = []
        if self._show_tools:
            parts.append("Tools:ON")
        if self._show_thinking:
            parts.append("Think:ON")
        return (" \u00b7 " + " ".join(parts)) if parts else ""

    def _set_status(self, text: str) -> None:
        self._status_base = text
        self.query_one("#status-bar", Static).update(text + self._format_status_suffix())

    def _refresh_status(self) -> None:
        self.query_one("#status-bar", Static).update(
            self._status_base + self._format_status_suffix()
        )

    def action_toggle_tools(self) -> None:
        """Toggle visibility of tool call messages."""
        self._show_tools = not self._show_tools
        if self._current_messages and self._current_session:
            self._render_messages(self._current_messages, self._current_session)
        self._refresh_status()

    def action_toggle_thinking(self) -> None:
        """Toggle visibility of thinking/reasoning messages."""
        self._show_thinking = not self._show_thinking
        if self._current_messages and self._current_session:
            self._render_messages(self._current_messages, self._current_session)
        self._refresh_status()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Load messages when a session or search result node is selected."""
        data = event.node.data
        if isinstance(data, SessionMeta):
            self.run_worker(
                lambda: self._load_messages(data),
                thread=True,
                exclusive=True,
                group="messages",
            )
        elif isinstance(data, SearchResult):
            session = self._session_from_search_result(data)
            if session:
                self.run_worker(
                    lambda: self._load_messages(session),
                    thread=True,
                    exclusive=True,
                    group="messages",
                )

    def _load_messages(self, session: SessionMeta) -> list[Message]:
        """Load messages in a thread."""
        from sesh.providers.claude import ClaudeProvider
        from sesh.providers.codex import CodexProvider

        if session.provider == Provider.CLAUDE:
            messages = ClaudeProvider().get_messages(session)
        elif session.provider == Provider.CODEX:
            messages = CodexProvider().get_messages(session)
        elif session.provider == Provider.CURSOR:
            from sesh.providers.cursor import CursorProvider
            messages = CursorProvider().get_messages(session)
        else:
            messages = []

        self.call_from_thread(self._render_messages, messages, session)
        return messages

    def _render_messages(
        self, messages: list[Message], session: SessionMeta, highlight: str = ""
    ) -> None:
        """Render messages in the right pane."""
        self._current_messages = messages
        self._current_session = session

        view = self.query_one("#message-view", MessageView)
        view.clear()

        if not messages:
            view.write("[dim]No messages found.[/dim]")
            return

        visible = filter_messages(
            messages,
            include_tools=self._show_tools,
            include_thinking=self._show_thinking,
        )

        if not visible and messages:
            view.write("[dim]No visible messages. Press t for tool calls, T for thinking.[/dim]")
            return

        if not visible:
            view.write("[dim]No messages found.[/dim]")
            return

        hl = highlight.lower()

        for msg in visible:
            ts = f" [dim]({msg.timestamp.strftime('%H:%M')})[/dim]" if msg.timestamp else ""

            if msg.content_type == "thinking":
                view.write(f"\n[dim magenta]Thinking[/dim magenta]{ts}:")
                thinking_text = (msg.thinking or "")[:3000]
                view.write(f"  [dim]{self._highlight_text(thinking_text, hl)}[/dim]")

            elif msg.content_type == "tool_use":
                tool = msg.tool_name or "tool"
                view.write(f"\n[bold yellow]{tool}[/bold yellow] [dim](call)[/dim]{ts}:")
                inp = (msg.tool_input or "")[:1000]
                view.write(f"  {self._highlight_text(inp, hl)}")

            elif msg.content_type == "tool_result":
                tool = msg.tool_name or "tool"
                view.write(f"\n[bold yellow]{tool}[/bold yellow] [dim](result)[/dim]{ts}:")
                out = (msg.tool_output or "")[:2000]
                view.write(f"  {self._highlight_text(out, hl)}")

            elif msg.role == "user":
                view.write(f"\n[bold cyan]User[/bold cyan]{ts}:")
                content = msg.content[:2000]
                view.write(f"  {self._highlight_text(content, hl)}")
            elif msg.role == "assistant":
                view.write(f"\n[bold green]Assistant[/bold green]{ts}:")
                if hl:
                    content = msg.content[:5000]
                    view.write(f"  {self._highlight_text(content, hl)}")
                else:
                    try:
                        from rich.markdown import Markdown
                        md = Markdown(msg.content[:5000])
                        view.write(md)
                    except Exception:
                        view.write(f"  {msg.content[:2000]}")
            elif msg.role == "tool":
                tool = msg.tool_name or "tool"
                view.write(f"\n[bold yellow]{tool}[/bold yellow]{ts}:")
                content = msg.content[:500]
                view.write(f"  {self._highlight_text(content, hl)}")

    @staticmethod
    def _highlight_text(text: str, term: str) -> str:
        """Wrap case-insensitive matches of *term* in reverse markup."""
        if not term:
            return text
        # Escape Rich markup in the term, then do case-insensitive replace
        escaped = re.escape(term)
        def _repl(m: re.Match) -> str:
            return f"[reverse]{m.group()}[/reverse]"
        return re.sub(escaped, _repl, text, flags=re.IGNORECASE)

    def action_search_messages(self) -> None:
        """Toggle the message search input."""
        search = self.query_one("#message-search", Input)
        if search.has_class("visible"):
            search.remove_class("visible")
            search.value = ""
            # Re-render without highlights
            if self._current_messages and self._current_session:
                self._render_messages(self._current_messages, self._current_session)
        else:
            search.add_class("visible")
            search.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter sessions as user types, or highlight in messages."""
        if event.input.id == "search-input":
            self._populate_tree(
                filter_text=event.value,
                provider_filter=self.current_filter,
            )
        elif event.input.id == "message-search":
            if self._current_messages and self._current_session:
                self._render_messages(
                    self._current_messages, self._current_session, highlight=event.value
                )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Full-text search on Enter."""
        if event.input.id == "search-input" and event.value.strip():
            self.run_worker(
                lambda: self._fulltext_search(event.value.strip()),
                thread=True,
                exclusive=True,
                group="search",
            )

    def _fulltext_search(self, query: str) -> None:
        """Run ripgrep full-text search in a thread."""
        from sesh.search import ripgrep_search
        results = ripgrep_search(query)
        if results:
            self.call_from_thread(self._show_search_results, results, query)
        else:
            self.call_from_thread(self._show_no_results, query)

    def _show_search_results(self, results: list[SearchResult], query: str) -> None:
        """Display search results in tree."""
        tree = self.query_one("#session-tree", SessionTree)
        tree.clear()

        node = tree.root.add(f"Search: '{query}' ({len(results)} matches)", expand=True)
        badge_map = {Provider.CLAUDE: "C", Provider.CODEX: "X", Provider.CURSOR: "U"}
        for r in results[:100]:
            badge = badge_map.get(r.provider, "?")
            proj = self.projects.get(r.project_path)
            proj_name = proj.display_name if proj else r.project_path.rsplit("/", 1)[-1]
            snippet = r.matched_line.replace("\n", " ")[:80]
            label = f"[{badge}] {proj_name} — {snippet}"
            child = node.add_leaf(label)
            child.data = r

        self._set_status(f"Search: {len(results)} matches for '{query}' · Escape to clear")

    def _show_no_results(self, query: str) -> None:
        self._set_status(f"No results for '{query}'")

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self) -> None:
        # If message search is active, close it first
        msg_search = self.query_one("#message-search", Input)
        if msg_search.has_class("visible"):
            msg_search.remove_class("visible")
            msg_search.value = ""
            if self._current_messages and self._current_session:
                self._render_messages(self._current_messages, self._current_session)
            return
        search = self.query_one("#search-input", Input)
        search.value = ""
        search.blur()
        self._populate_tree(provider_filter=self.current_filter)

    def action_cycle_filter(self) -> None:
        self.filter_index = (self.filter_index + 1) % len(self.filter_cycle)
        self.current_filter = self.filter_cycle[self.filter_index]
        label = self.current_filter.value.title() if self.current_filter else "All"
        self.query_one("#provider-filter", Static).update(label)
        search_text = self.query_one("#search-input", Input).value
        self._populate_tree(filter_text=search_text, provider_filter=self.current_filter)

    def _copy_text(self, text: str) -> None:
        """Copy text to the system clipboard.

        Uses pbcopy on macOS (works in Terminal.app), falling back to
        Textual's OSC 52 method for other platforms/terminals.
        """
        if platform.system() == "Darwin" and shutil.which("pbcopy"):
            subprocess.run(
                ["pbcopy"], input=text.encode("utf-8"), check=True
            )
        else:
            self.copy_to_clipboard(text)

    def action_copy_session_id(self) -> None:
        """Copy the resume command for the selected session to the clipboard."""
        tree = self.query_one("#session-tree", SessionTree)
        node = tree.cursor_node
        if node is None:
            return
        data = node.data
        if isinstance(data, SearchResult):
            session = self._session_from_search_result(data)
        elif isinstance(data, SessionMeta):
            session = data
        else:
            return
        if session is None:
            return
        result = self._resume_command(session)
        if result is None:
            self._set_status(f"No resume command for {session.provider.value} session")
            return
        cmd_args, _cwd = result
        cmd_str = " ".join(cmd_args)
        self._copy_text(cmd_str)
        self._set_status(f"Copied: {cmd_str}")

    def action_toggle_bookmark(self) -> None:
        """Toggle bookmark on the selected session."""
        tree = self.query_one("#session-tree", SessionTree)
        node = tree.cursor_node
        if node is None:
            return
        data = node.data
        if isinstance(data, SearchResult):
            session = self._session_from_search_result(data)
        elif isinstance(data, SessionMeta):
            session = data
        else:
            return
        if session is None:
            return

        key = (session.provider.value, session.id)
        if key in self._bookmarks:
            self._bookmarks.discard(key)
            self._set_status("Bookmark removed")
        else:
            self._bookmarks.add(key)
            self._set_status("Bookmark added")
        save_bookmarks(self._bookmarks)

        search_text = self.query_one("#search-input", Input).value
        self._populate_tree(filter_text=search_text, provider_filter=self.current_filter)

    def action_cycle_sort(self) -> None:
        """Cycle session sort order."""
        self.sort_index = (self.sort_index + 1) % len(self.sort_options)
        search_text = self.query_one("#search-input", Input).value
        self._populate_tree(filter_text=search_text, provider_filter=self.current_filter)

    def action_next_project(self) -> None:
        """Jump to the next project node in the tree."""
        tree = self.query_one("#session-tree", SessionTree)
        current = tree.cursor_node
        # Collect all project-level nodes (direct children of root)
        project_nodes = list(tree.root.children)
        if not project_nodes:
            return
        if current is None:
            tree.select_node(project_nodes[0])
            return
        # Find which project the cursor is in or on
        target = current
        while target.parent is not None and target.parent is not tree.root:
            target = target.parent
        # target is now a project node (or root if something went wrong)
        try:
            idx = project_nodes.index(target)
            next_idx = (idx + 1) % len(project_nodes)
        except ValueError:
            next_idx = 0
        tree.select_node(project_nodes[next_idx])
        project_nodes[next_idx].expand()

    def action_prev_project(self) -> None:
        """Jump to the previous project node in the tree."""
        tree = self.query_one("#session-tree", SessionTree)
        current = tree.cursor_node
        project_nodes = list(tree.root.children)
        if not project_nodes:
            return
        if current is None:
            tree.select_node(project_nodes[-1])
            return
        target = current
        while target.parent is not None and target.parent is not tree.root:
            target = target.parent
        try:
            idx = project_nodes.index(target)
            prev_idx = (idx - 1) % len(project_nodes)
        except ValueError:
            prev_idx = len(project_nodes) - 1
        tree.select_node(project_nodes[prev_idx])
        project_nodes[prev_idx].expand()

    def action_open_session(self) -> None:
        """Open/resume the selected session in its CLI."""
        tree = self.query_one("#session-tree", SessionTree)
        node = tree.cursor_node
        if node is None:
            return
        if isinstance(node.data, SessionMeta):
            self._open_session(node.data)
        elif isinstance(node.data, SearchResult):
            session = self._session_from_search_result(node.data)
            if session:
                self._open_session(session)

    def _open_session(self, session: SessionMeta) -> None:
        """Suspend sesh and launch the provider's CLI to resume the session."""
        result = self._resume_command(session)
        if result is None:
            self._set_status(f"CLI not found for {session.provider.value}")
            return
        cmd_args, cwd = result
        # The project directory may no longer exist if files were moved
        # without updating provider metadata (or if a move only partially
        # completed).  Show a status-bar message instead of crashing.
        if not Path(cwd).is_dir():
            status = self.query_one("#status-bar", Static)
            status.update(f"Project directory not found: {cwd}")
            return
        with self.suspend():
            subprocess.run(cmd_args, cwd=cwd)

    @staticmethod
    def _resume_command(session: SessionMeta) -> tuple[list[str], str] | None:
        """Return (cmd_args, cwd) to resume a session, or None if the CLI is missing."""
        # Cursor IDE sessions (txt transcripts) can't be resumed from CLI
        if (
            session.provider == Provider.CURSOR
            and session.source_path
            and session.source_path.endswith(".txt")
        ):
            return None

        commands: dict[Provider, tuple[str, list[str]]] = {
            Provider.CLAUDE: ("claude", ["claude", "--resume", session.id]),
            Provider.CODEX: ("codex", ["codex", "resume", session.id]),
            Provider.CURSOR: ("agent", ["agent", f"--resume={session.id}"]),
        }
        binary, args = commands[session.provider]
        if shutil.which(binary) is None:
            return None
        return args, session.project_path

    def action_move_project(self) -> None:
        """Prompt to move the selected project and rewrite metadata."""
        tree = self.query_one("#session-tree", SessionTree)
        node = tree.cursor_node
        if node is None:
            return

        project_path = self._project_path_from_node(node)
        if not project_path:
            self._set_status("Select a project or session to move")
            return

        self.push_screen(
            MoveProjectScreen(project_path),
            lambda result: self._handle_move_result(project_path, result),
        )

    def _project_path_from_node(self, node) -> str | None:
        """Resolve a project path from the selected node or its ancestors."""
        current = node
        while current is not None:
            data = current.data
            if isinstance(data, Project):
                return data.path
            if isinstance(data, SessionMeta):
                return data.project_path
            if isinstance(data, SearchResult):
                return data.project_path
            current = current.parent
        return None

    def _handle_move_result(
        self,
        old_path: str,
        result: tuple[str, bool] | None,
    ) -> None:
        """Handle modal output and enqueue move work."""
        if result is None:
            return

        new_path, full_move = result
        old_abs = os.path.abspath(os.path.expanduser(old_path))
        new_abs = os.path.abspath(os.path.expanduser(new_path))

        if old_abs == new_abs:
            self._set_status("New path must be different from current path")
            return

        self.run_worker(
            lambda: self._execute_move(old_abs, new_abs, full_move),
            thread=True,
            exclusive=True,
            group="move",
        )

    def _execute_move(self, old_path: str, new_path: str, full_move: bool) -> None:
        """Run project move + metadata updates and refresh discovery."""
        from sesh.move import move_project

        self.call_from_thread(self._set_status, "Moving project...")

        try:
            reports = move_project(
                old_path=old_path,
                new_path=new_path,
                full_move=full_move,
                dry_run=False,
            )
        except Exception as exc:
            self.call_from_thread(self._set_status, f"Move failed: {exc}")
            return

        # Rebuild in-memory state and tree after metadata updates.
        self._discover_all()

        self.call_from_thread(self._set_status, self._format_move_status(new_path, reports))

    @staticmethod
    def _format_move_status(new_path: str, reports) -> str:
        """Build a concise status message from provider move reports."""
        failures = []
        warnings = []
        details = []
        for report in reports:
            change_bits = []
            if report.dirs_renamed:
                change_bits.append(f"{report.dirs_renamed} dirs")
            if report.files_modified:
                change_bits.append(f"{report.files_modified} files")
            change_summary = ", ".join(change_bits) if change_bits else "no changes"

            if report.success:
                details.append(f"{report.provider.value}: {change_summary}")
                if report.error:
                    warnings.append(f"{report.provider.value}: {report.error}")
            else:
                reason = report.error or "unknown error"
                failures.append(f"{report.provider.value}: {reason}")

        if failures:
            return f"Move completed with errors: {'; '.join(failures)}"
        if warnings:
            return (
                f"Move complete -> {new_path} "
                f"({'; '.join(details)}; warnings: {'; '.join(warnings)})"
            )
        return f"Move complete -> {new_path} ({'; '.join(details)})"

    def action_delete_session(self) -> None:
        """Prompt to delete the selected session."""
        tree = self.query_one("#session-tree", SessionTree)
        node = tree.cursor_node
        if node is not None and isinstance(node.data, SessionMeta):
            session = node.data
            self.push_screen(
                ConfirmDeleteScreen(session.summary),
                lambda confirmed: self._delete_session(session) if confirmed else None,
            )

    def _delete_session(self, session: SessionMeta) -> None:
        """Delete a session via its provider and refresh the tree."""
        from sesh.providers.claude import ClaudeProvider
        from sesh.providers.codex import CodexProvider
        from sesh.providers.cursor import CursorProvider

        providers_map: dict[Provider, type] = {
            Provider.CLAUDE: ClaudeProvider,
            Provider.CODEX: CodexProvider,
            Provider.CURSOR: CursorProvider,
        }

        provider_cls = providers_map.get(session.provider)
        if provider_cls is None:
            return

        try:
            provider_cls().delete_session(session)
        except Exception:
            self._set_status("Error deleting session")
            return

        # Remove from in-memory session list
        sess_list = self.sessions.get(session.project_path, [])
        self.sessions[session.project_path] = [
            s for s in sess_list if s.id != session.id
        ]

        # Remove bookmark if present
        bm_key = (session.provider.value, session.id)
        if bm_key in self._bookmarks:
            self._bookmarks.discard(bm_key)
            save_bookmarks(self._bookmarks)

        # Update project metadata
        proj = self.projects.get(session.project_path)
        if proj:
            remaining = self.sessions[session.project_path]
            proj.session_count = len(remaining)
            if not remaining:
                del self.sessions[session.project_path]
                del self.projects[session.project_path]
            else:
                proj.providers = {s.provider for s in remaining}
                proj.latest_activity = max(s.timestamp for s in remaining)

        search_text = self.query_one("#search-input", Input).value
        self._populate_tree(filter_text=search_text, provider_filter=self.current_filter)

        self._set_status("Session deleted")


def tui_main() -> None:
    app = SeshApp()
    app.run()


if __name__ == "__main__":
    tui_main()
