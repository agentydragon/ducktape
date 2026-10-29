# haku/console/frontend — dashboard SPA

React + Mantine + Tailwind SPA, esbuild-bundled, one `ts_library` per file, modeled on
`finance/augur/frontend` (dependencies in the root [`package.json`](../../../package.json),
`//:node_modules/*`; no per-package `package.json`). Production
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
- `styles.src.css` — production Tailwind + `@mantine/core` CSS → `@tailwindcss/cli` →
  `generated/styles.css` → `dist/assets/styles-<hash>.css`. **Deviation:** the `@source` content
  index is a generated file (`tailwind_content_index`) listing the shipped shell sources, since
  Bazel sandboxes inputs. Renderer-only styles and utility classes are added by
  `preview_styles.src.css` for the per-server preview targets.
- `tool_call_card.tsx` + `tool_rendering/` — enriched Gmail, Calendar, Grocy, Haku Routine and
  Tana tool-call renderers. No shipped page mounts them since the tool-call service was retired;
  their MCP lookup clients, schema exporters, and per-server preview targets remain buildable. The
  production `:bundle` excludes this renderer subtree and its stylesheet inputs. Arguments/results
  validate against the MCP schemas `//haku/console/mcp:export_tool_schemas_bin` exports; renderer
  targets consume `ToolCallRecord` from the shared wire model via `:tool_call_record`.
- `tool_arguments_field.tsx` / `tool_result_field.tsx` — argument / result rendering: per-server
  widget (`tool_rendering/<server>/{requests,responses}.tsx`, result over the unwrapped
  `CallToolResult`) else raw JSON (result: detailed only).
- `icons.tsx` — wrappers over **per-icon `@tabler` subpath imports, never the barrel** (OOMs
  esbuild on RBE at ~8.7 GB); ambient types in `tabler_icons.d.ts`.
- `periodic_refresh.ts` — shared 30 s polling interval for Settings resources.
- `index.html` — docroot template; `spa_bundle(fingerprint = True)` rewrites placeholders to hashed
  JS/CSS/logo URLs under `/_console/assets/`.

```bash
bbr build //haku/console/frontend/...      # compile + type-check
bbr build //haku/console/frontend:bundle   # production bundle (dist/)
bbr test //haku/console/frontend/...       # vitest, previews, screenshots
```
