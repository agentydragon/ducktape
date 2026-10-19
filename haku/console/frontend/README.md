# haku/console/frontend — dashboard SPA

React 18 + Mantine v7 + Tailwind v4 SPA, esbuild-bundled, one `ts_library` per file, modeled on
`finance/augur/frontend` (root `//:node_modules/*`; no per-package `package.json`). Production
serves the fingerprinted bundle from the baked `haku-console-static` nginx image with
route-specific cache headers.

- `main.tsx` → `app.tsx` → `haku_ui_embed.tsx`: persistent shell. The cross-origin iframe stays
  mounted while content switches between Haku UI, Settings, and Past tool calls, preserving bridge
  and in-frame state.
- `routing.ts` — trusted pages `/_console/settings`, `/_console/tool-calls`; chrome-free
  `/_console/approvals-embed` for the desktop approvals window; fingerprinted assets at
  `/_console/assets/*`. Every other path is mirrored into haku-ui; the last frame path is
  remembered per tab across console-page detours.
- `shell_chrome.tsx` — fixed-width left icon rail: approvals trigger (independent non-modal drawer
  over content), page icons, bottom sync / location-sharing / screenshot-capture indicators with
  popovers.
- `approvals_embed_page.tsx` — small-window approvals: approvals tab + shared tool-call cards, no
  navigation chrome or iframe; bridge-only location and screenshot approvals stay in the full shell.
- `settings_panel.tsx` — Agent access and deployment commit links.
- `open_external.ts` — `openExternal(url)`: new tab, opener severed; used by the `openLink` bridge
  action and the account-link popup.
- `tool_arguments_field.tsx` / `tool_result_field.tsx` — argument / result rendering: per-server
  widget (`tool_rendering/<server>/{requests,responses}.tsx`, result over the unwrapped
  `CallToolResult`) else raw JSON (result: detailed only).
- `icons.tsx` — wrappers over **per-icon `@tabler` subpath imports, never the barrel** (OOMs
  esbuild on RBE at ~8.7 GB); ambient types in `tabler_icons.d.ts`.
- `periodic_refresh.ts` — `usePeriodicRefresh`: load on mount, refresh every 30 s over REST.
- `coalesced_refresh.ts` — `useCoalescedRefresh`: one refetch in flight; bursts collapse into one
  catch-up. Used by the history page.
- `client.ts` — `openapi-fetch` client typed from the backend's OpenAPI schema (`:schema` runs
  `//haku/console:export_schema_bin`; Pydantic models are the wire contract), plus launch-routine
  and approval/history helpers.
- `confirm_dialog.tsx` — trusted top-layer confirmations: bridge launches, geolocation grants,
  off-whitelist opens, tool-call approvals.
- `styles.src.css` — Tailwind + `@mantine/core` CSS → `@tailwindcss/cli` → `generated/styles.css`
  → `dist/assets/styles-<hash>.css`. **Deviation:** the `@source` content index is a generated
  file (`tailwind_content_index`) listing the sources, since Bazel sandboxes inputs.
- `index.html` — docroot template; `spa_bundle(fingerprint = True)` rewrites placeholders to hashed
  JS/CSS/logo URLs under `/_console/assets/`.

```bash
bbr build //haku/console/frontend/...      # compile + type-check
bbr build //haku/console/frontend:bundle   # production bundle (dist/)
bbr test //haku/console/frontend/...       # vitest, previews, screenshots
```
