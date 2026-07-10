"""Shared session export formatting."""

from __future__ import annotations

import hashlib
import html
import json
import re
from importlib.resources import files

from sesh.models import Message, SessionMeta, SubagentMeta


def _md_message_lines(m: Message, heading_offset: int = 0) -> list[str]:
    """Render one message as Markdown lines.

    ``heading_offset`` demotes every heading by that many levels so a message
    thread can be nested under a higher-level section (e.g. a sub-agent's
    interior renders one level deeper than the main thread).
    """
    h2 = "#" * (2 + heading_offset)
    h3 = "#" * (3 + heading_offset)
    ts = f" ({m.timestamp.strftime('%H:%M')})" if m.timestamp else ""
    lines: list[str] = []

    if m.content_type == "thinking":
        lines.append(f"{h3} Thinking{ts}")
        lines.append("")
        for line in (m.thinking or "").splitlines():
            lines.append(f"> {line}")
        lines.append("")
        return lines

    if m.content_type == "tool_use":
        tool = m.tool_name or "tool"
        lines.append(f"{h3} {tool} (call){ts}")
        lines.append("")
        lines.append("```json")
        lines.append(m.tool_input or "")
        lines.append("```")
        lines.append("")
        return lines

    if m.content_type == "tool_result":
        tool = m.tool_name or "tool"
        lines.append(f"{h3} {tool} (result){ts}")
        lines.append("")
        lines.append(m.tool_output or "")
        lines.append("")
        return lines

    if m.role == "user":
        lines.append(f"{h2} User{ts}")
        lines.append("")
        lines.append(m.content)
        lines.append("")
        return lines

    if m.role == "assistant":
        lines.append(f"{h2} Assistant{ts}")
        lines.append("")
        lines.append(m.content)
        lines.append("")
        return lines

    if m.role == "tool":
        tool = m.tool_name or "tool"
        lines.append(f"{h3} {tool}{ts}")
        lines.append("")
        lines.append(m.content)
        lines.append("")
        return lines

    lines.append(f"{h2} {m.role}{ts}")
    lines.append("")
    lines.append(m.content)
    lines.append("")
    return lines


def format_session_markdown(
    session: SessionMeta,
    messages: list[Message],
    subagents: list[tuple[SubagentMeta, list[Message]]] | None = None,
) -> str:
    """Render a session + messages as Markdown.

    When ``subagents`` is given (Claude sub-agent threads), each is appended
    after the main thread as a ``## Sub-agent:`` section with its interior
    messages demoted one heading level.
    """
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
        lines.extend(_md_message_lines(m))

    for meta, interior in subagents or []:
        desc = meta.description or meta.agent_id
        lines.append(f"## Sub-agent: {desc} ({meta.agent_id})")
        lines.append("")
        meta_bits = [f"**Type:** {meta.agent_type or 'agent'}"]
        meta_bits.append(f"**Messages:** {meta.message_count}")
        if meta.output_tokens is not None:
            meta_bits.append(f"**Output tokens:** {meta.output_tokens:,}")
        if meta.is_fork:
            meta_bits.append("**Fork:** yes")
        lines.append(" · ".join(meta_bits))
        lines.append("")
        for m in interior:
            lines.extend(_md_message_lines(m, heading_offset=1))

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

    # Stable keys let live rerenders restore expanded details even when rows
    # are inserted or visibility toggles change ahead of an existing item.
    occurrences: dict[str, int] = {}
    for entry in out:
        material = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]
        occurrence = occurrences.get(digest, 0)
        occurrences[digest] = occurrence + 1
        entry["key"] = f"{digest}-{occurrence}"
    return out


def _agent_entry(meta: SubagentMeta, interior: list[Message]) -> dict:
    """Build a ``kind: "agent"`` display dict for a sub-agent thread.

    Carries the collapsed summary ``label`` plus the nested interior mapped
    through :func:`_html_messages`, so the viewer JS can recurse into it.
    """
    desc = meta.description or meta.agent_id
    label = f"⑂ {meta.agent_type or 'agent'} — {desc} · {meta.message_count} msgs"
    return {
        "role": "agent",
        "label": label,
        "kind": "agent",
        "key": f"agent-{meta.agent_id}",
        "description": meta.description,
        "agent_type": meta.agent_type,
        "message_count": meta.message_count,
        "messages": _html_messages(interior),
    }


