# haku/console/frontend — dashboard SPA

React 18 single-page app for the Haku console, bundled with esbuild and served
same-origin by the console service. Production serves the fingerprinted bundle
from the baked `haku-console-static` nginx image, with route-specific cache
headers. Styled with the repo's house stack — **Mantine v7**
components + **Tailwind v4** utilities — modeled on
`finance/augur/frontend` (references root `//:node_modules/*`; no per-package
`package.json`).

- `main.tsx` (wraps the app in `MantineProvider`) → `app.tsx` (routes between the two
  console views) → either `haku_ui_embed.tsx` (the full-page framed haku-ui plus shell
  chrome: approval drawer, bridge, confirms) or `tool_calls_page.tsx` (the full-page
  past-tool-calls history).
- `routing.ts` — the console's tiny pathname router. Two views keyed on URL **path**
  (`/` embed, `/tool-calls` history) so console navigation never collides with the hash,
  which is reserved for mirroring the framed haku-ui route.
- `console_panel.tsx` — the shell drawer (`ShellDrawer`) and its persistent toggle
  (`ShellControls`): a hamburger icon badged with a pending-approval callout light. Hosts
  the approval queue, the past-tool-calls link, and the Access tab (location + MCP
  accounts).
- `tool_arguments_field.tsx` / `icons.tsx` — shared tool-argument renderer (per-server
  preview or raw JSON) and inline-SVG icons (never the `@tabler` barrel — see
  `debug/esbuild_tabler_memory.md`), used by both the drawer and the history view.
- `client.ts` — typed `openapi-fetch` client; the types come from the backend's
  OpenAPI schema (the `:schema` target runs `//haku/console:export_schema_bin`), so
  the Pydantic models are the single source of truth for the wire contract. Includes the
  launch-routine helper, MCP approval queue helpers (`pending`, approve, deny), and
  MCP operator-account association helpers.
- `confirm_dialog.tsx` — trusted top-layer confirmations for bridge launches, geolocation
  grants, off-whitelist opens, and MCP tool-call approvals.
- `console_panel.tsx` — the single settings-button drawer for shell-owned controls. Keep
  MCP account connect/reconnect/disconnect affordances here rather than adding more visible
  widgets above the framed haku-ui.
- `markdown.ts` — item `body` → sanitized HTML (`marked` + `dompurify`).
- `styles.src.css` — `@import`s Tailwind + `@mantine/core` CSS; compiled by
  `@tailwindcss/cli` to `generated/styles.css`, then fingerprinted into
  `dist/assets/styles-<hash>.css`. Deviation from a plain Tailwind setup: the `@source`
  content index is a generated file (`tailwind_content_index`) concatenating the sources
  Tailwind must scan, since Bazel sandboxes the inputs.
- `index.html` — the docroot shell template; `spa_bundle(fingerprint = True)` rewrites
  placeholders to hashed JS/CSS/logo URLs under `dist/assets/`.

```bash
bbr build //haku/console/frontend:bundle   # production bundle (dist/)
bbr test //haku/console/frontend/...        # tsc type-check + vitest
```
