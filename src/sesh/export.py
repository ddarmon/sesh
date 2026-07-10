"""Shared session export formatting."""

from __future__ import annotations

import html
import json
import re
from importlib.resources import files

from sesh import transcript
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


def _message_display_dict(m: Message) -> dict:
    """Map one ``Message`` to the display dict the viewer JS consumes.

    Each dict has ``role`` (CSS class), ``label`` (heading text), ``kind``
    (``"text"`` | ``"tool"`` | ``"thinking"`` — drives collapsible wrapping),
    and ``content`` (raw Markdown rendered client-side). The stable ``key`` is
    attached separately by the caller from :mod:`sesh.transcript`. The
    branching mirrors ``format_session_markdown``.
    """
    ts = f" ({m.timestamp.strftime('%H:%M')})" if m.timestamp else ""

    if m.content_type == "thinking":
        return {"role": "tool", "label": f"Thinking{ts}", "kind": "thinking", "content": m.thinking or ""}

    if m.content_type == "tool_use":
        tool = m.tool_name or "tool"
        return {
            "role": "tool",
            "label": f"{tool} (call){ts}",
            "kind": "tool",
            "content": f"```json\n{m.tool_input or ''}\n```",
        }

    if m.content_type == "tool_result":
        tool = m.tool_name or "tool"
        return {
            "role": "tool",
            "label": f"{tool} (result){ts}",
            "kind": "tool",
            "content": f"```\n{m.tool_output or ''}\n```",
        }

    if m.role == "user":
        return {"role": "user", "label": f"User{ts}", "kind": "text", "content": m.content}

    if m.role == "assistant":
        return {"role": "assistant", "label": f"Assistant{ts}", "kind": "text", "content": m.content}

    if m.role == "tool":
        tool = m.tool_name or "tool"
        return {"role": "tool", "label": f"{tool}{ts}", "kind": "tool", "content": m.content}

    return {"role": m.role, "label": f"{m.role}{ts}", "kind": "text", "content": m.content}


def _agent_display_dict(item: transcript.TranscriptItem) -> dict:
    """Build a ``kind: "agent"`` display dict from a composed agent item.

    Carries the collapsed summary ``label``, the container anchor ``key``, and
    the already-keyed interior messages, so the viewer JS can recurse into it.
    """
    meta = item.meta
    assert meta is not None
    desc = meta.description or meta.agent_id
    label = f"⑂ {meta.agent_type or 'agent'} — {desc} · {meta.message_count} msgs"
    messages: list[dict] = []
    for interior in item.interior:
        entry = _message_display_dict(interior.message)
        entry["key"] = interior.key
        messages.append(entry)
    return {
        "role": "agent",
        "label": label,
        "kind": "agent",
        "key": item.key,
        "description": meta.description,
        "agent_type": meta.agent_type,
        "message_count": meta.message_count,
        "messages": messages,
    }


