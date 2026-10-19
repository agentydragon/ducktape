# haku/console/frontend — dashboard SPA

React 18 + Mantine v7 + Tailwind v4 SPA, esbuild-bundled, one `ts_library` per file, modeled on
`finance/augur/frontend` (root `//:node_modules/*`; no per-package `package.json`). Production
serves the fingerprinted bundle from the baked `haku-console-static` nginx image with
route-specific cache headers.

- `main.tsx` → `app.tsx` → `haku_ui_embed.tsx`: persistent shell. The cross-origin iframe stays
  mounted while content switches between Haku UI and Settings, preserving bridge and in-frame
  state.
- `routing.ts` — trusted Settings page at `/_console/settings`; fingerprinted assets at
  `/_console/assets/*`. Every other path is mirrored into Haku UI; the last frame path is remembered
  per tab across console-page detours.
- `shell_chrome.tsx` — fixed-width left icon rail: bridge capability-request drawer, page icons,
  and location-sharing, screenshot-capture, and session-expiry indicators with trusted controls.
  The request drawer handles only Agent UI bridge geolocation and screenshot requests.
- `settings_panel.tsx` — Agent access and deployment commit links.
- `open_external.ts` — `openExternal(url)`: new tab, opener severed; used by the `openLink` bridge
  action and the account-link popup.
- `client.ts` — `openapi-fetch` client typed from the backend's OpenAPI schema (`:schema` runs
  `//haku/console:export_schema_bin`; Pydantic models are the wire contract), with config, operator,
  deployment, agent enrollment, and launch-routine helpers.
- `confirm_dialog.tsx` — trusted top-layer confirmations for bridge launches and off-whitelist opens.
- `styles.src.css` — Tailwind + `@mantine/core` CSS → `@tailwindcss/cli` → `generated/styles.css`
  → `dist/assets/styles-<hash>.css`. The `@source` content index is generated (`tailwind_content_index`)
  because Bazel sandboxes inputs.
- Retired enriched Gmail, Calendar, Grocy, Haku Routine, and Tana renderer source is preserved as
  inert `.txt` snapshots under [`../archive/tool_renderers/`](../archive/tool_renderers/README.md),
  with the former wire contract and generated MCP schemas. The archive has no build targets.
- `index.html` — docroot template; `spa_bundle(fingerprint = True)` rewrites placeholders to hashed
  JS/CSS/logo URLs under `/_console/assets/`.

```bash
bbr build //haku/console/frontend/...      # compile + type-check
bbr build //haku/console/frontend:bundle   # production bundle (dist/)
bbr test //haku/console/frontend/...       # vitest and shell/settings screenshots
```
