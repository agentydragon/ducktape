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
- `console_panel.tsx` — the shell chrome (`ShellChrome`): a fixed top-right **floating toolbar**
  (one squished bar of toggle buttons, each `filled` when its panel is open) over a column that
  stacks its open panels **by Y, never by z-index**. Toolbar buttons are neutral gray with only
  semantic color cues (red pending count, orange offline, green live-location/live-capture dot):
  approvals (a checklist), settings (a gear), plus a crossed-wifi live-offline warning when the
  event socket is down, a location pin while the geolocation standing grant is held, and a camera
  while the screenshot standing grant is held. Below the toolbar, the open panels stack: the
  approvals panel (queue + a Past-tool-calls link) follows its content until it reaches the
  available height, then scrolls its list; settings (MCP accounts) behaves the same way, while
  location/screenshot (stop/withdraw) and live-offline panels take their natural height beneath
  them. Opening several shows them stacked, not overlapping.
- `settings_page.tsx` — `SettingsPanel`, the operator settings drawer (a `ShellChrome` panel
  toggled from the toolbar's gear): MCP operator account connect/reconnect/disconnect. It's a
  chrome drawer, not a route, so it stacks alongside the other panels.
- `open_external.ts` — `openExternal(url)`: opens a link in a new tab with the opener
  severed, shared by the embed shell (the `openLink` bridge action) and the settings panel
  (the MCP OAuth popup).
- `tool_arguments_field.tsx` / `icons.tsx` — shared tool-argument renderer (per-server
  preview or raw JSON) and inline-SVG icons (never the `@tabler` barrel — see
  `debug/esbuild_tabler_memory.md`), used by both the drawer and the history view.
- `tool_result_field.tsx` — the result-side counterpart: a finished call's result as a
  per-server widget (`tool_results/`, a registry mirroring `tool_previews/`) over the
  unwrapped `CallToolResult` payload, else the raw-JSON `Result` field (detailed only).
- `tool_call_events.ts` — `useToolCallEvents(onEvent)`: the shared live signal (the
  `/api/approvals/ws` WebSocket) that refetches on every submit/approve/deny/finish. The
  server broadcasts each event to every connected tab, so both the approval drawer and the
  history view stay live without a reload — no client-side cross-tab plumbing needed. It
  auto-reconnects with backoff and returns a `LiveStatus` (`connecting`/`live`/`offline`) the
  shell uses to warn when the channel is down (and refetches on reconnect to catch up).
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
