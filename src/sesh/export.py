"""Shared session export formatting."""

from __future__ import annotations

import html
import json
import re
from importlib.resources import files

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
    if session.input_tokens is not None or session.output_tokens is not None:
        ctx = (session.input_tokens or 0) + (session.output_tokens or 0)
        lines.append(
            f"- **Context:** {ctx:,} tokens "
            f"({session.input_tokens or 0:,} in / {session.output_tokens or 0:,} out)"
        )
    if session.cumulative_input_tokens is not None:
        cumul = (session.cumulative_input_tokens or 0) + (session.output_tokens or 0)
        lines.append(
            f"- **Cumulative:** {cumul:,} tokens "
            f"({session.cumulative_input_tokens or 0:,} in / {session.output_tokens or 0:,} out)"
        )
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


def _load_asset(name: str) -> str:
    """Read a vendored viewer asset bundled under ``sesh/viewer_assets``."""
    return files("sesh.viewer_assets").joinpath(name).read_text(encoding="utf-8")


def _html_messages(messages: list[Message]) -> list[dict]:
    """Map ``Message`` objects to the display dicts the viewer JS consumes.

    Each dict has ``role`` (CSS class), ``label`` (heading text), ``kind``
    (``"text"`` | ``"tool"`` | ``"thinking"`` — drives collapsible wrapping),
    and ``content`` (raw Markdown rendered client-side). The branching mirrors
    ``format_session_markdown``.
    """
    out: list[dict] = []
    for m in messages:
        ts = f" ({m.timestamp.strftime('%H:%M')})" if m.timestamp else ""

        if m.content_type == "thinking":
            out.append({
                "role": "tool",
                "label": f"Thinking{ts}",
                "kind": "thinking",
                "content": m.thinking or "",
            })
            continue

        if m.content_type == "tool_use":
            tool = m.tool_name or "tool"
            out.append({
                "role": "tool",
                "label": f"{tool} (call){ts}",
                "kind": "tool",
                "content": f"```json\n{m.tool_input or ''}\n```",
            })
            continue

        if m.content_type == "tool_result":
            tool = m.tool_name or "tool"
            out.append({
                "role": "tool",
                "label": f"{tool} (result){ts}",
                "kind": "tool",
                "content": f"```\n{m.tool_output or ''}\n```",
            })
            continue

        if m.role == "user":
            out.append({"role": "user", "label": f"User{ts}", "kind": "text", "content": m.content})
            continue

        if m.role == "assistant":
            out.append({"role": "assistant", "label": f"Assistant{ts}", "kind": "text", "content": m.content})
            continue

        if m.role == "tool":
            tool = m.tool_name or "tool"
            out.append({"role": "tool", "label": f"{tool}{ts}", "kind": "tool", "content": m.content})
            continue

        out.append({"role": m.role, "label": f"{m.role}{ts}", "kind": "text", "content": m.content})

    return out


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__KATEX_CSS__</style>
<style>__HLJS_CSS__</style>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
         background:#fff; color:#1f2328; }
  @media (prefers-color-scheme: dark){ body{ background:#0d1117; color:#e6edf3; } }
  .wrap { max-width: 820px; margin: 0 auto; padding: 24px 16px 120px; }
  header.meta { font-size:13px; opacity:.65; border-bottom:1px solid #8884; padding-bottom:12px; margin-bottom:24px; }
  header.meta code { font-size:12px; }
  .msg { margin: 18px 0; }
  .role { font-size:12px; text-transform:uppercase; letter-spacing:.06em; opacity:.6; margin-bottom:6px; }
  .bubble { border-radius:14px; padding:14px 18px; }
  .user .bubble { background:#2563eb12; border:1px solid #2563eb33; }
  .assistant .bubble { background:transparent; }
  .tool .bubble { background:#8881; border:1px dashed #8886; font-size:14px; opacity:.85; }
  .tool details summary { cursor:pointer; opacity:.7; text-transform:uppercase; font-size:12px; letter-spacing:.06em; }
  .tool details[open] summary { margin-bottom:8px; }
  .bubble > :first-child { margin-top:0; } .bubble > :last-child { margin-bottom:0; }
  pre { background:#8881; padding:12px 14px; border-radius:8px; overflow:auto; font-size:13.5px; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  :not(pre) > code { background:#8882; padding:.15em .4em; border-radius:5px; font-size:.9em; }
  table { border-collapse:collapse; } th,td { border:1px solid #8885; padding:6px 10px; }
  blockquote { border-left:3px solid #8886; margin-left:0; padding-left:14px; opacity:.85; }
  a { color:#2563eb; }
  .katex-display { overflow-x:auto; overflow-y:hidden; }
</style></head>
<body><div class="wrap">
<header class="meta">__META__</header>
<div id="thread"></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>__MARKDOWNIT_JS__</script>
<script>__KATEX_JS__</script>
<script>__TEXMATH_JS__</script>
<script>__HLJS_JS__</script>
<script>
  const data = JSON.parse(document.getElementById('data').textContent);
  const md = window.markdownit({
    html:false, linkify:true, breaks:false,
    highlight:(s,l)=>{ try{ if(l && hljs.getLanguage(l)) return hljs.highlight(s,{language:l}).value; }catch(e){} return ''; }
  });
  if (window.texmath) md.use(window.texmath, { engine: katex, delimiters: 'dollars',
       katexOptions:{ throwOnError:false, macros:{"\\RR":"\\mathbb{R}","\\EE":"\\mathbb{E}"} } });

  // Normalize \( \) \[ \] -> $ $$ so texmath (dollars) catches them too.
  function normMath(t){
    return t.replace(/\\\[([\s\S]+?)\\\]/g, (_,m)=>'$$'+m+'$$')
            .replace(/\\\(([\s\S]+?)\\\)/g, (_,m)=>'$'+m+'$');
  }
  const thread = document.getElementById('thread');
  for (const m of data.messages){
    const role = (m.role||'assistant');
    const wrap = document.createElement('div'); wrap.className = 'msg '+role;
    const bub = document.createElement('div'); bub.className='bubble';
    const rendered = md.render(normMath(m.content||''));
    if (m.kind === 'tool' || m.kind === 'thinking'){
      const det = document.createElement('details');
      const sum = document.createElement('summary'); sum.textContent = m.label||role;
      det.appendChild(sum);
      const inner = document.createElement('div'); inner.innerHTML = rendered;
      det.appendChild(inner);
      bub.appendChild(det);
      wrap.appendChild(bub);
    } else {
      const lab = document.createElement('div'); lab.className='role'; lab.textContent = m.label||role;
      bub.innerHTML = rendered;
      wrap.appendChild(lab); wrap.appendChild(bub);
    }
    thread.appendChild(wrap);
  }
</script>
</body></html>
"""


def format_session_html(session: SessionMeta, messages: list[Message]) -> str:
    """Render a session + messages as a self-contained HTML document.

    Markdown, syntax highlighting, and LaTeX math are rendered client-side by
    vendored JS/CSS inlined into the document, so the output is a single
    ``.html`` file that works offline from ``file://``. ``messages`` should
    already be filtered (tools/thinking) by the caller.
    """
    title = f"{session.provider.value} · {session.id[:8]}"

    meta_parts = [f"<b>{html.escape(session.provider.value)}</b>"]
    if session.model:
        meta_parts.append(f"model <code>{html.escape(str(session.model))}</code>")
    meta_parts.append(f"<code>{html.escape(session.project_path)}</code>")
    meta_parts.append(html.escape(session.timestamp.strftime("%Y-%m-%d %H:%M")))
    meta_parts.append(f"{len(messages)} msgs")
    meta = " &nbsp;·&nbsp; ".join(meta_parts)

    payload = {
        "session_id": session.id,
        "provider": session.provider.value,
        "project_path": session.project_path,
        "model": session.model,
        "timestamp": session.timestamp.isoformat(),
        "messages": _html_messages(messages),
    }
    # Escape "</" so the embedded JSON can't terminate the <script> early.
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    # Single-pass substitution: a vendored asset (e.g. highlight.min.js) can
    # itself contain a literal "__DATA__", so sequential str.replace() calls
    # would re-scan injected content and corrupt it. re.sub does one left-to-
    # right pass and never rescans replacement text.
    mapping = {
        "__TITLE__": html.escape(title),
        "__META__": meta,
        "__KATEX_CSS__": _load_asset("katex.min.css"),
        "__HLJS_CSS__": _load_asset("github.min.css"),
        "__MARKDOWNIT_JS__": _load_asset("markdown-it.min.js"),
        "__KATEX_JS__": _load_asset("katex.min.js"),
        "__TEXMATH_JS__": _load_asset("texmath.min.js"),
        "__HLJS_JS__": _load_asset("highlight.min.js"),
        "__DATA__": data,
    }
    pattern = re.compile(
        "__(?:TITLE|META|KATEX_CSS|HLJS_CSS|MARKDOWNIT_JS|KATEX_JS|TEXMATH_JS|HLJS_JS|DATA)__"
    )
    return pattern.sub(lambda mo: mapping[mo.group(0)], _HTML_TEMPLATE)
