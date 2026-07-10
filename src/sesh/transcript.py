"""Provider-neutral stable transcript identity.

This module is the single source of truth for *stable message keys* --- the
internal identifiers the TUI (expansion state, find navigation) and the HTML
viewer (anchors, open-``<details>`` restoration, live reconciliation) use to
track a message across rerenders. It is deliberately pure and free of any
Textual / rendering / I-O dependency so it can be heavily unit tested and
shared by both renderers.

Design
------

A message's key is derived from a *thread namespace* plus a content digest
plus a duplicate-occurrence counter:

    ``{namespace}-{digest}-{occurrence}``

where ``digest`` is a short SHA-1 over the message's role, content type, tool
name, timestamp, system flag, and full normalized content, and ``occurrence``
disambiguates messages whose digests collide (the same content appearing more
than once). The current visible-list *index* is never part of identity, so a
key survives insertion, reordering, and appends.

Sub-agent interior keys are namespaced by ``agent_id`` (see
:data:`MAIN_NAMESPACE` for the main thread and :func:`agent_anchor` for the
container's own anchor), so identical content in two different sub-agents ---
or in a sub-agent and the main thread --- never shares a key.

Stability under visibility filtering
------------------------------------

The plan requires a message's key to be identical whether or not tool /
thinking messages are currently visible. Occurrence counters are assigned by
walking the list handed to :func:`assign_message_keys` / :func:`compose_transcript`.
Callers may hand in either the full message list or a list already filtered by
``tools`` / ``thinking`` visibility: **both produce the same key for every
surviving message.** This holds because the per-message digest incorporates
every axis the visibility filter keys on (``content_type`` and the
``is_system`` flag; see :func:`sesh.models.filter_messages`). Two messages that
share a digest therefore share a filter verdict --- they are kept together or
dropped together --- so a surviving message's occurrence index among
identical-digest siblings cannot shift when other content types are hidden or
revealed. (:func:`assign_message_keys` computing over the visible list is thus
provably equivalent to computing over the full list for every message that is
actually rendered.) Consumers that want to be maximally explicit may still pass
the full unfiltered list and filter afterward; the keys will match.

These keys are internal viewer identifiers, not permanent provider IDs and not
a public storage schema. They may change format between releases.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from sesh.models import Message, SubagentMeta

#: Thread namespace for the main (non-sub-agent) transcript.
MAIN_NAMESPACE = "main"

_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


def _safe_namespace(namespace: str) -> str:
    """Collapse a namespace to a compact ``[A-Za-z0-9_]`` token.

    Sub-agent namespaces are ``agent_id`` values, which are already gated to a
    traversal-safe charset upstream; normalizing here keeps derived DOM ids /
    URL fragments (Phase 4) safe without the caller having to care.
    """
    cleaned = _UNSAFE.sub("_", namespace).strip("_")
    return cleaned or "ns"


def message_material(message: Message) -> str:
    """Return the canonical, filter-complete material a digest is taken over.

    Includes ``content_type`` and ``is_system`` (the two visibility-filter
    axes) so identical-digest messages always share a filter verdict, which is
    what makes occurrence counters stable across tool/thinking toggles.
    """
    ct = message.content_type
    if ct == "thinking":
        body = message.thinking or ""
    elif ct == "tool_use":
        body = message.tool_input or ""
    elif ct == "tool_result":
        body = message.tool_output or ""
    else:
        body = message.content or ""
    ts = message.timestamp.isoformat() if message.timestamp is not None else ""
    parts = [
        ct,
        message.role or "",
        message.tool_name or "",
        ts,
        "sys" if message.is_system else "",
        body,
    ]
    return json.dumps(parts, ensure_ascii=False)


def message_digest(message: Message) -> str:
    """Short, deterministic content digest for a single message (no namespace)."""
    return hashlib.sha1(message_material(message).encode("utf-8")).hexdigest()[:12]


def message_key(namespace: str, digest: str, occurrence: int) -> str:
    """Compose a stable key from its parts. Inverse-free / opaque to consumers."""
    return f"{_safe_namespace(namespace)}-{digest}-{occurrence}"


def agent_anchor(agent_id: str) -> str:
    """Stable anchor/key for a sub-agent *container* (not its interior)."""
    return f"agent-{_safe_namespace(agent_id)}"


def assign_message_keys(
    messages: list[Message], *, namespace: str = MAIN_NAMESPACE
) -> list[str]:
    """Return one stable key per message, occurrence-counted within ``messages``.

    Occurrence is counted per digest, so duplicate content gets distinct keys
    (``...-0``, ``...-1``, ...). See the module docstring for why counting over
    a visibility-filtered list yields the same keys as counting over the full
    list.
    """
    occurrences: dict[str, int] = {}
    keys: list[str] = []
    for m in messages:
        digest = message_digest(m)
        occ = occurrences.get(digest, 0)
        occurrences[digest] = occ + 1
        keys.append(message_key(namespace, digest, occ))
    return keys


@dataclass(frozen=True)
class TranscriptItem:
    """One composed, keyed transcript entry.

    ``kind == "message"``: ``message`` is set, ``meta`` / ``interior`` empty.
    ``kind == "agent"``:   ``meta`` is the sub-agent metadata, ``interior`` is
    the tuple of keyed interior :class:`TranscriptItem` messages, and ``key`` is
    the container anchor (:func:`agent_anchor`).
    """

    kind: str
    key: str
    message: Message | None = None
    meta: SubagentMeta | None = None
    interior: tuple[TranscriptItem, ...] = field(default_factory=tuple)


def compose_transcript(
    messages: list[Message],
    subagents: list[tuple[SubagentMeta, list[Message]]] | None = None,
    *,
    namespace: str = MAIN_NAMESPACE,
) -> list[TranscriptItem]:
    """Compose messages + Claude sub-agents into an ordered, keyed item list.

    Main-thread messages are keyed under ``namespace``. Each sub-agent becomes
    one ``kind == "agent"`` container (keyed by :func:`agent_anchor`) whose
    ``interior`` messages are keyed under the sub-agent's ``agent_id``. Sub-agent
    containers are anchored chronologically --- spliced just before the first
    main-thread message with a later timestamp --- matching the historical
    ``export._compose_thread`` / ``app.splice_subagent_threads`` behavior;
    sub-agents with no timestamp trail after the whole thread.

    ``messages`` (and each sub-agent's interior) may be pre-filtered for
    tool/thinking visibility by the caller; keys remain stable regardless (see
    the module docstring).
    """
    main_keys = assign_message_keys(messages, namespace=namespace)
    base: list[TranscriptItem] = [
        TranscriptItem(kind="message", key=key, message=m)
        for key, m in zip(main_keys, messages)
    ]
    if not subagents:
        return base

    anchored: list[tuple[int, TranscriptItem]] = []
    trailing: list[TranscriptItem] = []
    for meta, interior in subagents:
        interior_keys = assign_message_keys(interior, namespace=meta.agent_id)
        interior_items = tuple(
            TranscriptItem(kind="message", key=key, message=m)
            for key, m in zip(interior_keys, interior)
        )
        container = TranscriptItem(
            kind="agent",
            key=agent_anchor(meta.agent_id),
            meta=meta,
            interior=interior_items,
        )
        ts = meta.first_timestamp
        if ts is None:
            trailing.append(container)
            continue
        idx = len(messages)
        for i, m in enumerate(messages):
            if m.timestamp is not None and m.timestamp > ts:
                idx = i
                break
        anchored.append((idx, container))

    anchored.sort(key=lambda t: t[0])
    result: list[TranscriptItem] = []
    ai = 0
    for i in range(len(base) + 1):
        while ai < len(anchored) and anchored[ai][0] == i:
            result.append(anchored[ai][1])
            ai += 1
        if i < len(base):
            result.append(base[i])
    result.extend(trailing)
    return result
