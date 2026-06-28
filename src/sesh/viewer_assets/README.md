# Vendored viewer assets

These JS/CSS files are bundled so that `sesh export --format html` and
`sesh view` produce **self-contained, offline** HTML transcripts (Markdown +
syntax highlighting + LaTeX math). They are inlined into the generated `.html`
by `sesh.export.format_session_html`, so the output file has no network
dependencies and works from `file://`.

All assets are permissively licensed (MIT / BSD-3-Clause), compatible with
sesh's MIT license. Full upstream license texts are in `LICENSES/`.

| File | Version | Source | License |
| --- | --- | --- | --- |
| `katex.min.css` | 0.16.11 | https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css | MIT (`LICENSES/katex.LICENSE`) |
| `katex.min.js` | 0.16.11 | https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js | MIT (`LICENSES/katex.LICENSE`) |
| KaTeX woff2 fonts | 0.16.11 | https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/fonts/ | MIT (`LICENSES/katex-fonts.LICENSE`) |
| `markdown-it.min.js` | 14.1.0 | https://cdn.jsdelivr.net/npm/markdown-it@14.1.0/dist/markdown-it.min.js | MIT (`LICENSES/markdown-it.LICENSE`) |
| `texmath.min.js` | 1.0.0 | https://cdn.jsdelivr.net/npm/markdown-it-texmath@1.0.0/texmath.min.js | MIT (`LICENSES/markdown-it-texmath.LICENSE`) |
| `highlight.min.js` | 11.9.0 | https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/highlight.min.js | BSD-3-Clause (`LICENSES/highlight.js.LICENSE`) |
| `github.min.css` | 11.9.0 | https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github.min.css | BSD-3-Clause (`LICENSES/highlight.js.LICENSE`) |

## Note on `katex.min.css`

The upstream CSS references its woff2/woff/ttf fonts by relative URL
(`fonts/KaTeX_*.woff2`). To keep the generated HTML fully self-contained, the
20 referenced **woff2** font files have been inlined into this CSS as
`data:font/woff2;base64,...` URIs. Browsers prefer the woff2 format, so the
remaining (now-dangling) `.woff`/`.ttf` fallback URLs in each `@font-face` are
never fetched. This is why `katex.min.css` here (~360 KB) is much larger than
the original (~23 KB).

## Upgrading

1. Re-download each file from the source URL above (bump the version).
2. For `katex.min.css`, re-inline the fonts: download every
   `fonts/KaTeX_*.woff2` it references and replace each
   `url(fonts/NAME.woff2)` with `url(data:font/woff2;base64,<b64>)`.
3. Refresh the matching `LICENSES/*` text if the upstream license changed.
4. Update the versions in this table.
