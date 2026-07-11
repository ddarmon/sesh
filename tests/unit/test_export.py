from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from sesh.export import format_session_html, format_session_markdown
from sesh.models import Provider, SubagentMeta
from tests.helpers import make_message, make_session


def _subagent(**overrides):
    """Build a (SubagentMeta, interior messages) pair for export tests."""
    data = {
        "agent_id": "ag-1",
        "file_path": "/proj/sess/subagents/agent-ag-1.jsonl",
        "description": "Build the FIRE projection layer",
        "agent_type": "Explore",
        "is_fork": True,
        "tool_use_id": "toolu_42",
        "first_timestamp": datetime(2025, 1, 1, 0, 30, tzinfo=timezone.utc),
        "message_count": 42,
        "output_tokens": 1234,
    }
    interior = overrides.pop("interior", None)
    data.update(overrides)
    meta = SubagentMeta(**data)
    if interior is None:
        interior = [make_message(role="user", content="agent kickoff", timestamp=None)]
    return meta, interior


def _extract_payload(html_out: str) -> dict:
    """Pull the embedded JSON payload back out of the HTML document."""
    m = re.search(
        r'<script id="data" type="application/json">(.*?)</script>',
        html_out,
        re.S,
    )
    assert m is not None
    return json.loads(m.group(1).replace("<\\/", "</"))


def _viewer_script(html_out: str) -> str:
    """Return the final authored inline viewer script (after vendored libs)."""
    scripts = re.findall(r"<script>(.*?)</script>", html_out, re.S)
    assert scripts, "no attribute-free <script> block found"
    return scripts[-1]


def test_format_session_markdown_renders_message_types() -> None:
    """Markdown export formats text, thinking, tool call, and tool result blocks."""
    session = make_session(
        id="sess-1",
        provider=Provider.CODEX,
        project_path="/repo",
        model="gpt-4.1",
        timestamp=datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc),
    )
    messages = [
        make_message(role="user", content="hello", timestamp=None),
        make_message(role="assistant", content="hi", timestamp=None),
        make_message(
            role="assistant",
            content="",
            content_type="thinking",
            thinking="step one\nstep two",
            timestamp=None,
        ),
        make_message(
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="Read",
            tool_input='{"path":"x"}',
            timestamp=None,
        ),
        make_message(
            role="tool",
            content="",
            content_type="tool_result",
            tool_name="Read",
            tool_output="contents",
            timestamp=None,
        ),
    ]

    out = format_session_markdown(session, messages)

    assert "# Session: sess-1" in out
    assert "- **Provider:** codex" in out
    assert "## User" in out
    assert "## Assistant" in out
    assert "### Thinking" in out
    assert "> step one" in out
    assert "### Read (call)" in out
    assert "```json" in out
    assert "### Read (result)" in out
    assert "contents" in out


def test_format_session_markdown_empty_messages_still_returns_header() -> None:
    """Exporting an empty message list is valid and still includes session metadata."""
    session = make_session(id="empty", project_path="/repo")

    out = format_session_markdown(session, [])

    assert "# Session: empty" in out
    assert "- **Project:** /repo" in out


def test_format_session_html_is_one_self_contained_doc() -> None:
    """HTML export is a single document with all viewer assets inlined offline."""
    session = make_session(
        id="abcdef12",
        provider=Provider.CLAUDE,
        project_path="/repo",
        model="claude-opus",
        timestamp=datetime(2026, 6, 28, 11, 30, tzinfo=timezone.utc),
    )
    out = format_session_html(session, [make_message(role="user", content="hi")])

    # Exactly one HTML document.
    assert out.count("<html") == 1
    assert out.count("</html>") == 1
    # Vendored assets inlined (not CDN <script src=>), so it works from file://.
    assert "cdn.jsdelivr.net" not in out
    assert "markdown-it 14.1.0" in out  # markdown-it.min.js header
    assert "Highlight.js v11.9.0" in out  # highlight.min.js header
    assert "data:font/woff2;base64," in out  # KaTeX fonts inlined
    # No unresolved template placeholders.
    for token in ("__TITLE__", "__META__", "__KATEX_CSS__", "__HLJS_JS__"):
        assert token not in out
    # Header surfaces session metadata.
    assert "claude" in out
    assert "abcdef12" in out


