from __future__ import annotations

import json
import re
from datetime import datetime, timezone

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