def _compose_thread(
    messages: list[Message],
    subagents: list[tuple[SubagentMeta, list[Message]]] | None,
) -> list[dict]:
    """Interleave sub-agent ``agent`` entries into the main message dicts.

    Composition, chronological sub-agent anchoring, and stable keys all come
    from :func:`sesh.transcript.compose_transcript`; this function only maps the
    resulting items to the browser display dicts.
    """
    items = transcript.compose_transcript(messages, subagents)
    out: list[dict] = []
    for item in items:
        if item.kind == "agent":
            out.append(_agent_display_dict(item))
        else:
            entry = _message_display_dict(item.message)
            entry["key"] = item.key
            out.append(entry)
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
  .wrap { max-width: 820px; margin: 0 auto; padding: 16px 16px 160px; }
  header.meta { font-size:13px; opacity:.65; border-bottom:1px solid #8884; padding-bottom:12px; margin-bottom:24px; }
  header.meta code { font-size:12px; }
  /* Sticky reader toolbar */
  header.toolbar { position:sticky; top:0; z-index:30; display:flex; flex-wrap:wrap;
    gap:10px 16px; align-items:center; padding:8px 16px; font-size:13px;
    background:#ffffffe6; border-bottom:1px solid #8883; }
  @media (prefers-color-scheme: dark){ header.toolbar{ background:#0d1117e6; } }
  .tb-group { display:flex; align-items:center; gap:6px; }
  .tb-find { flex:1 1 260px; }
  #find-input { flex:1 1 auto; min-width:120px; max-width:340px; padding:4px 8px;
    border:1px solid #8886; border-radius:6px; background:transparent; color:inherit; font:inherit; }
  .tb-btn { padding:3px 9px; border:1px solid #8886; border-radius:6px; background:transparent;
    color:inherit; cursor:pointer; font:inherit; line-height:1.3; }
  .tb-btn:hover { background:#8882; }
  .tb-btn.on { background:#2563eb22; border-color:#2563eb66; }
  .tb-count { opacity:.7; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .tb-status { font-weight:600; color:#16a34a; white-space:nowrap; }
  .tb-status.waiting { color:#ca8a04; }
  .tb-status.paused { color:#ca8a04; }
  .tb-status.error { color:#dc2626; }
  .tb-meta { opacity:.6; white-space:nowrap; }
  .tb-badge { background:#2563eb; color:#fff; border:none; border-radius:10px;
    padding:2px 9px; cursor:pointer; font:inherit; font-size:12px; white-space:nowrap; }
  .msg { margin: 18px 0; position:relative; scroll-margin-top:56px; }
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
  /* Unobtrusive per-message copy / link actions (shown on hover / focus). */
  .msg-actions { position:absolute; top:0; right:0; display:flex; gap:4px;
    opacity:0; transition:opacity .1s; }
  .msg:hover > .msg-actions, .msg:focus-within > .msg-actions { opacity:1; }
  .msg-actions .act { font-size:11px; padding:1px 7px; border:1px solid #8886;
    border-radius:5px; background:#ffffffcc; color:inherit; cursor:pointer; font:inherit; }
  @media (prefers-color-scheme: dark){ .msg-actions .act{ background:#161b22cc; } }
  /* Find matches and fragment anchor highlight. */
  .msg.match-hit > .bubble, .msg.match-hit > details { box-shadow:0 0 0 2px #f59e0b55; border-radius:10px; }
  .msg.match-active > .bubble, .msg.match-active > details { box-shadow:0 0 0 3px #f59e0b; border-radius:10px; }
  .msg.anchor-active > .bubble, .msg.anchor-active > details { box-shadow:0 0 0 3px #2563eb; border-radius:10px; }
  pre { background:#8881; padding:12px 14px; border-radius:8px; overflow:auto; font-size:13.5px; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  :not(pre) > code { background:#8882; padding:.15em .4em; border-radius:5px; font-size:.9em; }
  table { border-collapse:collapse; } th,td { border:1px solid #8885; padding:6px 10px; }
  blockquote { border-left:3px solid #8886; margin-left:0; padding-left:14px; opacity:.85; }
  a { color:#2563eb; }
  .katex-display { overflow-x:auto; overflow-y:hidden; }
</style></head>
<body>
__TOOLBAR__
<div class="wrap">
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

  // --- Clipboard (secure-context navigator.clipboard, execCommand fallback) ---
  // file:// and 127.0.0.1 are secure contexts, but clipboard permissions vary,
  // so we always keep the textarea + execCommand fallback.
  async function copyText(text){
    try {
      if (navigator.clipboard && window.isSecureContext){
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) {}
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed'; ta.style.top = '0'; ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (e) { return false; }
  }
  function flash(btn, label){
    const prev = btn.textContent;
    btn.textContent = label;
    window.setTimeout(()=>{ btn.textContent = prev; }, 1100);
  }
  // Full plain-text of a sub-agent container: its interior messages joined.
  function agentText(m){
    return (m.messages||[]).map((x)=> (x.label ? x.label + '\n' : '') + (x.content||'')).join('\n\n');
  }
  function linkFor(key){
    const base = (location.origin && location.origin !== 'null')
      ? location.origin + location.pathname + location.search
      : location.href.split('#')[0];
    return base + '#' + key;
  }
  // Small copy-message / copy-link controls attached to every card.
  function buildActions(key, getText){
    const box = document.createElement('div'); box.className = 'msg-actions';
    const copy = document.createElement('button');
    copy.type = 'button'; copy.className = 'act'; copy.textContent = 'Copy';
    copy.title = 'Copy full message';
    copy.addEventListener('click', async (e)=>{
      e.stopPropagation(); e.preventDefault();
      const ok = await copyText(getText());
      flash(copy, ok ? 'Copied' : 'Failed');
    });
    box.appendChild(copy);
    if (key){
      const link = document.createElement('button');
      link.type = 'button'; link.className = 'act'; link.textContent = 'Link';
      link.title = 'Copy link to this message';
      link.addEventListener('click', async (e)=>{
        e.stopPropagation(); e.preventDefault();
        try { history.replaceState(null, '', '#' + key); } catch (err) {}
        const ok = await copyText(linkFor(key));
        flash(link, ok ? 'Copied' : 'Failed');
      });
      box.appendChild(link);
    }
    return box;
  }

  // Build one message/agent node. The stable key becomes the DOM id (and the
  // <details> liveKey), so anchors, open-state, and reconciliation are all
  // keyed by identity rather than list position.
  function buildNode(m, container){
    const key = m.key || '';
    if (m.kind === 'agent'){
      const wrap = document.createElement('div'); wrap.className = 'msg agent';
      if (key){ wrap.id = key; wrap.dataset.key = key; }
      wrap.appendChild(buildActions(key, ()=>agentText(m)));
      const det = document.createElement('details'); det.dataset.liveKey = key;
      const sum = document.createElement('summary'); sum.textContent = m.label||'sub-agent';
      det.appendChild(sum);
      const inner = document.createElement('div'); inner.className = 'agent-thread';
      (m.messages||[]).forEach((x)=> buildNode(x, inner));
      det.appendChild(inner);
      wrap.appendChild(det);
      container.appendChild(wrap);
      return;
    }
    const role = (m.role||'assistant');
    const wrap = document.createElement('div'); wrap.className = 'msg '+role;
    if (key){ wrap.id = key; wrap.dataset.key = key; }
    wrap.appendChild(buildActions(key, ()=> m.content||''));
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
  }
  function renderThread(list, container){ (list||[]).forEach((m)=> buildNode(m, container)); }

  const thread = document.getElementById('thread');
  renderThread(data.messages, thread);
  let topKeys = (data.messages||[]).map((m)=> m.key || '');

  // ---- Message count + new-message indicator ----
  const msgCountEl = document.getElementById('msg-count');
  const newMsgsBtn = document.getElementById('new-msgs');
  let renderedCount = topKeys.length;
  let pendingNew = 0;
  function setMsgCount(n){
    if (msgCountEl) msgCountEl.textContent = n + (n === 1 ? ' message' : ' messages');
  }
  function updateNewBadge(){
    if (!newMsgsBtn) return;
    if (pendingNew > 0 && !follow){
      newMsgsBtn.hidden = false;
      newMsgsBtn.textContent = pendingNew + ' new ↓';
    } else {
      newMsgsBtn.hidden = true;
    }
  }
  setMsgCount(renderedCount);

  // ---- Transcript find (browser equivalent of the TUI find) ----
  const findInput = document.getElementById('find-input');
  const findCount = document.getElementById('find-count');
  const findNext = document.getElementById('find-next');
  const findPrev = document.getElementById('find-prev');
  let matches = [];
  let matchIndex = -1;
  let findQuery = '';
  let activeKey = '';

  // Leaf message cards: a .msg with no nested .msg. This covers main-thread and
  // sub-agent-interior messages without double-counting agent containers.
  function leafCards(){
    return Array.from(thread.querySelectorAll('.msg')).filter((el)=> !el.querySelector('.msg'));
  }
  function updateFindCount(){
    if (!findCount) return;
    if (!findQuery.trim()){ findCount.textContent = ''; return; }
    findCount.textContent = matches.length ? (matchIndex + 1) + ' / ' + matches.length : '0 / 0';
  }
  function applyMatchClasses(){
    thread.querySelectorAll('.match-hit, .match-active').forEach((el)=>
      el.classList.remove('match-hit', 'match-active'));
    matches.forEach((el)=> el.classList.add('match-hit'));
    if (matchIndex >= 0 && matches[matchIndex]) matches[matchIndex].classList.add('match-active');
  }
  // Reveal a card: open its own <details> and every ancestor <details> so a hit
  // hidden inside a collapsed tool/thinking/sub-agent block becomes visible.
  function revealElement(el){
    let node = el;
    while (node && node !== document.body){
      if (node.tagName === 'DETAILS') node.open = true;
      node = node.parentElement;
    }
    el.querySelectorAll('details').forEach((d)=>{ d.open = true; });
  }
  function recomputeMatches(){
    const q = (findQuery || '').trim().toLowerCase();
    if (!q){
      matches = []; matchIndex = -1; activeKey = '';
      applyMatchClasses(); updateFindCount();
      return;
    }
    matches = leafCards().filter((el)=> (el.textContent || '').toLowerCase().includes(q));
    let idx = activeKey ? matches.findIndex((el)=> el.dataset.key === activeKey) : -1;
    if (idx < 0) idx = matches.length ? 0 : -1;
    matchIndex = idx;
    activeKey = matchIndex >= 0 ? (matches[matchIndex].dataset.key || '') : '';
    applyMatchClasses();
    updateFindCount();
  }
  function gotoMatch(i){
    if (!matches.length){ updateFindCount(); return; }
    matchIndex = ((i % matches.length) + matches.length) % matches.length;
    activeKey = matches[matchIndex].dataset.key || '';
    applyMatchClasses();
    revealElement(matches[matchIndex]);
    matches[matchIndex].scrollIntoView({block:'center'});
    updateFindCount();
  }
  if (findInput){
    findInput.addEventListener('input', ()=>{ findQuery = findInput.value; recomputeMatches(); });
    findInput.addEventListener('keydown', (e)=>{
      if (e.key === 'Enter'){
        e.preventDefault();
        gotoMatch(e.shiftKey ? matchIndex - 1 : matchIndex + 1);
      } else if (e.key === 'Escape'){
        findInput.blur();
      }
    });
  }
  if (findNext) findNext.addEventListener('click', ()=> gotoMatch(matchIndex + 1));
  if (findPrev) findPrev.addEventListener('click', ()=> gotoMatch(matchIndex - 1));
  document.addEventListener('keydown', (e)=>{
    if (e.key === '/' && findInput && document.activeElement !== findInput){
      const tag = (document.activeElement && document.activeElement.tagName) || '';
      if (tag !== 'INPUT' && tag !== 'TEXTAREA'){ e.preventDefault(); findInput.focus(); }
    }
  });

  // ---- Fragment anchor highlight ----
  function applyHash(doScroll){
    const raw = location.hash.slice(1);
    if (!raw) return;
    let id;
    try { id = decodeURIComponent(raw); } catch (e) { id = raw; }
    const el = document.getElementById(id);
    if (!el) return;
    document.querySelectorAll('.anchor-active').forEach((n)=> n.classList.remove('anchor-active'));
    revealElement(el);
    el.classList.add('anchor-active');
    if (doScroll) el.scrollIntoView({block:'center'});
  }
  window.addEventListener('hashchange', ()=> applyHash(true));

  // ---- Live view: polling transport with pause / follow / reconcile ----
  const isLive = !!data.live;
  let revision = isLive ? (data.live.revision || 0) : 0;
  const pollMs = isLive ? (data.live.poll_ms || 1500) : 1500;
  let paused = false;
  let follow = true;
  let inFlight = false;
  let pollTimer = null;
  const statusEl = document.getElementById('live-status');
  const updatedEl = document.getElementById('updated-at');
  const followBtn = document.getElementById('toggle-follow');
  const pauseBtn = document.getElementById('toggle-pause');
  const refreshBtn = document.getElementById('refresh-now');

  function nearBottom(){
    return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120;
  }
  function setStatus(text, cls){
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = 'tb-status' + (cls ? ' ' + cls : '');
  }
  function setUpdated(iso){
    if (!updatedEl || !iso) return;
    try { updatedEl.textContent = 'updated ' + new Date(iso).toLocaleTimeString(); }
    catch (e) { updatedEl.textContent = ''; }
  }
  if (isLive && data.live.updated_at) setUpdated(data.live.updated_at);

  // Reconcile a fresh payload against the DOM. Append-only when the old top-level
  // keys are a strict prefix of the new ones (content edits change keys, so a
  // prefix match means pure appends); otherwise a full rerender preserving open
  // <details> state by stable key.
  function applyPayload(payload){
    const list = payload.messages || [];
    const newKeys = list.map((m)=> m.key || '');
    const oldKeys = topKeys;
    const wasNear = nearBottom();
    const oldY = window.scrollY;
    const added = Math.max(0, newKeys.length - oldKeys.length);
    const followed = follow && wasNear;
    const isPrefix = oldKeys.length < newKeys.length
      && oldKeys.every((k, i)=> k === newKeys[i]);
    if (isPrefix){
      const frag = document.createDocumentFragment();
      for (let i = oldKeys.length; i < list.length; i++) buildNode(list[i], frag);
      thread.appendChild(frag);
    } else {
      const open = new Set(Array.from(thread.querySelectorAll('details[open]'))
        .map((node)=> node.dataset.liveKey));
      thread.replaceChildren();
      renderThread(list, thread);
      thread.querySelectorAll('details').forEach((node)=>{
        if (open.has(node.dataset.liveKey)) node.open = true;
      });
    }
    topKeys = newKeys;
    renderedCount = newKeys.length;
    setMsgCount(renderedCount);
    if (followed){
      pendingNew = 0;
      window.scrollTo(0, document.documentElement.scrollHeight);
    } else {
      window.scrollTo(0, oldY);
      if (added > 0) pendingNew += added;
    }
    updateNewBadge();
    recomputeMatches();
    applyHash(false);
  }

  async function poll(manual){
    if (inFlight) return;
    if (paused && !manual) return;
    inFlight = true;
    try {
      const separator = data.live.api.includes('?') ? '&' : '?';
      const response = await fetch(
        data.live.api + separator + 'revision=' + encodeURIComponent(revision),
        {cache:'no-store'}
      );
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const update = await response.json();
      if (update.updated_at) setUpdated(update.updated_at);
      if (update.revision !== revision && update.payload){
        applyPayload(update.payload);
        revision = update.revision;
      }
      if (paused) setStatus('❚❚ PAUSED', 'paused');
      else if (update.error) setStatus('● RETRYING', 'waiting');
      else setStatus('● LIVE', '');
    } catch (error) {
      setStatus('● DISCONNECTED', 'error');
    } finally {
      inFlight = false;
    }
  }
  // A single self-rescheduling timer (never a repeating interval) so polls can
  // never overlap or drift, and pausing is a browser-side no-op that keeps the timer.
  function schedule(){
    if (!isLive) return;
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(async ()=>{
      if (!paused) await poll(false);
      schedule();
    }, pollMs);
  }

  if (pauseBtn) pauseBtn.addEventListener('click', ()=>{
    paused = !paused;
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
    pauseBtn.setAttribute('aria-pressed', paused ? 'true' : 'false');
    if (paused){ setStatus('❚❚ PAUSED', 'paused'); }
    else { setStatus('● LIVE', ''); poll(false); }
  });
  if (followBtn) followBtn.addEventListener('click', ()=>{
    follow = !follow;
    followBtn.setAttribute('aria-pressed', follow ? 'true' : 'false');
    followBtn.classList.toggle('on', follow);
    if (follow){
      pendingNew = 0; updateNewBadge();
      window.scrollTo(0, document.documentElement.scrollHeight);
    }
  });
  if (refreshBtn) refreshBtn.addEventListener('click', ()=> poll(true));
  if (newMsgsBtn) newMsgsBtn.addEventListener('click', ()=>{
    pendingNew = 0; updateNewBadge();
    window.scrollTo(0, document.documentElement.scrollHeight);
  });

  if (isLive){
    document.body.classList.add('is-live');
    if (followBtn) followBtn.classList.add('on');
    setStatus('● LIVE', '');
    schedule();
  }

  // Honor a #fragment present on initial load (after the thread is rendered).
  applyHash(true);
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


def _toolbar_html(is_live: bool) -> str:
    """Build the sticky reader toolbar.

    The transcript-find group and message count work from ``file://`` with no
    server, so they render in both static and live documents. The live-only
    group (status, follow/pause/refresh, last-update, new-message indicator) is
    emitted *only* for live views, so a static export cannot expose controls
    that depend on the private polling server.
    """
    find_group = (
        '<div class="tb-group tb-find">'
        '<input id="find-input" type="search" placeholder="Find in transcript" '
        'autocomplete="off" autocorrect="off" spellcheck="false" '
        'aria-label="Find in transcript">'
        '<span id="find-count" class="tb-count" aria-live="polite"></span>'
        '<button id="find-prev" type="button" class="tb-btn" '
        'title="Previous match (Shift+Enter)" aria-label="Previous match">↑</button>'
        '<button id="find-next" type="button" class="tb-btn" '
        'title="Next match (Enter)" aria-label="Next match">↓</button>'
        "</div>"
    )
    if is_live:
        info_group = (
            '<div class="tb-group tb-live">'
            '<span id="msg-count" class="tb-count"></span>'
            '<button id="new-msgs" type="button" class="tb-badge" hidden></button>'
            '<span id="live-status" class="tb-status">● LIVE</span>'
            '<span id="updated-at" class="tb-meta"></span>'
            '<button id="toggle-follow" type="button" class="tb-btn on" '
            'aria-pressed="true" title="Follow new output">Follow</button>'
            '<button id="toggle-pause" type="button" class="tb-btn" '
            'aria-pressed="false" title="Pause live polling">Pause</button>'
            '<button id="refresh-now" type="button" class="tb-btn" '
            'title="Refresh now">Refresh</button>'
            "</div>"
        )
    else:
        info_group = (
            '<div class="tb-group tb-info">'
            '<span id="msg-count" class="tb-count"></span>'
            "</div>"
        )
    return f'<header class="toolbar" role="toolbar">{find_group}{info_group}</header>'


def format_session_html(
    session: SessionMeta,
    messages: list[Message],
    subagents: list[tuple[SubagentMeta, list[Message]]] | None = None,
    *,
    live_api: str | None = None,
    live_revision: int = 0,
    live_poll_ms: int = 1500,
    live_updated_at: str | None = None,
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

    payload = session_html_payload(session, messages, subagents)
    if live_api is not None:
        live_cfg: dict = {
            "api": live_api,
            "revision": live_revision,
            "poll_ms": max(250, live_poll_ms),
        }
        if live_updated_at is not None:
            live_cfg["updated_at"] = live_updated_at
        payload["live"] = live_cfg
    # Escape "</" so the embedded JSON can't terminate the <script> early.
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    # Single-pass substitution: a vendored asset (e.g. highlight.min.js) can
    # itself contain a literal "__DATA__", so sequential str.replace() calls
    # would re-scan injected content and corrupt it. re.sub does one left-to-
    # right pass and never rescans replacement text.
    mapping = {
        "__TITLE__": html.escape(title),
        "__TOOLBAR__": _toolbar_html(live_api is not None),
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
        "__(?:TITLE|TOOLBAR|META|KATEX_CSS|HLJS_CSS|MARKDOWNIT_JS|KATEX_JS|TEXMATH_JS|HLJS_JS|DATA)__"
    )
    return pattern.sub(lambda mo: mapping[mo.group(0)], _HTML_TEMPLATE)