def test_format_session_html_embeds_messages_and_preserves_math() -> None:
    """Each message is embedded as JSON; inline/display LaTeX survives verbatim."""
    session = make_session(id="s-math", provider=Provider.CODEX)
    messages = [
        make_message(role="user", content=r"What is $x^2$ and \(y\) and \[z\]?"),
        make_message(role="assistant", content="answer with `</script>` token"),
        make_message(
            role="assistant",
            content="",
            content_type="thinking",
            thinking="secret reasoning",
        ),
        make_message(
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="Bash",
            tool_input='{"cmd":"ls"}',
        ),
    ]
    out = format_session_html(session, messages)

    # Math written either way survives into the embedded JSON for client render.
    # Backslashes are JSON-escaped (\( -> \\(), but the content is intact.
    assert "$x^2$" in out
    assert r"\\(y\\)" in out
    assert r"\\[z\\]" in out
    # Each message's content is present.
    assert "answer with" in out
    assert "secret reasoning" in out
    assert "Bash" in out
    # The embedded JSON escapes "</" so it cannot terminate the data <script>.
    assert "<\\/script>" in out


def test_format_session_html_renders_with_no_messages() -> None:
    """An empty session still produces a valid self-contained document."""
    session = make_session(id="empty", provider=Provider.CLAUDE, project_path="/repo")

    out = format_session_html(session, [])

    assert out.count("<html") == 1
    assert "0 msgs" in out


def test_format_session_html_does_not_expand_tokens_in_message_content() -> None:
    """Template tokens in user content are NOT substituted (single-pass re.sub).

    This is the core safety invariant of the substitution: message text that
    happens to contain `__DATA__`, `__KATEX_JS__`, etc. must land in the
    embedded JSON verbatim, never be replaced by an asset or the data payload.
    """
    session = make_session(id="tok", provider=Provider.CLAUDE)
    sentinel = "SEN#__DATA__#__KATEX_JS__#__TITLE__#__HLJS_JS__#END"

    out = format_session_html(session, [make_message(role="user", content=sentinel)])

    # If any token were expanded, the asset/data text would split the sentinel.
    assert sentinel in out


def test_format_session_html_escapes_meta_header() -> None:
    """HTML in project_path / model is escaped in the (raw-HTML) meta header."""
    session = make_session(
        id="x",
        provider=Provider.CLAUDE,
        project_path="/x/<img src=q onerror=alert(1)>",
        model="</script><b>m</b>",
    )

    out = format_session_html(session, [])

    import re

    header = re.search(r'<header class="meta">(.*?)</header>', out, re.S).group(1)
    assert "<img src=q onerror=alert(1)>" not in header
    assert "&lt;img src=q onerror=alert(1)&gt;" in header
    assert "</script>" not in header
    assert "&lt;/script&gt;" in header


def test_format_session_html_pins_markdown_html_disabled() -> None:
    """markdown-it stays configured html:false (raw HTML escaped, not injected)."""
    out = format_session_html(make_session(id="h", provider=Provider.CLAUDE), [])

    assert "html:false" in out


def test_format_session_html_embeds_agent_entry_with_nested_messages() -> None:
    """A sub-agent becomes a kind:'agent' payload entry carrying nested messages."""
    session = make_session(id="s-agent", provider=Provider.CLAUDE)
    messages = [
        make_message(
            role="user",
            content="please investigate",
            timestamp=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        ),
        make_message(
            role="assistant",
            content="done",
            timestamp=datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc),
        ),
    ]
    meta, interior = _subagent(
        interior=[make_message(role="assistant", content="nested reply", timestamp=None)]
    )
    out = format_session_html(session, messages, [(meta, interior)])

    payload = _extract_payload(out)
    agents = [e for e in payload["messages"] if e.get("kind") == "agent"]
    assert len(agents) == 1
    agent = agents[0]
    assert agent["role"] == "agent"
    assert agent["agent_type"] == "Explore"
    assert agent["message_count"] == 42
    assert "Build the FIRE projection layer" in agent["label"]
    assert "42 msgs" in agent["label"]
    # Nested interior is mapped through the same message display shape.
    assert [m["content"] for m in agent["messages"]] == ["nested reply"]
    # Anchored chronologically: spawn at 00:30 lands between the two main msgs.
    kinds = [e.get("kind") for e in payload["messages"]]
    assert kinds == ["text", "agent", "text"]
    # The recursive renderer and agent styling are present.
    assert "renderThread" in out
    assert "agent-thread" in out


