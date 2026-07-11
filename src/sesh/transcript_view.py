"""Focusable, expandable transcript viewer widget for the TUI.

This module replaces the old ``RichLog`` message pane, which silently sliced
long user / assistant / thinking / tool bodies with no indication that content
was dropped. The new model is explicit:

* A message longer than :data:`PREVIEW_CHARS` renders a bounded **preview**
  plus an omission marker such as ``… 4,280 more characters``.
* Expanding a message (``Enter`` on the focused card) shows the **complete**
  normalized body; collapsing restores the preview.
* Copying a message (``C``) always copies the complete body, never the preview.
* The **full** body always lives in the model; only the preview is rendered
  until the user expands a card, so a 1000-message transcript with several
  multi-megabyte tool results never renders every full body eagerly.

Identity and composition come from :mod:`sesh.transcript`: the caller hands a
list of :class:`~sesh.transcript.TranscriptItem` (already visibility-filtered
and composed with sub-agent containers) to :meth:`TranscriptView.set_transcript`.
Every rendered card carries the item's **stable key**, so expansion state,
the selection cursor, and (Phase 3) find-navigation all survive tool/thinking/
agent toggles and live rerenders.

Design notes for the Phase 3 (find-navigation) author
-----------------------------------------------------

* The transcript is a **flat list of cards** in document order. Sub-agent
  interior messages are cards at ``depth == 1`` that exist only while their
  container is expanded. :meth:`TranscriptView.reveal_key` handles the
  "expand the container so a hidden interior match becomes visible" case.
* Highlighting is decoupled from matching. :func:`find_match_spans` is the pure
  matcher (case-insensitive, non-overlapping) used both to paint highlights and
  as a building block for a future match index. :meth:`TranscriptView.set_active_match`
  paints one card's active-match style distinctly from ordinary highlights.
* Everything you need to drive navigation is public: :attr:`~TranscriptView.keys`
  (document order), :meth:`~TranscriptView.card_for_key`,
  :meth:`~TranscriptView.reveal_key`, :meth:`~TranscriptView.set_active_match`,
  and :attr:`~TranscriptView.active_key`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

from sesh.models import Message, short_workflow_id
from sesh.transcript import TranscriptItem

#: Characters of a message body shown before collapsing to a preview + marker.
#: Bodies at or under this length render in full with no marker, keeping short
#: messages visually close to the old presentation.
PREVIEW_CHARS = 1600


def omission_marker(omitted: int) -> str:
    """Return the collapsed-body marker, e.g. ``… 4,280 more characters``."""
    unit = "character" if omitted == 1 else "characters"
    return f"… {omitted:,} more {unit}"


def preview_and_omitted(body: str, limit: int = PREVIEW_CHARS) -> tuple[str, int]:
    """Split *body* into (shown-preview, omitted-count).

    ``omitted == 0`` means the whole body fits and no marker is needed.
    """
    if len(body) <= limit:
        return body, 0
    return body[:limit], len(body) - limit


def find_match_spans(text: str, term: str) -> list[tuple[int, int]]:
    """Return non-overlapping ``(start, end)`` spans of *term* in *text*.

    Case-insensitive; literal (the term is not treated as a regex). Empty term
    or no match yields ``[]``. This is the shared matcher for highlighting and
    (Phase 3) match counting/navigation.
    """
    if not term:
        return []
    spans: list[tuple[int, int]] = []
    for m in re.finditer(re.escape(term), text, flags=re.IGNORECASE):
        spans.append((m.start(), m.end()))
    return spans


@dataclass(frozen=True)
class Match:
    """One find hit: a stable card ``key`` and a ``(start, end)`` body span.

    Spans are offsets into the card's **full** (uncollapsed) body, so a match
    can point past a collapsed card's preview boundary; navigation expands the
    card when it needs to surface such a hit.
    """

    key: str
    start: int
    end: int


def _match_bodies(items: list[TranscriptItem]):
    """Yield ``(key, full_body)`` for every matchable card in document order.

    Sub-agent interiors are included (each under its own interior key) so hits
    inside a collapsed sub-agent still count and can be revealed. Agent
    container headers are not searched — only message bodies.
    """
    for item in items:
        if item.kind == "agent":
            for interior in item.interior:
                assert interior.message is not None
                _, _, body = normalize_message(interior.message)
                yield interior.key, body
        else:
            assert item.message is not None
            _, _, body = normalize_message(item.message)
            yield item.key, body


def compute_matches(items: list[TranscriptItem], term: str) -> list[Match]:
    """Ordered per-card ``Match`` list for *term* across full bodies of *items*.

    Counting is **per card**: a body that contains the term contributes exactly
    one ``Match``, carrying the body's *first* span (used to reveal/scroll to the
    hit). This matches the HTML viewer's find, which counts one matching card per
    hit, so the two readers report the same ``i / n`` totals. Matching runs over
    complete bodies (not collapsed previews), so a hit past a card's preview
    boundary is still found. Empty term yields ``[]``.
    """
    if not term:
        return []
    matches: list[Match] = []
    for key, body in _match_bodies(items):
        spans = find_match_spans(body, term)
        if spans:
            start, end = spans[0]
            matches.append(Match(key, start, end))
    return matches


class TranscriptFinder:
    """Pure match-index model for transcript find navigation.

    Holds an ordered ``Match`` list (one entry per matching card — a card with
    three hits still contributes a single match, so the counter agrees with the
    HTML viewer) plus a 0-based active pointer. ``next`` /
    ``prev`` wrap around. On recompute (a live append or rerender) the active
    match is preserved **by stable key** — the same key's hit stays active even
    though list positions shifted — and resets gracefully to no-active when the
    matched card disappears. Free of any widget dependency so it is heavily
    unit-testable.
    """

    def __init__(self) -> None:
        self._term: str = ""
        self._matches: list[Match] = []
        self._active: int = -1

    @property
    def term(self) -> str:
        return self._term

    @property
    def matches(self) -> list[Match]:
        return list(self._matches)

    @property
    def count(self) -> int:
        return len(self._matches)

    @property
    def active_index(self) -> int:
        """0-based index of the active match, or ``-1`` when none is active."""
        return self._active

    @property
    def active(self) -> Match | None:
        if 0 <= self._active < len(self._matches):
            return self._matches[self._active]
        return None

    @property
    def active_key(self) -> str | None:
        m = self.active
        return m.key if m is not None else None

    def position(self) -> tuple[int, int]:
        """``(current_1based, total)``; current is ``0`` when nothing active."""
        cur = self._active + 1 if self._active >= 0 else 0
        return cur, len(self._matches)

    def label(self) -> str:
        """Human counter: ``""`` (no term), ``"No matches"``, or ``"3 / 17"``."""
        if not self._term:
            return ""
        cur, total = self.position()
        if total == 0:
            return "No matches"
        return f"{cur} / {total}"

    def set_term(self, term: str, items: list[TranscriptItem]) -> None:
        """Set the search *term* and recompute over *items*.

        A genuine term change jumps the active pointer to the first match; a
        no-op term (same string) preserves the active match by key.
        """
        changed = term != self._term
        self._term = term
        self._recompute(items, reset=changed)

    def recompute(self, items: list[TranscriptItem]) -> None:
        """Recompute with the current term (live append), preserving by key."""
        self._recompute(items, reset=False)

    def _recompute(self, items: list[TranscriptItem], *, reset: bool) -> None:
        prev = self.active
        self._matches = compute_matches(items, self._term)
        if not self._matches:
            self._active = -1
            return
        if reset:
            self._active = 0
            return
        if prev is not None:
            # Prefer the identical hit, then any hit sharing the stable key.
            for i, m in enumerate(self._matches):
                if m == prev:
                    self._active = i
                    return
            for i, m in enumerate(self._matches):
                if m.key == prev.key:
                    self._active = i
                    return
        # Active card vanished (or none was active): reset gracefully.
        self._active = -1

    def next(self) -> Match | None:
        """Advance to the next match with wraparound; ``None`` if no matches."""
        if not self._matches:
            self._active = -1
            return None
        self._active = 0 if self._active < 0 else (self._active + 1) % len(self._matches)
        return self.active

    def prev(self) -> Match | None:
        """Step to the previous match with wraparound; ``None`` if no matches."""
        if not self._matches:
            self._active = -1
            return None
        if self._active < 0:
            self._active = len(self._matches) - 1
        else:
            self._active = (self._active - 1) % len(self._matches)
        return self.active


def normalize_message(m: Message) -> tuple[str, str, str]:
    """Return ``(role_category, header, body)`` for one message.

    ``role_category`` drives styling ("user", "assistant", "thinking", "tool").
    ``body`` is the complete normalized content used for preview, expansion, and
    copy. Mirrors the branching in ``export._message_display_dict`` /
    ``sesh.transcript.message_material`` so the TUI and HTML views agree on what
    a message's content is.
    """
    ts = f" ({m.timestamp.strftime('%H:%M')})" if m.timestamp else ""
    ct = m.content_type
    if ct == "thinking":
        return "thinking", f"Thinking{ts}", m.thinking or ""
    if ct == "tool_use":
        tool = m.tool_name or "tool"
        return "tool", f"{tool} (call){ts}", m.tool_input or ""
    if ct == "tool_result":
        tool = m.tool_name or "tool"
        return "tool", f"{tool} (result){ts}", m.tool_output or ""
    if m.role == "user":
        return "user", f"User{ts}", m.content or ""
    if m.role == "assistant":
        return "assistant", f"Assistant{ts}", m.content or ""
    if m.role == "tool":
        tool = m.tool_name or "tool"
        return "tool", f"{tool}{ts}", m.content or ""
    return "other", f"{m.role}{ts}", m.content or ""


@dataclass
class CardEntry:
    """A pure, render-ready description of one transcript card.

    One entry becomes one :class:`MessageCard`. Agent containers are entries
    with ``agent=True`` and an empty ``body`` (their interior are separate
    ``depth == 1`` entries emitted only while the container is expanded).
    """

    key: str
    role: str
    header: str
    body: str
    depth: int = 0
    agent: bool = False
    #: For interior message cards, the key of their agent container (else None).
    parent_key: str | None = None


_ROLE_STYLE = {
    "user": "bold cyan",
    "assistant": "bold green",
    "thinking": "dim magenta",
    "tool": "bold yellow",
    "agent": "bold magenta",
    "other": "bold",
}


def _agent_header(item: TranscriptItem) -> str:
    meta = item.meta
    assert meta is not None
    desc = meta.description or meta.agent_id
    atype = meta.agent_type or "agent"
    marker = f"[{short_workflow_id(meta.workflow_id)}] " if meta.workflow_id else ""
    return f"⑂ {marker}{atype} — {desc} · {meta.message_count} msgs"


def build_rows(
    items: list[TranscriptItem], expanded: set[str]
) -> list[CardEntry]:
    """Flatten composed transcript *items* into ordered :class:`CardEntry` rows.

    Only **agent** expansion changes the row set: an expanded agent container is
    followed by its interior message cards (``depth == 1``). Message expansion is
    an in-card content change and does not appear here.
    """
    rows: list[CardEntry] = []
    for item in items:
        if item.kind == "agent":
            rows.append(
                CardEntry(
                    key=item.key,
                    role="agent",
                    header=_agent_header(item),
                    body="",
                    depth=0,
                    agent=True,
                )
            )
            if item.key in expanded:
                for interior in item.interior:
                    role, header, body = normalize_message(interior.message)
                    rows.append(
                        CardEntry(
                            key=interior.key,
                            role=role,
                            header=header,
                            body=body,
                            depth=1,
                            parent_key=item.key,
                        )
                    )
        else:
            role, header, body = normalize_message(item.message)
            rows.append(
                CardEntry(key=item.key, role=role, header=header, body=body)
            )
    return rows


class MessageCard(Static):
    """One transcript row: a header line plus preview-or-full body.

    Holds the complete ``body`` but renders only a preview until ``expanded``.
    ``active`` marks it as the view's selection cursor; ``active_match`` marks it
    as the current find hit (Phase 3). Both are purely visual (CSS classes).
    """

    can_focus = False

    DEFAULT_CSS = """
    MessageCard {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    MessageCard.-nested {
        padding-left: 3;
    }
    MessageCard.-active {
        background: $boost;
    }
    MessageCard.-active-match {
        background: $warning 30%;
    }
    """

    def __init__(self, entry: CardEntry, *, expanded: bool, highlight: str = "") -> None:
        super().__init__()
        self.entry = entry
        self.expanded = expanded
        self._highlight = highlight
        self.active_match = False
        if entry.depth > 0:
            self.add_class("-nested")

    @property
    def key(self) -> str:
        return self.entry.key

    @property
    def full_body(self) -> str:
        """The complete normalized body (what ``C`` copies)."""
        return self.entry.body

    @property
    def is_agent(self) -> bool:
        return self.entry.agent

    @property
    def expandable(self) -> bool:
        """True if there is hidden content behind a preview, or it's an agent."""
        if self.entry.agent:
            return True
        return len(self.entry.body) > PREVIEW_CHARS

    def set_expanded(self, value: bool) -> None:
        if value != self.expanded:
            self.expanded = value
            self.refresh(layout=True)

    def set_highlight(self, term: str) -> None:
        if term != self._highlight:
            self._highlight = term
            self.refresh(layout=True)

    def set_active_match(self, value: bool) -> None:
        if value != self.active_match:
            self.active_match = value
            self.set_class(value, "-active-match")

    def _highlighted(self, text: str, base_style: str = "") -> Text:
        rich = Text(text, style=base_style)
        for start, end in find_match_spans(text, self._highlight):
            style = "black on yellow" if self.active_match else "reverse"
            rich.stylize(style, start, end)
        return rich

    def render(self) -> RenderableType:
        header_text = self.entry.header
        if self.entry.agent:
            header_text = ("▾ " if self.expanded else "▸ ") + header_text
        header = Text(header_text, style=_ROLE_STYLE.get(self.entry.role, "bold"))

        parts: list[RenderableType] = [header]

        body = self.entry.body
        if self.entry.agent:
            if not self.expanded:
                parts.append(Text("[collapsed — Enter to expand]", style="dim"))
        elif body:
            if self.expanded:
                shown, omitted = body, 0
            else:
                shown, omitted = preview_and_omitted(body)
            base = "dim" if self.entry.role == "thinking" else ""
            parts.append(self._highlighted(shown, base))
            if omitted:
                parts.append(Text(omission_marker(omitted), style="dim italic"))
        return Group(*parts)


class TranscriptView(VerticalScroll):
    """Scrollable list of :class:`MessageCard` widgets with a keyboard cursor.

    The container itself is the single focus target (cards are not individual
    tab stops), so ``Tab`` moves focus between the session tree and the whole
    transcript. Within the transcript, ``↑``/``↓`` (or ``k``/``j``) move the
    selection cursor, ``Enter`` expands/collapses the selected card, and ``C``
    (an app-level binding) copies the selected card's complete body.
    """

    can_focus = True
    BORDER_TITLE = "Messages"

    BINDINGS = [
        Binding("down,j", "cursor_down", "Down", show=False),
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("enter", "toggle_expand", "Expand", show=False),
        Binding("home", "cursor_home", "Top", show=False),
        Binding("end", "cursor_end", "Bottom", show=False),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._items: list[TranscriptItem] = []
        self._rows: list[CardEntry] = []
        self._cards: list[MessageCard] = []
        self._expanded: set[str] = set()
        self._highlight: str = ""
        self._cursor: int = -1
        self._empty_message: str = "No messages found."
        self._placeholder: Static | None = None
        #: interior-message-key -> agent-container-key (for reveal_key)
        self._parent_of: dict[str, str] = {}
        #: Pure find-navigation model over the composed items (Phase 3).
        self._finder = TranscriptFinder()

    # ---- Public surface (also used by the Phase 3 find-navigation) ----------

    @property
    def expanded_keys(self) -> set[str]:
        """The set of currently-expanded card keys (stable keys)."""
        return set(self._expanded)

    @property
    def keys(self) -> list[str]:
        """Keys of all visible cards, in document order."""
        return [c.key for c in self._cards]

    @property
    def active_key(self) -> str | None:
        """Stable key of the selection cursor card, if any."""
        if 0 <= self._cursor < len(self._cards):
            return self._cards[self._cursor].key
        return None

    def card_for_key(self, key: str) -> MessageCard | None:
        for card in self._cards:
            if card.key == key:
                return card
        return None

    def set_transcript(
        self,
        items: list[TranscriptItem],
        *,
        highlight: str = "",
        empty_message: str = "No messages found.",
    ) -> None:
        """Replace the transcript, preserving expansion + cursor by stable key.

        Cards are rebuilt from *items* and the persisted expansion set, so a
        message expanded before a tool/thinking/agent toggle stays expanded if it
        still exists. The cursor is restored to the same key when possible. When
        *items* is empty, *empty_message* is shown as a non-focusable placeholder.
        """
        self._items = items
        self._highlight = highlight
        self._empty_message = empty_message
        prev_key = self.active_key
        # Prune expansion keys that no longer exist so the set can't grow forever.
        live_keys = _all_keys(items)
        self._expanded &= live_keys
        self._rebuild(preferred_key=prev_key)
        # Reconcile find state: a new highlight term jumps to its first match; an
        # unchanged term (a live append / toggle rerender) preserves the active
        # match by stable key. Repaint but do not steal scroll on a passive
        # rerender — the active card keeps its distinct style if still visible.
        if highlight != self._finder.term:
            self._finder.set_term(highlight, items)
        else:
            self._finder.recompute(items)
        self.set_active_match(self._finder.active_key)

    def set_highlight(self, term: str) -> None:
        """Update the find highlight on all cards in place (cheap)."""
        self._highlight = term
        for card in self._cards:
            card.set_highlight(term)

    def set_active_match(self, key: str | None) -> None:
        """Paint one card as the active find match; clear the rest (Phase 3)."""
        for card in self._cards:
            card.set_active_match(card.key == key)

    def reveal_key(self, key: str) -> bool:
        """Bring the card for *key* into view, expanding its container if hidden.

        Returns True if a card for *key* exists (after any needed expansion).
        Used by find-navigation to surface a match inside a collapsed sub-agent.
        """
        if self.card_for_key(key) is None:
            parent = self._parent_of.get(key)
            if parent is not None and parent not in self._expanded:
                self._expanded.add(parent)
                self._rebuild(preferred_key=self.active_key)
        card = self.card_for_key(key)
        if card is None:
            return False
        idx = self._cards.index(card)
        self._set_cursor(idx)
        return True

    def reveal_match(self, match: Match) -> bool:
        """Surface a find hit: expand its container/card and scroll it into view.

        Reveals the card for ``match.key`` (expanding a collapsed sub-agent
        container if needed via :meth:`reveal_key`). When the hit falls beyond a
        collapsed non-agent card's preview boundary, the card is expanded so the
        matched text is actually visible. Returns True if the card exists.
        """
        if not self.reveal_key(match.key):
            return False
        card = self.card_for_key(match.key)
        if card is None:
            return False
        if (
            not card.is_agent
            and match.start >= PREVIEW_CHARS
            and match.key not in self._expanded
        ):
            self._expanded.add(match.key)
            card.set_expanded(True)
        return True

    # ---- Find navigation (Phase 3) -----------------------------------------

    @property
    def find_position(self) -> tuple[int, int]:
        """``(current_1based, total)`` of the find match index."""
        return self._finder.position()

    @property
    def find_label(self) -> str:
        """Human counter string (``""`` / ``"No matches"`` / ``"3 / 17"``)."""
        return self._finder.label()

    @property
    def find_active_key(self) -> str | None:
        return self._finder.active_key

    def find(self, term: str) -> tuple[int, int]:
        """Set the find *term*, recompute matches, and reveal the first hit.

        Highlights all occurrences in place and (when the term changed) jumps to
        the first match, expanding/scrolling as needed. Returns ``find_position``.
        """
        self.set_highlight(term)
        self._finder.set_term(term, self._items)
        self._apply_active_match()
        return self._finder.position()

    def find_next(self) -> tuple[int, int]:
        """Advance to the next match (wraparound) and reveal it."""
        self._finder.next()
        self._apply_active_match()
        return self._finder.position()

    def find_prev(self) -> tuple[int, int]:
        """Step to the previous match (wraparound) and reveal it."""
        self._finder.prev()
        self._apply_active_match()
        return self._finder.position()

    def _apply_active_match(self) -> None:
        """Reveal + paint the finder's active match (or clear if none)."""
        match = self._finder.active
        if match is None:
            self.set_active_match(None)
            return
        self.reveal_match(match)
        self.set_active_match(match.key)

    # ---- Rendering / mounting ----------------------------------------------

    def _rebuild(self, *, preferred_key: str | None = None) -> None:
        self._rows = build_rows(self._items, self._expanded)
        # Map every interior key to its container, even while the container is
        # collapsed, so reveal_key() can expand the right block for a hidden hit.
        self._parent_of = {}
        for item in self._items:
            if item.kind == "agent":
                for interior in item.interior:
                    self._parent_of[interior.key] = item.key
        # Remove old cards / placeholder and mount fresh ones.
        for card in self._cards:
            card.remove()
        if self._placeholder is not None:
            self._placeholder.remove()
            self._placeholder = None
        self._cards = [
            MessageCard(
                entry,
                expanded=(entry.key in self._expanded),
                highlight=self._highlight,
            )
            for entry in self._rows
        ]
        if self._cards:
            self.mount_all(self._cards)
        else:
            self._placeholder = Static(self._empty_message, classes="-empty")
            self.mount(self._placeholder)
        # Restore cursor to the same key, else clamp.
        new_cursor = -1
        if preferred_key is not None:
            for i, card in enumerate(self._cards):
                if card.key == preferred_key:
                    new_cursor = i
                    break
        if new_cursor == -1 and self._cards:
            new_cursor = 0
        self._cursor = -1
        self._set_cursor(new_cursor)

    def _set_cursor(self, index: int) -> None:
        if not self._cards:
            self._cursor = -1
            return
        index = max(0, min(index, len(self._cards) - 1))
        if self._cursor == index:
            # Still ensure visible / active class is set (e.g. first assignment).
            self._cards[index].add_class("-active")
            self._scroll_to_card(index)
            return
        if 0 <= self._cursor < len(self._cards):
            self._cards[self._cursor].remove_class("-active")
        self._cursor = index
        self._cards[index].add_class("-active")
        self._scroll_to_card(index)

    def _scroll_to_card(self, index: int) -> None:
        try:
            self.scroll_to_widget(self._cards[index], animate=False)
        except Exception:
            pass

    # ---- Actions ------------------------------------------------------------

    def action_cursor_down(self) -> None:
        if self._cards:
            self._set_cursor(self._cursor + 1)

    def action_cursor_up(self) -> None:
        if self._cards:
            self._set_cursor(self._cursor - 1)

    def action_cursor_home(self) -> None:
        if self._cards:
            self._set_cursor(0)

    def action_cursor_end(self) -> None:
        if self._cards:
            self._set_cursor(len(self._cards) - 1)

    def action_toggle_expand(self) -> None:
        self.toggle_active()

    def toggle_active(self) -> None:
        """Expand/collapse the selection cursor card."""
        if not (0 <= self._cursor < len(self._cards)):
            return
        card = self._cards[self._cursor]
        if not card.expandable:
            return
        key = card.key
        if key in self._expanded:
            self._expanded.discard(key)
        else:
            self._expanded.add(key)
        if card.is_agent:
            # Interior rows appear/disappear -> rebuild, keep cursor on the agent.
            self._rebuild(preferred_key=key)
        else:
            card.set_expanded(key in self._expanded)

    def copy_active(self) -> str | None:
        """Return the complete body of the cursor card (never the preview)."""
        if 0 <= self._cursor < len(self._cards):
            return self._cards[self._cursor].full_body
        return None


def _all_keys(items: list[TranscriptItem]) -> set[str]:
    keys: set[str] = set()
    for item in items:
        keys.add(item.key)
        for interior in item.interior:
            keys.add(interior.key)
    return keys
