# haku/console/frontend — dashboard SPA

React 18 single-page app for the Haku console, bundled with esbuild and served
same-origin by the console service. Production serves the fingerprinted bundle
from the baked `haku-console-static` nginx image, with route-specific cache
headers. Styled with the repo's house stack — **Mantine v7**
components + **Tailwind v4** utilities — modeled on
`finance/augur/frontend` (references root `//:node_modules/*`; no per-package
`package.json`).

Every module is its own `ts_library` (`//devinfra/js:ts_library.bzl`), one target per file. tsc
type-checks each target as it compiles it, so `bbr build` is the type check — there is no separate
whole-project checker whose file list could drift from the library graph. esbuild bundles the
emitted `.js`, and vitest runs the emitted `.test.js`.

- `main.tsx` → `app.tsx` → `haku_ui_embed.tsx`, the persistent application shell. The cross-origin
  iframe remains mounted while the content area switches between Haku UI, Settings, and Past tool
  calls, preserving bridge and in-frame state.
- `routing.ts` — `/_console/settings` and `/_console/tool-calls` are trusted console pages;
  `/_console/assets/*` holds fingerprinted browser assets. Every other pathname is mirrored into
  haku-ui, and the last frame path is remembered per tab across console-page detours.
- `shell_chrome.tsx` — a fixed-width icon rail that reserves the left edge of the viewport. Its
  top approvals trigger controls an independent non-modal drawer over the content area; page icons
  select Haku UI, Settings, or Past tool calls; bottom indicators expose sync, location-sharing,
  and screenshot-capture state through compact popovers.
- `settings_panel.tsx` — the Settings page. It reads MCP-server and node-daemon reflection through
  the console's Operator-authenticated MCP transport, validating each result against the
  Python-generated MCP result-schema catalog, and hosts account connect/disconnect, per-Agent
  auto-approval policy, Web Push registration, and the deployment commit links.
- `open_external.ts` — `openExternal(url)`: opens a link in a new tab with the opener
  severed, shared by the embed shell (the `openLink` bridge action) and the settings panel
  (the MCP OAuth popup).
- `tool_arguments_field.tsx` / `icons.tsx` — shared tool-argument renderer (per-server
  preview or raw JSON) and the icon set, used by both the approvals panel and the history view.
  Icons are thin wrappers over **per-icon `@tabler` subpath imports**, never the barrel, which OOMs
  esbuild on RBE at ~8.7 GB (<../debug/esbuild_tabler_memory.md>); their ambient types live in
  `tabler_icons.d.ts`.
- `tool_result_field.tsx` — the result-side counterpart: a finished call's result as a
  per-server widget (`tool_rendering/<server>/responses.tsx`) over the unwrapped
  `CallToolResult` payload, else the raw-JSON `Result` field (detailed only).
- `console_events.ts` — `useConsoleEvents(onEvent)`: the shared live signal (the `/api/events/ws`
  WebSocket) carrying tool-call, operator-link and chat-session changes. The server broadcasts typed
  invalidations to every connected tab, so panels, the history view and open transcripts stay live
  without a reload and without client-side cross-tab plumbing. It auto-reconnects with backoff,
  refetches on reconnect to catch up, and returns a `LiveStatus` (`connecting`/`live`/`offline`)
  the shell uses to warn when the channel is down. Every consumer sees every event, so
  `changedSessionId(event)` is how the tool-call surfaces skip session invalidations, which a
  streaming turn emits every coalescing window.
- `coalesced_refresh.ts` — `useCoalescedRefresh(read)`: at most one refetch in flight, with a burst
  of live events collapsing into a single catch-up afterwards, since overlapping fetches buy answers
  the next one discards. Used by the history page and both conversation surfaces.
- `client.ts` — typed `openapi-fetch` client; the types come from the backend's
  OpenAPI schema (the `:schema` target runs `//haku/console:export_schema_bin`), so
  the Pydantic models are the single source of truth for the wire contract. Includes the
  launch-routine helper, MCP approval queue helpers (`pending`, approve, deny), and
  MCP operator-account association helpers.
- `confirm_dialog.tsx` — trusted top-layer confirmations for bridge launches, geolocation
  grants, off-whitelist opens, and MCP tool-call approvals.
- `styles.src.css` — `@import`s Tailwind + `@mantine/core` CSS; compiled by
  `@tailwindcss/cli` to `generated/styles.css`, then fingerprinted into
  `dist/assets/styles-<hash>.css` and served at `/_console/assets/…`. Deviation from a plain Tailwind setup: the `@source`
  content index is a generated file (`tailwind_content_index`) concatenating the sources
  Tailwind must scan, since Bazel sandboxes the inputs.
- `index.html` — the docroot shell template; `spa_bundle(fingerprint = True)` rewrites
  placeholders to hashed JS/CSS/logo URLs under `/_console/assets/`.

```bash
bbr build //haku/console/frontend/...      # compile + type-check everything
bbr build //haku/console/frontend:bundle   # production bundle (dist/)
bbr test //haku/console/frontend/...       # vitest, previews, screenshots
```