def test_format_session_html_workflow_agent_label_and_id() -> None:
    """A workflow sub-agent's HTML payload carries workflow_id and a [wf] label."""
    session = make_session(id="s-wf", provider=Provider.CLAUDE)
    messages = [make_message(role="user", content="go", timestamp=None)]
    meta, interior = _subagent(
        workflow_id="wf_a1be27ca-98b",
        agent_type="Worker",
        description="Do the step",
    )
    out = format_session_html(session, messages, [(meta, interior)])

    payload = _extract_payload(out)
    agent = next(e for e in payload["messages"] if e.get("kind") == "agent")
    assert agent["workflow_id"] == "wf_a1be27ca-98b"
    assert "[wf_a1be27ca]" in agent["label"]
    assert "Do the step" in agent["label"]


def test_format_session_markdown_workflow_agent_header() -> None:
    """Markdown export prefixes a workflow sub-agent and lists the workflow id."""
    session = make_session(id="s-wf-md", provider=Provider.CLAUDE)
    messages = [make_message(role="user", content="go", timestamp=None)]
    meta, interior = _subagent(workflow_id="wf_a1be27ca-98b", description="Do the step")
    out = format_session_markdown(session, messages, [(meta, interior)])
    assert "## Sub-agent: [wf_a1be27ca] Do the step" in out
    assert "**Workflow:** wf_a1be27ca-98b" in out


def test_short_workflow_id_shortens_suffix() -> None:
    """short_workflow_id drops the trailing -suffix; plain/empty ids pass through."""
    from sesh.models import short_workflow_id

    assert short_workflow_id("wf_a1be27ca-98b") == "wf_a1be27ca"
    assert short_workflow_id("plainid") == "plainid"
    assert short_workflow_id("") == ""


def test_format_session_html_unanchorable_agent_trails() -> None:
    """A sub-agent with no timestamp is appended after the main thread."""
    session = make_session(id="s-trail", provider=Provider.CLAUDE)
    messages = [
        make_message(
            role="user",
            content="hi",
            timestamp=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        ),
    ]
    meta, interior = _subagent(first_timestamp=None)
    out = format_session_html(session, messages, [(meta, interior)])

    payload = _extract_payload(out)
    kinds = [e.get("kind") for e in payload["messages"]]
    assert kinds == ["text", "agent"]


def test_compose_thread_mixed_naive_and_aware_timestamps() -> None:
    """[finding 2] Anchoring a sub-agent must not crash when a main-thread
    timestamp came from a no-offset (naive-source) stamp and the sub-agent's
    came from a Z stamp. Both parse to aware datetimes now, so the ``>``
    comparison in _compose_thread is safe."""
    from sesh.export import _compose_thread
    from sesh.providers.claude import _parse_timestamp

    messages = [
        make_message(role="user", content="early",
                     timestamp=_parse_timestamp("2026-07-05T11:00:00")),  # no offset
        make_message(role="assistant", content="late",
                     timestamp=_parse_timestamp("2026-07-05T13:00:00Z")),  # aware
    ]
    meta, interior = _subagent(
        first_timestamp=_parse_timestamp("2026-07-05T12:00:00Z")
    )

    composed = _compose_thread(messages, [(meta, interior)])  # must not raise
    kinds = [e.get("kind") for e in composed]
    # Sub-agent anchored between the 11:00 and 13:00 messages.
    assert kinds == ["text", "agent", "text"]


def test_format_session_html_no_subagents_matches_baseline() -> None:
    """Passing no sub-agents produces the same payload as omitting the arg."""
    session = make_session(id="s-none", provider=Provider.CLAUDE)
    messages = [make_message(role="user", content="hi", timestamp=None)]

    a = _extract_payload(format_session_html(session, messages))
    b = _extract_payload(format_session_html(session, messages, []))
    assert a == b
    assert all(e.get("kind") != "agent" for e in a["messages"])


