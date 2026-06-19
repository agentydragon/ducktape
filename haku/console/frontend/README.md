# haku/console/frontend — dashboard SPA

React 18 single-page app for the Haku console, bundled with esbuild and served
same-origin by the console's FastAPI backend. Modeled on `finance/augur/frontend`
(references root `//:node_modules/*`; no per-package `package.json`).

- `main.tsx` → `app.tsx` (the page: tiered open items + feedback box) → `task.tsx`
  (one item card with its action toggles).
- `client.ts` — typed `openapi-fetch` client; the types come from the backend's
  OpenAPI schema (the `:schema` target runs `//haku/console:export_schema_bin`), so
  the `Item` Pydantic models are the single source of truth for the wire contract.
- `markdown.ts` — item `body` → sanitized HTML (`marked` + `dompurify`).
- `index.html` + `styles.css` — the docroot the backend serves.

```bash
bbr build //haku/console/frontend:bundle   # production bundle (dist/)
bbr test //haku/console/frontend/...        # tsc type-check + vitest
```
