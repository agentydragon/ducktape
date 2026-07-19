# haku/console/frontend — dashboard SPA

React 18 single-page app for the Haku console, bundled with esbuild and served
same-origin by the console service. Production serves the fingerprinted bundle
from the baked `haku-console-static` nginx image, with route-specific cache
headers. Styled with the repo's house stack — **Mantine v7**
components + **Tailwind v4** utilities — modeled on
`finance/augur/frontend` (references root `//:node_modules/*`; no per-package
`package.json`).

- `main.tsx` (wraps the app in `MantineProvider`) → `app.tsx` (routes between the console
  views) → `haku_ui_embed.tsx` (the full-page framed haku-ui plus shell chrome, bridge, and
  confirmations) or `tool_calls_page.tsx` (the full-page past-tool-calls history).
- `routing.ts` — the console's tiny pathname router. `/tool-calls` is the console's own
  history view; **every other path is the mirrored haku-ui route** (the embed), so
  path-form deep links restore both shell and frame. The last embed path is remembered
  across a `/tool-calls` detour.
- `shell_chrome.tsx` — the shell chrome (`ShellChrome`): a fixed top-right **floating toolbar**
  (one squished bar of toggle buttons, each `filled` when its panel is selected) over its active
  panel. The toggles behave as deselectable tabs, so at most one panel is open. Toolbar buttons are neutral gray with only
  semantic color cues (red pending count, orange offline, green live-location/live-capture dot):
  approvals (a checklist), settings (a gear), plus a crossed-wifi live-offline warning when the
  event socket is down, a location pin while the geolocation standing grant is held, and a camera
  while the screenshot standing grant is held. The approvals panel (queue + a Past-tool-calls
  link) follows its content until it reaches the available height, then scrolls its list.
- `settings_panel.tsx` — `SettingsPanel`, the operator settings panel; reads MCP/account and node-daemon
  reflection through the console's Operator-authenticated MCP transport
  toggled from the toolbar's gear: MCP operator account connect/reconnect/disconnect plus linked
  server/web deployment commits. It is shell chrome, not a route.
- `open_external.ts` — `openExternal(url)`: opens a link in a new tab with the opener
  severed, shared by the embed shell (the `openLink` bridge action) and the settings panel
  (the MCP OAuth popup).
- `tool_arguments_field.tsx` / `icons.tsx` — shared tool-argument renderer (per-server
  preview or raw JSON) and inline-SVG icons (never the `@tabler` barrel — see
  `debug/esbuild_tabler_memory.md`), used by both the approvals panel and the history view.
- `tool_result_field.tsx` — the result-side counterpart: a finished call's result as a
  per-server widget (`tool_rendering/<server>/responses.tsx`) over the unwrapped
  `CallToolResult` payload, else the raw-JSON `Result` field (detailed only).
- `console_events.ts` — `useConsoleEvents(onEvent)`: the shared live signal (the
  `/api/events/ws` WebSocket) that carries tool-call and operator-link changes. The server
  broadcasts typed invalidations to every connected tab, so panels and the history view stay live without
  a reload — no client-side cross-tab plumbing needed. It
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