def test_format_session_markdown_appends_subagent_section() -> None:
    """Markdown export gains a '## Sub-agent:' section with demoted interior."""
    session = make_session(id="s-md", provider=Provider.CLAUDE)
    messages = [make_message(role="user", content="main question", timestamp=None)]
    meta, _ = _subagent()
    interior = [
        make_message(role="user", content="sub question", timestamp=None),
        make_message(role="assistant", content="sub answer", timestamp=None),
        make_message(
            role="assistant",
            content="",
            content_type="thinking",
            thinking="nested thought",
            timestamp=None,
        ),
    ]
    out = format_session_markdown(session, messages, [(meta, interior)])

    assert "## User" in out  # main thread heading (H2)
    assert "## Sub-agent: Build the FIRE projection layer (ag-1)" in out
    assert "**Type:** Explore" in out
    assert "**Messages:** 42" in out
    assert "**Output tokens:** 1,234" in out
    # Interior headings are demoted one level.
    assert "### User" in out  # sub-agent user (H3)
    assert "### Assistant" in out
    assert "#### Thinking" in out  # sub-agent thinking (H4)
    assert "> nested thought" in out


def test_format_session_markdown_no_subagents_unchanged() -> None:
    """Omitting sub-agents leaves the markdown identical to the two-arg call."""
    session = make_session(id="s-md2", provider=Provider.CLAUDE)
    messages = [make_message(role="user", content="hi", timestamp=None)]

    assert format_session_markdown(session, messages) == format_session_markdown(
        session, messages, []
    )
    assert "Sub-agent" not in format_session_markdown(session, messages)


def test_html_message_keys_stay_stable_when_rows_are_inserted() -> None:
    session = make_session()
    existing = make_message(
        role="assistant",
        content="",
        content_type="tool_use",
        tool_name="bash",
        tool_input="pwd",
        timestamp=None,
    )
    before = _extract_payload(format_session_html(session, [existing]))
    after = _extract_payload(
        format_session_html(
            session,
            [make_message(content="inserted", timestamp=None), existing],
        )
    )

    assert before["messages"][0]["key"] == after["messages"][1]["key"]


def test_html_payload_reuses_shared_transcript_keys() -> None:
    """The embedded payload keys come from sesh.transcript, not a private algo."""
    from sesh import transcript
    from sesh.export import session_html_payload

    session = make_session(id="s-keys", provider=Provider.CLAUDE)
    msgs = [
        make_message(role="user", content="hi", timestamp=None),
        make_message(role="assistant", content="yo", timestamp=None),
    ]
    payload = session_html_payload(session, msgs)
    expected = transcript.assign_message_keys(msgs)
    assert [e["key"] for e in payload["messages"]] == expected


def test_html_payload_agent_key_is_shared_container_anchor() -> None:
    from sesh import transcript
    from sesh.export import session_html_payload

    session = make_session(id="s-agent-key", provider=Provider.CLAUDE)
    messages = [
        make_message(role="user", content="go",
                     timestamp=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)),
        make_message(role="assistant", content="ok",
                     timestamp=datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)),
    ]
    meta, interior = _subagent(
        interior=[make_message(role="assistant", content="nested", timestamp=None)]
    )
    payload = session_html_payload(session, messages, [(meta, interior)])
    agent = next(e for e in payload["messages"] if e.get("kind") == "agent")
    assert agent["key"] == transcript.agent_anchor(meta.agent_id)
    # Interior message keys are namespaced by the sub-agent's agent_id.
    assert agent["messages"][0]["key"] == transcript.assign_message_keys(
        interior, namespace=meta.agent_id
    )[0]


