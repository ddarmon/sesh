from __future__ import annotations

from datetime import datetime, timezone

from sesh.export import format_session_html, format_session_markdown
from sesh.models import Provider
from tests.helpers import make_message, make_session


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
