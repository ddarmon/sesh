"""Shared session export formatting."""

from __future__ import annotations

from sesh.models import Message, SessionMeta


def format_session_markdown(session: SessionMeta, messages: list[Message]) -> str:
    """Render a session + messages as Markdown."""
    lines: list[str] = [
        f"# Session: {session.id}",
        "",
        f"- **Provider:** {session.provider.value}",
        f"- **Project:** {session.project_path}",
    ]
    if session.model:
        lines.append(f"- **Model:** {session.model}")
    lines.append(f"- **Date:** {session.timestamp.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    for m in messages:
        ts = f" ({m.timestamp.strftime('%H:%M')})" if m.timestamp else ""

        if m.content_type == "thinking":
            lines.append(f"### Thinking{ts}")
            lines.append("")
            for line in (m.thinking or "").splitlines():
                lines.append(f"> {line}")
            lines.append("")
            continue

        if m.content_type == "tool_use":
            tool = m.tool_name or "tool"
            lines.append(f"### {tool} (call){ts}")
            lines.append("")
            lines.append("```json")
            lines.append(m.tool_input or "")
            lines.append("```")
            lines.append("")
            continue

        if m.content_type == "tool_result":
            tool = m.tool_name or "tool"
            lines.append(f"### {tool} (result){ts}")
            lines.append("")
            lines.append(m.tool_output or "")
            lines.append("")
            continue

        if m.role == "user":
            lines.append(f"## User{ts}")
            lines.append("")
            lines.append(m.content)
            lines.append("")
            continue

        if m.role == "assistant":
            lines.append(f"## Assistant{ts}")
            lines.append("")
            lines.append(m.content)
            lines.append("")
            continue

        if m.role == "tool":
            tool = m.tool_name or "tool"
            lines.append(f"### {tool}{ts}")
            lines.append("")
            lines.append(m.content)
            lines.append("")
            continue

        lines.append(f"## {m.role}{ts}")
        lines.append("")
        lines.append(m.content)
        lines.append("")

    return "\n".join(lines)