def test_format_session_html_embeds_live_configuration() -> None:
    out = format_session_html(
        make_session(),
        [make_message(content="live")],
        live_api="./api/session",
        live_revision=7,
        live_poll_ms=800,
    )
    payload = _extract_payload(out)
    assert payload["live"] == {
        "api": "./api/session",
        "revision": 7,
        "poll_ms": 800,
    }
    assert 'id="live-status"' in out
    # A single self-rescheduling timer (never setInterval) prevents overlapping
    # polls; pause/follow/refresh controls are present in the live toolbar.
    script = _viewer_script(out)
    assert "window.setTimeout" in script
    assert "setInterval" not in script
    assert 'id="toggle-pause"' in out
    assert 'id="toggle-follow"' in out
    assert 'id="refresh-now"' in out


# --- Phase 4: HTML anchors and reader controls ---------------------------


def _thread_message_ids(html_out: str) -> list[str]:
    """DOM ids the viewer JS will assign are the payload message/agent keys."""

    def walk(entries: list[dict]) -> list[str]:
        ids: list[str] = []
        for e in entries:
            if "key" in e:
                ids.append(e["key"])
            if e.get("kind") == "agent":
                ids.extend(walk(e.get("messages", [])))
        return ids

    return walk(_extract_payload(html_out)["messages"])


def test_html_anchor_ids_are_unique_and_fragment_safe() -> None:
    """Every message/agent key (which becomes a DOM id) is unique and safe."""
    session = make_session(id="s-anchor", provider=Provider.CLAUDE)
    dup = make_message(role="user", content="same body", timestamp=None)
    messages = [
        make_message(role="user", content="same body", timestamp=None),
        make_message(role="assistant", content="answer", timestamp=None),
        dup,  # identical content to the first -> distinct occurrence key
    ]
    meta, interior = _subagent(
        interior=[make_message(role="assistant", content="nested", timestamp=None)]
    )
    out = format_session_html(session, messages, [(meta, interior)])

    ids = _thread_message_ids(out)
    # Duplicate content still yields distinct keys (occurrence counter).
    assert len(ids) == len(set(ids))
    # Keys are DOM-id / URL-fragment safe by construction.
    for key in ids:
        assert re.fullmatch(r"[A-Za-z0-9_-]+", key), key
    # The viewer assigns the key as the element id and reveals #fragments.
    script = _viewer_script(out)
    assert "wrap.id = key" in script
    assert "location.hash" in script
    assert "anchor-active" in script


def test_html_details_open_state_restored_by_stable_key() -> None:
    """Live rerender restores open <details> by the stable key, not position."""
    out = format_session_html(
        make_session(id="s-open", provider=Provider.CLAUDE),
        [make_message(content="x")],
        live_api="./api/session",
    )
    script = _viewer_script(out)
    # <details> carry the stable key, and restoration keys off dataset.liveKey.
    assert "det.dataset.liveKey = key" in script
    assert "open.has(node.dataset.liveKey)" in script
    # No list-index fallback: the key is used directly as identity.
    assert "|| index" not in script


def test_html_live_rerender_reapplies_anchor_without_reforcing() -> None:
    """[finding 6] A live poll re-marks the anchored card but never re-reveals it.

    `applyPayload` must NOT call `applyHash` (which force-opens the anchored
    card's <details> and scrolls); it uses `reapplyAnchor`, which only re-adds
    the highlight class. Full reveal stays reserved for initial load / hashchange.
    """
    out = format_session_html(
        make_session(id="s-anchor-live", provider=Provider.CLAUDE),
        [make_message(content="x")],
        live_api="./api/session",
    )
    script = _viewer_script(out)
    # The reveal-only helper exists and is what applyPayload calls.
    assert "function reapplyAnchor()" in script
    assert "reapplyAnchor();" in script
    # applyPayload no longer force-reveals on every poll.
    assert "applyHash(false)" not in script
    # reapplyAnchor re-adds the highlight class but does not open <details>
    # (revealElement) or scroll.
    anchor_body = script.split("function reapplyAnchor()", 1)[1].split("}", 1)[0]
    assert "anchor-active" in anchor_body
    assert "revealElement" not in anchor_body
    assert "scrollIntoView" not in anchor_body
    # Initial load and real hashchange still perform the full reveal.
    assert "applyHash(true)" in script
    assert 'addEventListener(\'hashchange\', ()=> applyHash(true))' in script


