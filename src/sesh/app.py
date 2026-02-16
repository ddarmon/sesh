"""sesh — Textual TUI for browsing AI coding sessions."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Header, Input, Label, RichLog, Static, Tree

from sesh.models import Message, Project, Provider, SessionMeta

_DATETIME_MIN = datetime.min.replace(tzinfo=timezone.utc)


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
        width: 40;
        min-width: 30;
        border: solid $accent;
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
    ]

    def __init__(self) -> None:
        super().__init__()
        self.projects: dict[str, Project] = {}
        self.sessions: dict[str, list[SessionMeta]] = {}
        self.current_filter: Provider | None = None
        self.filter_cycle = [None, Provider.CLAUDE, Provider.CODEX, Provider.CURSOR]
        self.filter_index = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="search-bar"):
            yield Input(placeholder="Search sessions...", id="search-input")
            yield Static("All", id="provider-filter")
        with Horizontal(id="main"):
            yield SessionTree("Sessions", id="session-tree")
            yield MessageView(id="message-view", wrap=True, markup=True)
        yield Static("Loading...", id="status-bar")

    def on_mount(self) -> None:
        tree = self.query_one("#session-tree", SessionTree)
        tree.root.expand()
        tree.show_root = False
        self.run_worker(self._discover_all, thread=True, exclusive=True)

    def _discover_all(self) -> None:
        """Background threaded worker: discover projects and sessions."""
        from sesh.providers.claude import ClaudeProvider

        providers_list = [ClaudeProvider()]

        try:
            from sesh.providers.codex import CodexProvider
            providers_list.append(CodexProvider())
        except Exception:
            pass

        try:
            from sesh.providers.cursor import CursorProvider
            providers_list.append(CursorProvider())
        except Exception:
            pass

        for provider in providers_list:
            try:
                for project_path, display_name in provider.discover_projects():
                    if project_path not in self.projects:
                        self.projects[project_path] = Project(
                            path=project_path,
                            display_name=display_name,
                        )
                    proj = self.projects[project_path]
                    sessions = provider.get_sessions(project_path)
                    if sessions:
                        proj.providers.add(sessions[0].provider)
                        existing = self.sessions.get(project_path, [])
                        existing.extend(sessions)
                        self.sessions[project_path] = existing
                        proj.session_count = len(self.sessions[project_path])
                        for s in sessions:
                            if proj.latest_activity is None or s.timestamp > proj.latest_activity:
                                proj.latest_activity = s.timestamp
            except Exception:
                pass

        for path in self.sessions:
            self.sessions[path].sort(key=lambda s: s.timestamp, reverse=True)

        # Persist cache
        try:
            from sesh.cache import SessionCache
            cache = SessionCache()
            for path, sess_list in self.sessions.items():
                for s in sess_list:
                    if s.source_path:
                        cache.put_sessions(s.source_path, [s])
            cache.save()
        except Exception:
            pass

        self.call_from_thread(self._populate_tree)

    def _populate_tree(self, filter_text: str = "", provider_filter: Provider | None = None) -> None:
        """Populate tree with projects and sessions."""
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

        for proj in sorted_projects:
            sessions = self.sessions.get(proj.path, [])

            if provider_filter:
                sessions = [s for s in sessions if s.provider == provider_filter]

            if not sessions:
                continue

            if filter_lower:
                proj_match = filter_lower in proj.display_name.lower()
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

            for session in sessions:
                ts = session.timestamp.strftime("%m-%d %H:%M")
                summary = session.summary[:50]
                session_label = f"{ts}  {summary}"
                child = project_node.add_leaf(session_label)
                child.data = session
                total_sessions += 1

        status = self.query_one("#status-bar", Static)
        filter_name = provider_filter.value.title() if provider_filter else "All"
        status.update(
            f"{shown_projects} projects · {total_sessions} sessions · "
            f"[{filter_name}] · q:Quit /:Search f:Filter o:Open d:Delete"
        )

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Load messages when a session node is selected."""
        if isinstance(event.node.data, SessionMeta):
            session = event.node.data
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

    def _render_messages(self, messages: list[Message], session: SessionMeta) -> None:
        """Render messages in the right pane."""
        view = self.query_one("#message-view", MessageView)
        view.clear()

        if not messages:
            view.write("[dim]No messages found.[/dim]")
            return

        for msg in messages:
            if msg.is_system:
                continue

            ts = msg.timestamp.strftime("%H:%M") if msg.timestamp else ""

            if msg.role == "user":
                view.write(f"\n[bold cyan]User[/bold cyan] [dim]({ts})[/dim]:")
                view.write(f"  {msg.content[:2000]}")
            elif msg.role == "assistant":
                view.write(f"\n[bold green]Assistant[/bold green] [dim]({ts})[/dim]:")
                try:
                    from rich.markdown import Markdown
                    md = Markdown(msg.content[:5000])
                    view.write(md)
                except Exception:
                    view.write(f"  {msg.content[:2000]}")
            elif msg.role == "tool":
                tool = msg.tool_name or "tool"
                view.write(f"\n[bold yellow]{tool}[/bold yellow] [dim]({ts})[/dim]:")
                view.write(f"  {msg.content[:500]}")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter sessions as user types."""
        if event.input.id == "search-input":
            self._populate_tree(
                filter_text=event.value,
                provider_filter=self.current_filter,
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

    def _show_search_results(self, results: list, query: str) -> None:
        """Display search results in tree."""
        tree = self.query_one("#session-tree", SessionTree)
        tree.clear()

        node = tree.root.add(f"Search: '{query}' ({len(results)} matches)", expand=True)
        for r in results[:100]:
            label = f"{r.matched_line[:60]}"
            child = node.add_leaf(label)
            child.data = r

        status = self.query_one("#status-bar", Static)
        status.update(f"Search: {len(results)} matches for '{query}' · Escape to clear")

    def _show_no_results(self, query: str) -> None:
        status = self.query_one("#status-bar", Static)
        status.update(f"No results for '{query}'")

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self) -> None:
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

    def action_open_session(self) -> None:
        """Open/resume the selected session in its CLI."""
        tree = self.query_one("#session-tree", SessionTree)
        node = tree.cursor_node
        if node is not None and isinstance(node.data, SessionMeta):
            self._open_session(node.data)

    def _open_session(self, session: SessionMeta) -> None:
        """Suspend sesh and launch the provider's CLI to resume the session."""
        result = self._resume_command(session)
        if result is None:
            status = self.query_one("#status-bar", Static)
            status.update(f"CLI not found for {session.provider.value}")
            return
        cmd_args, cwd = result
        with self.suspend():
            subprocess.run(cmd_args, cwd=cwd)

    @staticmethod
    def _resume_command(session: SessionMeta) -> tuple[list[str], str] | None:
        """Return (cmd_args, cwd) to resume a session, or None if the CLI is missing."""
        commands: dict[Provider, tuple[str, list[str]]] = {
            Provider.CLAUDE: ("claude", ["claude", "--resume", session.id]),
            Provider.CODEX: ("codex", ["codex", "resume", session.id]),
            Provider.CURSOR: ("agent", ["agent", f"--resume={session.id}"]),
        }
        binary, args = commands[session.provider]
        if shutil.which(binary) is None:
            return None
        return args, session.project_path

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
            status = self.query_one("#status-bar", Static)
            status.update(f"Error deleting session")
            return

        # Remove from in-memory session list
        sess_list = self.sessions.get(session.project_path, [])
        self.sessions[session.project_path] = [
            s for s in sess_list if s.id != session.id
        ]

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

        status = self.query_one("#status-bar", Static)
        status.update("Session deleted")


def main() -> None:
    app = SeshApp()
    app.run()


if __name__ == "__main__":
    main()
