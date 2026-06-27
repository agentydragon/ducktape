# haku/console/frontend — dashboard SPA

React 18 single-page app for the Haku console, bundled with esbuild and served
same-origin by the console service. Production serves static files through nginx
in front of the FastAPI API process; FastAPI still has a direct StaticFiles mount
for local/dev serving. Styled with the repo's house stack — **Mantine v7**
components + **Tailwind v4** utilities — modeled on
`finance/augur/frontend` (references root `//:node_modules/*`; no per-package
`package.json`).

- `main.tsx` (wraps the app in `MantineProvider`) → `app.tsx` (the page: tiered open
  items + global feedback box) → `task.tsx` (one item card with its action toggles
  and a per-item feedback box).
- `feedback.tsx` — shared feedback form (global note + per-item, the latter tagged
  with the item id). The Mantine `Button`'s `loading` prop shows an in-flight spinner
  while the commit-push lands; a failure surfaces inline on the `Textarea`.
- `client.ts` — typed `openapi-fetch` client; the types come from the backend's
  OpenAPI schema (the `:schema` target runs `//haku/console:export_schema_bin`), so
  the `Item` Pydantic models are the single source of truth for the wire contract.
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