def test_html_no_per_card_copy_or_link_chrome() -> None:
    """Per-card hover Copy/Link buttons were removed; only the anchors remain."""
    out = format_session_html(
        make_session(id="s-copy", provider=Provider.CLAUDE),
        [make_message(content="hello")],
    )
    script = _viewer_script(out)
    # No per-message action chrome or its link machinery survives.
    assert "buildActions" not in script
    assert "msg-actions" not in out
    assert "linkFor" not in script
    # Anchors stay: every card still becomes an addressable #fragment target.
    assert "wrap.id = key" in script
    assert "anchor-active" in script


def test_html_static_export_hides_live_only_controls() -> None:
    """A static export exposes find + count but no live-only server controls."""
    out = format_session_html(
        make_session(id="s-static", provider=Provider.CLAUDE),
        [make_message(content="hi")],
    )
    # Find controls and the message count work from file:// without a server.
    assert 'id="find-input"' in out
    assert 'id="msg-count"' in out
    # Live-only toolbar controls are absent from the static document.
    for control in (
        'id="live-status"',
        'id="toggle-pause"',
        'id="toggle-follow"',
        'id="refresh-now"',
        'id="new-msgs"',
        'id="updated-at"',
    ):
        assert control not in out


def test_html_live_export_shows_full_toolbar() -> None:
    """A live document adds the live-only controls on top of find + count."""
    out = format_session_html(
        make_session(id="s-live", provider=Provider.CLAUDE),
        [make_message(content="hi")],
        live_api="./api/session",
    )
    for control in (
        'id="find-input"',
        'id="msg-count"',
        'id="live-status"',
        'id="toggle-pause"',
        'id="toggle-follow"',
        'id="refresh-now"',
        'id="new-msgs"',
        'id="updated-at"',
    ):
        assert control in out


def test_html_find_navigation_is_present() -> None:
    """The transcript find implements counted, wrap-around next/prev with reveal."""
    out = format_session_html(
        make_session(id="s-find", provider=Provider.CLAUDE),
        [make_message(content="find me")],
    )
    script = _viewer_script(out)
    assert "recomputeMatches" in script
    assert "gotoMatch" in script
    # Match count "i / n" display.
    assert "' / '" in script
    # Reveal a hidden match by opening its own + ancestor <details>.
    assert "revealElement" in script
    # Find matches against the body-only match map, not the card's textContent,
    # so only the message body is ever counted as a hit.
    assert "matchText.get(el.dataset.key)" in script
    assert "el.textContent" not in script


def test_html_find_scopes_matching_to_message_body() -> None:
    """[finding 5] The find map is populated with body-only text per key, so only
    the message body (never rendered chrome) can be matched."""
    out = format_session_html(
        make_session(id="s-find2", provider=Provider.CLAUDE),
        [make_message(content="body text")],
    )
    script = _viewer_script(out)
    # A per-key map holds the lowercased body used for matching.
    assert "const matchText = new Map();" in script
    assert "matchText.set(key, matchSource(m).toLowerCase())" in script


def test_html_tool_message_match_field_is_raw_body() -> None:
    """[finding 4] Tool messages carry a `match` field with the raw (unfenced)
    body matching the TUI, while `content` keeps the render-time code fence."""
    out = format_session_html(
        make_session(id="s-matchfield", provider=Provider.CLAUDE),
        [
            make_message(
                role="assistant",
                content="",
                content_type="tool_use",
                tool_name="bash",
                tool_input='{"cmd": "ls"}',
            ),
            make_message(
                role="user",
                content="",
                content_type="tool_result",
                tool_name="bash",
                tool_output="file-a\nfile-b",
            ),
            make_message(role="assistant", content="plain answer"),
        ],
    )
    payload = _extract_payload(out)
    tool_use = next(m for m in payload["messages"] if "(call)" in m["label"])
    tool_res = next(m for m in payload["messages"] if "(result)" in m["label"])
    text_msg = next(m for m in payload["messages"] if m["label"].startswith("Assistant"))
    # Raw body in `match`, fenced body in `content`.
    assert tool_use["match"] == '{"cmd": "ls"}'
    assert tool_use["content"] == '```json\n{"cmd": "ls"}\n```'
    assert tool_res["match"] == "file-a\nfile-b"
    assert tool_res["content"] == "```\nfile-a\nfile-b\n```"
    # Plain text messages don't carry a redundant `match` field.
    assert "match" not in text_msg


