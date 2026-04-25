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
from textual.widgets import (
    Button,
    Checkbox,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    Tree,
)

from sesh.bookmarks import load_bookmarks, save_bookmarks
from sesh.export import format_session_markdown
from sesh.models import Message, Project, Provider, SearchResult, SessionMeta, filter_messages
from sesh.preferences import load_preferences, save_preferences

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


def _relative_time(dt: datetime, now: datetime | None = None) -> str:
    """Return a compact relative timestamp label for session list display."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    if now is None:
        now = datetime.now(tz=timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    delta_seconds = (now - dt).total_seconds()
    if delta_seconds <= 0:
        return "now"
    if delta_seconds < 60:
        return "now"
    if delta_seconds < 3600:
        return f"{int(delta_seconds // 60)}m ago"
    if delta_seconds < 86400:
        return f"{int(delta_seconds // 3600)}h ago"
    if delta_seconds < 2 * 86400:
        return "yesterday"
    if delta_seconds < 7 * 86400:
        return f"{int(delta_seconds // 86400)}d ago"
    return dt.strftime("%m-%d %H:%M")


def _format_duration(start: datetime | None, end: datetime | None) -> str:
    """Return a compact duration for a session span, or empty string if unavailable."""
    if start is None or end is None:
        return ""

    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)

    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)

    delta_seconds = (end - start).total_seconds()
    if delta_seconds <= 0:
        return ""
    if delta_seconds < 60:
        return ""
    if delta_seconds < 3600:
        return f"{int(delta_seconds // 60)}m"
    if delta_seconds < 86400:
        return f"{int(delta_seconds // 3600)}h"
    return f"{int(delta_seconds // 86400)}d"


def _compact_tokens(input_tokens: int | None, output_tokens: int | None) -> str:
    """Format token counts as a compact string like '15K tok'."""
    if input_tokens is None and output_tokens is None:
        return ""
    total = (input_tokens or 0) + (output_tokens or 0)
    if total >= 1_000_000:
        return f"{total / 1_000_000:.1f}M tok"
    if total >= 1_000:
        return f"{total / 1_000:.0f}K tok"
    return f"{total} tok"


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


class HelpScreen(ModalScreen[None]):
    """Modal help screen listing keyboard shortcuts."""

    CSS = """
    HelpScreen {
        align: center middle;
    }

    #help-dialog {
        width: 60;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }

    #help-title {
        text-align: center;
        text-style: bold;
        padding-bottom: 1;
    }

    .help-group {
        margin-top: 1;
        text-style: bold;
    }

    .help-row {
        padding-left: 2;
    }

    #help-footer {
        margin-top: 1;
        text-align: center;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_help", "Close", show=False),
        Binding("question_mark", "dismiss_help", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        # Keep in sync with SeshApp.BINDINGS
        groups = [
            (
                "Navigation",
                [
                    ("/", "Focus session search"),
                    ("Esc", "Clear search / close message find"),
                    ("J", "Next project"),
                    ("K", "Previous project"),
                ],
            ),
            (
                "View",
                [
                    ("f", "Cycle provider filter"),
                    ("s", "Cycle sort mode"),
                    ("n", "Find in current messages"),
                    ("t", "Toggle tool messages"),
                    ("T", "Toggle thinking messages"),
                    ("F", "Toggle fullscreen message pane"),
                    ("S", "Open Terminal-tab snapshots (macOS)"),
                ],
            ),
            (
                "Session Actions",
                [
                    ("o", "Open / resume session"),
                    ("b", "Toggle bookmark"),
                    ("e", "Export session to clipboard"),
                    ("y", "Copy resume command"),
                    ("d", "Delete session"),
                    ("m", "Move project"),
                    ("r", "Refresh discovery"),
                ],
            ),
            (
                "General",
                [
                    ("?", "Show / close help"),
                    ("q", "Quit"),
                ],
            ),
        ]

        with Vertical(id="help-dialog"):
            yield Label("Keyboard Shortcuts", id="help-title")
            for title, rows in groups:
                yield Label(title, classes="help-group")
                for key, desc in rows:
                    yield Label(f"{key:<4} {desc}", classes="help-row")
            yield Label("Press Esc or ? to close", id="help-footer")

    def action_dismiss_help(self) -> None:
        self.dismiss(None)


class SnapshotsScreen(ModalScreen[None]):
    """Modal screen listing stored Terminal-tab snapshots."""

    CSS = """
    SnapshotsScreen {
        align: center middle;
    }

    #snapshots-dialog {
        width: 70;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #snapshots-title {
        text-align: center;
        text-style: bold;
        padding-bottom: 1;
    }

    #snapshots-list {
        height: 12;
        margin-top: 1;
    }

    #snapshots-empty {
        height: 3;
        margin-top: 1;
        color: $text-muted;
        text-align: center;
    }

    #snapshots-status {
        margin-top: 1;
        height: 1;
        color: $text-muted;
    }

    #snapshots-footer {
        margin-top: 1;
        text-align: center;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Close", show=False),
        Binding("c", "capture", "Capture", show=False),
        Binding("d", "delete_snapshot", "Delete", show=False),
        Binding("enter", "preview", "Preview", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._summaries: list = []

    def compose(self) -> ComposeResult:
        with Vertical(id="snapshots-dialog"):
            yield Label("Terminal Tab Snapshots", id="snapshots-title")
            yield Label("", id="snapshots-empty")
            yield ListView(id="snapshots-list")
            yield Label("", id="snapshots-status")
            yield Label(
                "c:Capture  Enter:Reopen  d:Delete  Esc:Close",
                id="snapshots-footer",
            )

    def on_mount(self) -> None:
        self._refresh_list()

    def _refresh_list(self) -> None:
        from sesh import snapshots

        try:
            self._summaries = snapshots.list_snapshots()
        except Exception as exc:
            self._summaries = []
            self._set_status(f"Error: {exc}")
            return

        view = self.query_one("#snapshots-list", ListView)
        empty = self.query_one("#snapshots-empty", Label)
        try:
            view.clear()
        except Exception:
            pass

        if not self._summaries:
            empty.update("No snapshots saved yet. Press 'c' to capture.")
            return

        empty.update("")
        for summary in self._summaries:
            view.append(
                ListItem(
                    Label(self._format_summary(summary)),
                )
            )

    @staticmethod
    def _format_summary(summary) -> str:
        ts = (summary.created_at or "").replace("T", " ")[:19]
        host = summary.host or "?"
        return (
            f"{ts}  {summary.tab_count} tabs "
            f"({summary.resumable_count} sessions)  [{host}]"
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#snapshots-status", Label).update(text)

    def _selected_summary(self):
        view = self.query_one("#snapshots-list", ListView)
        idx = getattr(view, "index", None)
        if idx is None or idx < 0 or idx >= len(self._summaries):
            return None
        return self._summaries[idx]

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)

    def action_capture(self) -> None:
        self._set_status("Capturing tabs (this may take a moment)...")
        self.run_worker(self._do_capture, thread=True, exclusive=True, group="snapshots-capture")

    def _do_capture(self) -> None:
        from sesh import snapshots

        try:
            snap = snapshots.capture()
            snapshots.save(snap)
        except snapshots.SnapshotsUnsupportedError as exc:
            self.app.call_from_thread(self._set_status, str(exc))
            return
        except Exception as exc:
            self.app.call_from_thread(self._set_status, f"Capture failed: {exc}")
            return

        def _after():
            self._refresh_list()
            self._set_status(
                f"Saved {snap.id} ({len(snap.tabs)} tabs, "
                f"{sum(1 for t in snap.tabs if t.resume) } resumable)"
            )

        self.app.call_from_thread(_after)

    def action_preview(self) -> None:
        summary = self._selected_summary()
        if summary is None:
            return
        self.app.push_screen(
            SnapshotPreviewScreen(summary.id),
            lambda result: self._handle_preview_result(summary.id, result),
        )

    def on_list_view_selected(self, event) -> None:
        # Allow click/Enter selection from the list to preview as well.
        self.action_preview()

    def _handle_preview_result(self, snapshot_id: str, result) -> None:
        from sesh import snapshots

        if result is None or not getattr(result, "confirmed", False):
            return

        include_shells = bool(getattr(result, "include_shells", False))
        self._set_status(f"Reopening {snapshot_id}...")

        def _do():
            try:
                snap = snapshots.load(snapshot_id)
                report = snapshots.restore(snap, include_shells=include_shells)
            except snapshots.SnapshotsUnsupportedError as exc:
                self.app.call_from_thread(self._set_status, str(exc))
                return
            except Exception as exc:
                self.app.call_from_thread(self._set_status, f"Reopen failed: {exc}")
                return

            note = report.note or ""
            msg = f"Reopened {report.launched} tab(s)"
            if report.fellback:
                msg += " (separate windows — Accessibility denied)"
            if note and not report.fellback:
                msg += f" — {note}"
            self.app.call_from_thread(self._set_status, msg)

        self.run_worker(_do, thread=True, exclusive=True, group="snapshots-restore")

    def action_delete_snapshot(self) -> None:
        summary = self._selected_summary()
        if summary is None:
            return

        def _on_confirm(confirmed):
            if not confirmed:
                return
            try:
                from sesh import snapshots

                snapshots.delete(summary.id)
            except Exception as exc:
                self._set_status(f"Delete failed: {exc}")
                return
            self._refresh_list()
            self._set_status(f"Deleted {summary.id}")

        self.app.push_screen(
            ConfirmDeleteScreen(f"Snapshot {summary.id}"),
            _on_confirm,
        )


class SnapshotPreviewScreen(ModalScreen):
    """Preview the restore plan for a snapshot before reopening tabs."""

    CSS = """
    SnapshotPreviewScreen {
        align: center middle;
    }

    #preview-dialog {
        width: 90;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #preview-title {
        text-align: center;
        text-style: bold;
        padding-bottom: 1;
    }

    #preview-warning {
        color: $warning;
        margin-bottom: 1;
    }

    #preview-rows {
        height: auto;
        max-height: 14;
        margin-top: 1;
    }

    .preview-row {
        padding: 0 1;
    }

    .preview-row-skip {
        color: $text-muted;
    }

    #preview-checkbox {
        margin-top: 1;
    }

    #preview-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }

    #preview-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Cancel", show=False),
    ]

    def __init__(self, snapshot_id: str) -> None:
        super().__init__()
        self.snapshot_id = snapshot_id
        self._snapshot = None
        self._include_shells = False

    def compose(self) -> ComposeResult:
        with Vertical(id="preview-dialog"):
            yield Label(f"Reopen snapshot: {self.snapshot_id}", id="preview-title")
            yield Label("", id="preview-warning")
            yield Vertical(id="preview-rows")
            yield Checkbox("Include plain shell tabs", id="preview-checkbox")
            with Horizontal(id="preview-buttons"):
                yield Button("Reopen", variant="primary", id="preview-confirm")
                yield Button("Cancel", variant="default", id="preview-cancel")

    def on_mount(self) -> None:
        from sesh import snapshots

        try:
            self._snapshot = snapshots.load(self.snapshot_id)
        except Exception as exc:
            self.query_one("#preview-warning", Label).update(f"Error: {exc}")
            return

        if self._snapshot.host:
            import socket as _socket

            if self._snapshot.host != _socket.gethostname():
                self.query_one("#preview-warning", Label).update(
                    f"[!] Captured on {self._snapshot.host}; paths may not exist"
                )

        self._render_rows()

    def _render_rows(self) -> None:
        from sesh import snapshots as _snapshots

        if self._snapshot is None:
            return

        plan = _snapshots.build_restore_plan(
            self._snapshot, include_shells=self._include_shells
        )

        container = self.query_one("#preview-rows", Vertical)
        try:
            for child in list(container.children):
                child.remove()
        except Exception:
            pass

        if not plan.items:
            container.mount(Label("(no tabs in snapshot)", classes="preview-row"))
            return

        for item in plan.items:
            text = item.label
            classes = "preview-row"
            if item.reason_skipped:
                text = f"{text}  — skipped: {item.reason_skipped}"
                classes = "preview-row preview-row-skip"
            container.mount(Label(text, classes=classes))

    def on_checkbox_changed(self, event) -> None:
        if getattr(getattr(event, "checkbox", None), "id", None) != "preview-checkbox":
            return
        self._include_shells = bool(getattr(event, "value", False))
        self._render_rows()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from sesh.snapshots import PreviewResult

        if event.button.id == "preview-confirm":
            self.dismiss(PreviewResult(confirmed=True, include_shells=self._include_shells))
        else:
            self.dismiss(None)

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)


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

    #main.fullscreen #session-tree {
        display: none;
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
        Binding("e", "export_session", "Export"),
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
        Binding("F", "toggle_fullscreen", "Fullscreen", key_display="F"),
        Binding("S", "show_snapshots", "Snapshots", key_display="S"),
        Binding("question_mark", "show_help", "Help", key_display="?"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.projects: dict[str, Project] = {}
        self.sessions: dict[str, list[SessionMeta]] = {}
        self.current_filter: Provider | None = None
        self.filter_cycle = [None, Provider.CLAUDE, Provider.CODEX, Provider.CURSOR, Provider.COPILOT]
        self.filter_index = 0
        self.sort_options = ["date", "name", "messages", "timeline"]
        self.sort_index = 0
        self._current_messages: list[Message] = []
        self._current_session: SessionMeta | None = None
        self._bookmarks: set[tuple[str, str]] = set()
        self._show_tools: bool = False
        self._show_thinking: bool = False
        self._fullscreen: bool = False
        self._status_base: str = "Loading..."

        prefs = load_preferences()
        provider_pref = prefs.get("provider_filter")
        for idx, provider in enumerate(self.filter_cycle):
            provider_value = provider.value if provider else None
            if provider_value == provider_pref:
                self.filter_index = idx
                break
        self.current_filter = self.filter_cycle[self.filter_index]

        sort_pref = prefs.get("sort_mode")
        if isinstance(sort_pref, str) and sort_pref in self.sort_options:
            self.sort_index = self.sort_options.index(sort_pref)

        self._show_tools = bool(prefs.get("show_tools", False))
        self._show_thinking = bool(prefs.get("show_thinking", False))
        self._fullscreen = bool(prefs.get("fullscreen", False))

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

        label = self.current_filter.value.title() if self.current_filter else "All"
        self.query_one("#provider-filter", Static).update(label)
        if self._fullscreen:
            self.query_one("#main", Horizontal).toggle_class("fullscreen")

        # Tier 1: instant display from cached index
        if self._load_from_index():
            self._populate_tree(provider_filter=self.current_filter)
            self._set_status("(refreshing...)")

        self._refresh_status()

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
        ts = _relative_time(session.timestamp)
        count = f"({session.message_count}) " if session.message_count else ""
        duration = _format_duration(session.start_timestamp, session.timestamp)
        dur = f"~{duration} " if duration else ""
        tok = _compact_tokens(session.input_tokens, session.output_tokens)
        tok_str = f"{tok} " if tok else ""
        summary = session.summary[:50]
        model = f" [{_short_model_name(session.model)}]" if session.model else ""
        if show_project:
            proj = self.projects.get(session.project_path)
            proj_name = proj.display_name if proj else session.project_path.rsplit("/", 1)[-1]
            return f"{star}{ts}  {count}{dur}{tok_str}{proj_name} — {summary}{model}"
        return f"{star}{ts}  {count}{dur}{tok_str}{summary}{model}"

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
            f"q:Quit /:Search f:Filter o:Open ?:Help"
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
            if Provider.COPILOT in prov_set:
                badges.append("P")
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
            f"q:Quit /:Search f:Filter o:Open ?:Help"
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

        # For Copilot, source_path must be the session directory (parent of events.jsonl).
        elif result.provider == Provider.COPILOT:
            source_path = str(Path(result.file_path).parent)

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
        if self._fullscreen:
            parts.append("Full:ON")
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

    def _save_current_prefs(self) -> None:
        sort_mode = self.sort_options[self.sort_index] if 0 <= self.sort_index < len(self.sort_options) else "date"
        save_preferences(
            {
                "provider_filter": self.current_filter.value if self.current_filter else None,
                "sort_mode": sort_mode,
                "show_tools": self._show_tools,
                "show_thinking": self._show_thinking,
                "fullscreen": self._fullscreen,
            }
        )

    def action_toggle_tools(self) -> None:
        """Toggle visibility of tool call messages."""
        self._show_tools = not self._show_tools
        if self._current_messages and self._current_session:
            self._render_messages(self._current_messages, self._current_session)
        self._save_current_prefs()
        self._refresh_status()

    def action_toggle_thinking(self) -> None:
        """Toggle visibility of thinking/reasoning messages."""
        self._show_thinking = not self._show_thinking
        if self._current_messages and self._current_session:
            self._render_messages(self._current_messages, self._current_session)
        self._save_current_prefs()
        self._refresh_status()

    def action_toggle_fullscreen(self) -> None:
        """Toggle fullscreen mode for the message pane."""
        self._fullscreen = not self._fullscreen
        self.query_one("#main", Horizontal).toggle_class("fullscreen")
        if self._fullscreen:
            self.query_one("#message-view", MessageView).focus()
        self._save_current_prefs()
        self._refresh_status()

    def action_show_help(self) -> None:
        """Show keyboard shortcuts help."""
        self.push_screen(HelpScreen())

    def action_show_snapshots(self) -> None:
        """Open the Terminal-tab snapshots modal (macOS only)."""
        from sesh.snapshots.backend import get_backend

        if get_backend() is None:
            self._set_status(
                "Terminal.app snapshots are macOS-only — "
                "no supported terminal backend on this platform"
            )
            return
        self.push_screen(SnapshotsScreen())

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
        elif session.provider == Provider.COPILOT:
            from sesh.providers.copilot import CopilotProvider
            messages = CopilotProvider().get_messages(session)
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
        badge_map = {Provider.CLAUDE: "C", Provider.CODEX: "X", Provider.CURSOR: "U", Provider.COPILOT: "P"}
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
        self._save_current_prefs()

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

    def action_export_session(self) -> None:
        """Export the current session to Markdown and copy it to the clipboard."""
        if self._current_session is None:
            return

        filtered = filter_messages(
            self._current_messages,
            include_tools=self._show_tools,
            include_thinking=self._show_thinking,
        )
        md = format_session_markdown(self._current_session, filtered)
        self._copy_text(md)
        self._set_status("Session exported to clipboard")

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
        self._save_current_prefs()

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
        from sesh.resume import is_resumable, resume_argv, resume_binary_available

        if not is_resumable(session):
            return None
        if not resume_binary_available(session.provider):
            return None
        return resume_argv(session.provider, session.id), session.project_path

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
        from sesh.providers.copilot import CopilotProvider
        from sesh.providers.cursor import CursorProvider

        providers_map: dict[Provider, type] = {
            Provider.CLAUDE: ClaudeProvider,
            Provider.CODEX: CodexProvider,
            Provider.CURSOR: CursorProvider,
            Provider.COPILOT: CopilotProvider,
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
            s
            for s in sess_list
            if not (s.id == session.id and s.provider == session.provider)
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
