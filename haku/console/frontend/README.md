# haku/console/frontend — dashboard SPA

React 18 single-page app for the Haku console, bundled with esbuild and served
same-origin by the console service. Production serves the fingerprinted bundle
from the baked `haku-console-static` nginx image, with route-specific cache
headers. Styled with the repo's house stack — **Mantine v7**
components + **Tailwind v4** utilities — modeled on
`finance/augur/frontend` (references root `//:node_modules/*`; no per-package
`package.json`).

- `main.tsx` (wraps the app in `MantineProvider`) → `app.tsx` (routes between the console
  views) → `haku_ui_embed.tsx` (the full-page framed haku-ui plus shell chrome: approval
  drawer, bridge, confirms), `tool_calls_page.tsx` (the full-page past-tool-calls history),
  or `settings_page.tsx` (the full-page operator settings — MCP account linkage).
- `routing.ts` — the console's tiny pathname router. Three views keyed on URL **path**
  (`/` embed, `/tool-calls` history, `/settings` settings) so console navigation never
  collides with the hash, which is reserved for mirroring the framed haku-ui route.
- `console_panel.tsx` — the shell drawer (`ShellDrawer`) and its persistent toggle
  (`ShellControls`): a hamburger icon badged with a pending-approval callout light, with a
  location-sharing pin below it (shown only while the standing grant is held; a live
  indicator dot marks an active read, and its popover carries the stop/withdraw kill
  switch). The drawer hosts the approval queue plus nav links to the past-tool-calls and
  settings pages.
- `settings_page.tsx` — the full-page operator settings view (`/settings`): MCP operator
  account connect/reconnect/disconnect, moved out of the drawer since it's rarely touched.
- `open_external.ts` — `openExternal(url)`: opens a link in a new tab with the opener
  severed, shared by the embed shell (the `openLink` bridge action) and the settings page
  (the MCP OAuth popup).
- `tool_arguments_field.tsx` / `icons.tsx` — shared tool-argument renderer (per-server
  preview or raw JSON) and inline-SVG icons (never the `@tabler` barrel — see
  `debug/esbuild_tabler_memory.md`), used by both the drawer and the history view.
- `tool_call_events.ts` — `useToolCallEvents(onEvent)`: the shared live signal (the
  `/api/approvals/ws` WebSocket) that refetches on every submit/approve/deny/finish. The
  server broadcasts each event to every connected tab, so both the approval drawer and the
  history view stay live without a reload — no client-side cross-tab plumbing needed.
- `client.ts` — typed `openapi-fetch` client; the types come from the backend's
  OpenAPI schema (the `:schema` target runs `//haku/console:export_schema_bin`), so
  the Pydantic models are the single source of truth for the wire contract. Includes the
  launch-routine helper, MCP approval queue helpers (`pending`, approve, deny), and
  MCP operator-account association helpers.
- `confirm_dialog.tsx` — trusted top-layer confirmations for bridge launches, geolocation
  grants, off-whitelist opens, and MCP tool-call approvals.
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