def test_html_find_source_prefers_match_over_content() -> None:
    """[finding 4] The find match-source helper prefers m.match (raw) over
    m.content, so a query hits the same unfenced bytes the TUI would."""
    out = format_session_html(
        make_session(id="s-matchprefer", provider=Provider.CLAUDE),
        [make_message(content="hi")],
    )
    script = _viewer_script(out)
    # A shared helper resolves the raw body, preferring `match` when present.
    assert "function matchSource(m){ return (m.match != null ? m.match : m.content) || ''; }" in script
    # The find map is populated from it.
    assert "matchText.set(key, matchSource(m).toLowerCase())" in script


def test_html_message_count_excludes_agent_containers() -> None:
    """[finding 3] The toolbar message count and new-badge count only real
    messages, never kind:'agent' container entries."""
    out = format_session_html(
        make_session(id="s-count", provider=Provider.CLAUDE),
        [make_message(content="hi")],
    )
    script = _viewer_script(out)
    # A helper filters out agent containers, and both the initial count and the
    # live-update added-count derive from it (not raw key-list length).
    assert "function messageCount(list){" in script
    assert "(list||[]).filter((m)=> m.kind !== 'agent').length" in script
    assert "let renderedCount = messageCount(data.messages);" in script
    assert "const newMsgCount = messageCount(list);" in script
    assert "const added = Math.max(0, newMsgCount - renderedCount);" in script


def test_html_live_append_guards_subagent_interior_fingerprint() -> None:
    """[finding 1] The append fast-path falls through to a full rerender when a
    shared sub-agent container's interior fingerprint changed, so stale interior
    (and its `· N msgs` label) can't survive a pure-append poll."""
    out = format_session_html(
        make_session(id="s-fp", provider=Provider.CLAUDE),
        [make_message(content="hi")],
        live_api="./api/session",
    )
    script = _viewer_script(out)
    # A fingerprint keyed by agent container id (joined interior keys).
    assert "function agentFingerprints(list){" in script
    assert "let topFingerprints = agentFingerprints(data.messages);" in script
    # The fast-path is gated on both the prefix check AND unchanged interiors.
    assert "const interiorChanged = Object.keys(topFingerprints).some(" in script
    assert "&& !interiorChanged;" in script
    # Fingerprints refresh after each reconcile.
    assert "topFingerprints = newFingerprints;" in script


def test_html_live_config_carries_updated_at_when_provided() -> None:
    """Live config includes updated_at only when the caller passes it."""
    without = _extract_payload(
        format_session_html(
            make_session(), [make_message(content="x")], live_api="./api/session"
        )
    )
    assert "updated_at" not in without["live"]

    with_ts = _extract_payload(
        format_session_html(
            make_session(),
            [make_message(content="x")],
            live_api="./api/session",
            live_updated_at="2026-07-10T12:00:00+00:00",
        )
    )
    assert with_ts["live"]["updated_at"] == "2026-07-10T12:00:00+00:00"


def test_html_markdown_stays_html_disabled_and_escapes_script() -> None:
    """Security invariants preserved: html:false and </ escaping in embedded JSON."""
    out = format_session_html(
        make_session(id="s-sec", provider=Provider.CLAUDE),
        [make_message(role="user", content="</script><b>x</b>")],
    )
    assert "html:false" in out
    assert "<\\/script>" in out
    # Raw content is assigned via textContent / md.render, never innerHTML of raw.
    script = _viewer_script(out)
    assert "sum.textContent" in script


def test_viewer_script_passes_node_check() -> None:
    """The final inline viewer script is syntactically valid JS (node --check)."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")

    out = format_session_html(
        make_session(id="s-node", provider=Provider.CLAUDE),
        [make_message(content="hi")],
        live_api="./api/session",
    )
    script = _viewer_script(out)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        result = subprocess.run(
            [node, "--check", path], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
    finally:
        import os

        os.unlink(path)


def _meta_header(html_out: str) -> str:
    """Return the inner HTML of the viewer's ``header.meta`` block."""
    m = re.search(r'<header class="meta">(.*?)</header>', html_out, re.S)
    assert m is not None
    return m.group(1)


