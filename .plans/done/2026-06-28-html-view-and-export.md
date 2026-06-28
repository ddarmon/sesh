---
Status: done
Type: feature
Owner: David
Branch: main
Created: 2026-06-28
Updated: 2026-06-28
---

# HTML export + `sesh view` (Markdown + LaTeX rendering)

## Outcome (implemented)

Shipped in v0.18.0. A rich, **offline** HTML rendering of a session —
Markdown, code highlighting, and **LaTeX** — so sessions read like they do
in ChatGPT/Claude. LaTeX is the differentiator vs. community JSONL→HTML
tools.

Delivered surfaces:

1. **`sesh export --format html`** — emits/writes a self-contained HTML file
   (mirrors `--format md|json`). `cli.py` `cmd_export` gained an `html`
   branch; `--format` choices now `["md", "json", "html"]`.
2. **`sesh view <id|last>`** — renders HTML to `<tmpdir>/sesh-<id8>.html`,
   prints the path, and opens it in the browser unless `--no-open`. Honors
   `--include-tools` / `--include-thinking` / `--full`.

Core logic: `export.format_session_html(session, messages)` builds the full
document; `_html_messages` maps `Message` objects to display dicts (mirrors
`format_session_markdown`'s branching). Assets are inlined via
`importlib.resources` and substituted into the template in a **single
`re.sub` pass** (not chained `str.replace`) because a vendored file
(highlight.min.js) itself contains a literal `__DATA__` token. The embedded
message JSON escapes `</` so it cannot terminate the data `<script>` early.

## Vendored assets (offline, not CDN)

Under `src/sesh/viewer_assets/` (ships in the wheel — top-level `assets/`
is not packaged):

- `katex.min.css` / `katex.min.js` (0.16.11), `markdown-it.min.js`
  (14.1.0), `texmath.min.js` (1.0.0), `highlight.min.js` + `github.min.css`
  (11.9.0).
- KaTeX's 20 referenced **woff2** fonts are base64-inlined into
  `katex.min.css` (so it is ~360 KB vs. ~23 KB upstream); browsers prefer
  woff2 so the dangling woff/ttf fallback URLs are never fetched.
- All libs are MIT/BSD-3 licensed. Upstream license texts bundled in
  `viewer_assets/LICENSES/`; versions/sources/licenses tracked in
  `viewer_assets/README.md`.

## Render behaviors ported from the `~/.local/bin/sesh-view` prototype

- `markdown-it` with `linkify:true, html:false`; `highlight.js` in the
  `highlight` callback.
- `md.use(texmath, { engine: katex, delimiters: 'dollars' })` with
  `throwOnError:false`.
- Normalize bracket math before render: `\[…\] → $$…$$`, `\(…\) → $…$`.
- Per-message bubbles: user (tinted), assistant (plain), tool/thinking
  (collapsible `<details>`, dimmed).
- Light/dark via `prefers-color-scheme`.

## Tests

- `tests/unit/test_export.py`: `format_session_html` is one self-contained
  doc, assets inlined (no CDN), fonts base64-inlined, no leftover
  placeholders, messages embedded, inline/display LaTeX survives into the
  embedded JSON (backslashes JSON-escaped), `</script>` escaped, empty
  session valid.
- `tests/unit/test_cli_commands.py`: `export --format html -o FILE` writes
  the file + prints the JSON confirmation; `view --no-open` writes the temp
  file and prints the path without opening; `view` opens a `file://` URI.

Full suite green (516 passed). Wheel build confirmed assets + LICENSES are
packaged under `sesh/viewer_assets/`. End-to-end `sesh view last --no-open`
verified on a real session (offline, fonts inlined, embedded JSON parses).

## Docs

`README.md` (HTML rendering subsection + usage examples) and `CLAUDE.md`
(Session export + new "HTML rendering" section, CLI table, `last` note)
updated.