def _compose_thread(
    messages: list[Message],
    subagents: list[tuple[SubagentMeta, list[Message]]] | None,
) -> list[dict]:
    """Interleave sub-agent ``agent`` entries into the main message dicts.

    Each sub-agent is anchored chronologically: it is spliced in just before
    the first visible main-thread message with a later timestamp (so it works
    even when tool rows are filtered out). Sub-agents with no timestamp fall
    back to a trailing section appended after the whole thread.
    """
    base = _html_messages(messages)
    if not subagents:
        return base

    anchored: list[tuple[int, dict]] = []
    trailing: list[dict] = []
    for meta, interior in subagents:
        entry = _agent_entry(meta, interior)
        ts = meta.first_timestamp
        if ts is None:
            trailing.append(entry)
            continue
        idx = len(messages)
        for i, m in enumerate(messages):
            if m.timestamp is not None and m.timestamp > ts:
                idx = i
                break
        anchored.append((idx, entry))

    anchored.sort(key=lambda t: t[0])
    result: list[dict] = []
    ai = 0
    for i in range(len(base) + 1):
        while ai < len(anchored) and anchored[ai][0] == i:
            result.append(anchored[ai][1])
            ai += 1
        if i < len(base):
            result.append(base[i])
    result.extend(trailing)
    return result


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
  #live-status { color:#16a34a; font-weight:600; }
  #live-status.waiting { color:#ca8a04; }
  #live-status.error { color:#dc2626; }
  .msg { margin: 18px 0; }
  .role { font-size:12px; text-transform:uppercase; letter-spacing:.06em; opacity:.6; margin-bottom:6px; }
  .bubble { border-radius:14px; padding:14px 18px; }
  .user .bubble { background:#2563eb12; border:1px solid #2563eb33; }
  .assistant .bubble { background:transparent; }
  .tool .bubble { background:#8881; border:1px dashed #8886; font-size:14px; opacity:.85; }
  .tool details summary { cursor:pointer; opacity:.7; text-transform:uppercase; font-size:12px; letter-spacing:.06em; }
  .tool details[open] summary { margin-bottom:8px; }
  .agent > details > summary { cursor:pointer; opacity:.75; font-size:13px; letter-spacing:.02em; }
  .agent > details[open] > summary { margin-bottom:10px; }
  .agent-thread { border-left:2px solid #8886; padding-left:16px; margin-left:4px; }
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
  // Split out fenced (``` / ~~~) and inline (`…`) code first and leave those
  // segments untouched, so LaTeX-looking text inside code (incl. the json/text
  // fences wrapping tool I/O) is shown literally rather than rendered as math.
  function normMath(t){
    const parts = (t||'').split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)/g);
    return parts.map((seg,i)=> (i%2===1) ? seg :
      seg.replace(/\\\[([\s\S]+?)\\\]/g, (_,m)=>'$$'+m+'$$')
         .replace(/\\\(([\s\S]+?)\\\)/g, (_,m)=>'$'+m+'$')
    ).join('');
  }
  // Render a list of message dicts into a container. Sub-agent entries
  // (kind:'agent') become a collapsed <details> whose interior is the same
  // thread rendering, recursed one level deeper.
  function renderThread(list, container, prefix='m'){
    list.forEach((m, index)=>{
      const key = prefix+'-'+(m.key || index);
      if (m.kind === 'agent'){
        const wrap = document.createElement('div'); wrap.className = 'msg agent';
        const det = document.createElement('details'); det.dataset.liveKey = key;
        const sum = document.createElement('summary'); sum.textContent = m.label||'sub-agent';
        det.appendChild(sum);
        const inner = document.createElement('div'); inner.className = 'agent-thread';
        renderThread(m.messages||[], inner, key);
        det.appendChild(inner);
        wrap.appendChild(det);
        container.appendChild(wrap);
        return;
      }
      const role = (m.role||'assistant');
      const wrap = document.createElement('div'); wrap.className = 'msg '+role;
      const bub = document.createElement('div'); bub.className='bubble';
      const rendered = md.render(normMath(m.content||''));
      if (m.kind === 'tool' || m.kind === 'thinking'){
        const det = document.createElement('details'); det.dataset.liveKey = key;
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
      container.appendChild(wrap);
    });
  }
  const thread = document.getElementById('thread');
  renderThread(data.messages, thread);

  // A live view polls a private loopback endpoint. Rerenders preserve the
  // reader's position and expanded tool/sub-agent sections; it follows new
  // output only when the reader was already near the bottom.
  if (data.live){
    let revision = data.live.revision || 0;
    let polling = false;
    const status = document.getElementById('live-status');
    const setStatus = (text, cls='')=>{
      if (!status) return;
      status.textContent = text;
      status.className = cls;
    };
    async function poll(){
      if (polling) return;
      polling = true;
      try {
        const separator = data.live.api.includes('?') ? '&' : '?';
        const response = await fetch(
          data.live.api + separator + 'revision=' + encodeURIComponent(revision),
          {cache:'no-store'}
        );
        if (!response.ok) throw new Error('HTTP '+response.status);
        const update = await response.json();
        if (update.revision !== revision && update.payload){
          const open = new Set(Array.from(thread.querySelectorAll('details[open]'))
            .map((node)=>node.dataset.liveKey));
          const oldY = window.scrollY;
          const nearBottom = window.innerHeight + oldY >= document.documentElement.scrollHeight - 100;
          thread.replaceChildren();
          renderThread(update.payload.messages || [], thread);
          thread.querySelectorAll('details').forEach((node)=>{
            if (open.has(node.dataset.liveKey)) node.open = true;
          });
          revision = update.revision;
          if (nearBottom) window.scrollTo(0, document.documentElement.scrollHeight);
          else window.scrollTo(0, oldY);
        }
        if (update.error) setStatus('● LIVE · retrying', 'waiting');
        else setStatus('● LIVE', '');
      } catch (error) {
        setStatus('● DISCONNECTED', 'error');
      } finally {
        polling = false;
      }
    }
    window.setInterval(poll, data.live.poll_ms || 1500);
  }
</script>
</body></html>
"""


def session_html_payload(
    session: SessionMeta,
    messages: list[Message],
    subagents: list[tuple[SubagentMeta, list[Message]]] | None = None,
) -> dict:
    """Return the normalized JSON payload consumed by the browser viewer."""
    return {
        "session_id": session.id,
        "provider": session.provider.value,
        "project_path": session.project_path,
        "model": session.model,
        "timestamp": session.timestamp.isoformat(),
        "messages": _compose_thread(messages, subagents),
    }


def format_session_html(
    session: SessionMeta,
    messages: list[Message],
    subagents: list[tuple[SubagentMeta, list[Message]]] | None = None,
    *,
    live_api: str | None = None,
    live_revision: int = 0,
    live_poll_ms: int = 1500,
) -> str:
    """Render a session + messages as a self-contained HTML document.

    Markdown, syntax highlighting, and LaTeX math are rendered client-side by
    vendored JS/CSS inlined into the document, so the output is a single
    ``.html`` file that works offline from ``file://``. ``messages`` should
    already be filtered (tools/thinking) by the caller.

    When ``subagents`` is given (Claude sub-agent threads), each renders as a
    collapsed ``kind: "agent"`` block spliced into the thread at its spawn
    point (see :func:`_compose_thread`); its interior should already be
    filtered by the caller.
    """
    title = f"{session.provider.value} · {session.id[:8]}"

    meta_parts = [f"<b>{html.escape(session.provider.value)}</b>"]
    if session.model:
        meta_parts.append(f"model <code>{html.escape(str(session.model))}</code>")
    meta_parts.append(f"<code>{html.escape(session.project_path)}</code>")
    meta_parts.append(html.escape(session.timestamp.strftime("%Y-%m-%d %H:%M")))
    meta_parts.append(f"{len(messages)} msgs")
    meta = " &nbsp;·&nbsp; ".join(meta_parts)
    if live_api is not None:
        meta += ' &nbsp;·&nbsp; <span id="live-status">● LIVE</span>'

    payload = session_html_payload(session, messages, subagents)
    if live_api is not None:
        payload["live"] = {
            "api": live_api,
            "revision": live_revision,
            "poll_ms": max(250, live_poll_ms),
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