def test_html_meta_header_full_fields() -> None:
    """The meta header carries provider, model, project, host, id+copy, time,
    duration, counts, and both token totals when all are available."""
    session = make_session(
        id="sess-abc-123",
        provider=Provider.CLAUDE,
        project_path="/repo/thing",
        model="claude-opus-4-8",
        start_timestamp=datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc),
        timestamp=datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc),
        input_tokens=1000,
        output_tokens=200,
        cumulative_input_tokens=5000,
        host="laptop",
    )
    meta, interior = _subagent()
    out = format_session_html(
        session,
        [make_message(content="hi"), make_message(content="yo")],
        subagents=[(meta, interior)],
    )
    header = _meta_header(out)
    assert "<b>claude</b>" in header
    assert "model <code>claude-opus-4-8</code>" in header
    assert "<code>/repo/thing</code>" in header
    assert "host <code>laptop</code>" in header
    assert "<code>sess-abc-123</code>" in header
    # Copy-ID control carries the id in data-copy and reuses the clipboard helper.
    assert 'id="copy-session-id"' in header
    assert 'data-copy="sess-abc-123"' in header
    # Start → end range plus duration.
    assert "2026-07-10 14:00 → 15:30 (1h)" in header
    assert "2 msgs" in header
    assert "⑂1 sub-agents" in header
    # ctx = input+output = 1,200; cumulative = cumulative_input+output = 5,200.
    assert "1,200 ctx tokens" in header
    assert "5,200 cumulative" in header


def test_html_meta_header_omits_empty_fields() -> None:
    """Model, host, sub-agents, and token totals are omitted when unavailable."""
    session = make_session(
        id="s-min",
        provider=Provider.CURSOR,
        project_path="/p",
        model=None,
        host=None,
        input_tokens=None,
        output_tokens=None,
        cumulative_input_tokens=None,
    )
    header = _meta_header(format_session_html(session, [make_message(content="x")]))
    assert "model <code>" not in header
    assert "host <code>" not in header
    assert "sub-agents" not in header
    assert "ctx tokens" not in header
    assert "cumulative" not in header
    # Core fields still present.
    assert "<b>cursor</b>" in header
    assert 'id="copy-session-id"' in header
    assert "1 msgs" in header


def test_html_meta_header_escapes_session_id() -> None:
    """A hostile session id is html-escaped in both the code span and data-copy."""
    session = make_session(id='a"><b>x', provider=Provider.CLAUDE)
    header = _meta_header(format_session_html(session, [make_message(content="x")]))
    assert "<b>x" not in header.replace("&lt;b&gt;x", "")
    assert "a&quot;&gt;&lt;b&gt;x" in header


def test_html_meta_header_copy_button_wired_in_script() -> None:
    """The inline script wires the Copy-ID button through the clipboard helper."""
    out = format_session_html(
        make_session(id="s-btn", provider=Provider.CLAUDE),
        [make_message(content="x")],
    )
    script = _viewer_script(out)
    assert "copy-session-id" in script
    assert "copyText(copyIdBtn.dataset.copy" in script


def test_format_duration_and_time_range_helpers() -> None:
    """Duration buckets (m/h/d, empty under a minute) and range formatting."""
    from sesh.export import format_duration, format_time_range

    d0 = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    assert format_duration(None, d0) == ""
    assert format_duration(d0, d0) == ""  # under a minute
    assert format_duration(d0, datetime(2026, 7, 10, 12, 45, tzinfo=timezone.utc)) == "45m"
    assert format_duration(d0, datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)) == "2h"
    assert format_duration(d0, datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)) == "3d"

    # Same-day range abbreviates the end; no start falls back to the end stamp.
    same = make_session(
        start_timestamp=d0,
        timestamp=datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc),
    )
    assert format_time_range(same) == "2026-07-10 12:00 → 13:00 (1h)"
    endonly = make_session(start_timestamp=None, timestamp=d0)
    assert format_time_range(endonly) == "2026-07-10 12:00"
