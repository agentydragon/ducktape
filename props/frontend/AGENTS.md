@README.md

## Bazel

One `ts_library` per `.ts`/`.tsx` file in its sub-package's `BUILD.bazel`; `bbr build
//props/frontend:bundle` checks the app graph. Sub-packages: `src/lib/` (utilities, API client,
stores, links), `src/components/` (+ `stats/`), `src/pages/`; the parent holds app entry,
bundler, dev server and test infra. `//props/frontend/src/lib:schema` generates
`schema.d.ts`.

**Deviation — `@tabler/icons-react`:** never the barrel import; it makes esbuild peak ~8.7 GB and
OOM the RBE VM (no per-action memory lever). Import each icon by subpath
(`@tabler/icons-react/dist/esm/icons/IconX.mjs`, default export), as <src/lib/icons.tsx> does;
<src/lib/tabler_icons.d.ts> declares the module for `tsc`.
