# `import/order` is incompatible with our Bazel ESLint setup

**Date:** 2026-06-02
**Status:** `import/order` stays **off** in `eslint.config.js`. Confirmed not fixable
via config; see "What was tried" below.

## Symptom

Enabling `import/order` (from `eslint-plugin-import-x`, registered as `import`)
makes the ESLint action crash — exit 2, not a lint violation:

```
TypeError [ERR_INVALID_ARG_TYPE]: The "path" argument must be of type string. Received null
Rule: "import/order"
    at Object.dirname (node:path)
    at getFilePackagePath (eslint-plugin-import-x/lib/utils/package-path.js:8)
    at getContextPackagePath (eslint-plugin-import-x/lib/utils/package-path.js:5)
    at isExternalPath (eslint-plugin-import-x/lib/utils/import-type.js:69)
    at typeTest → importType → computeRank (eslint-plugin-import-x/lib/rules/order.js)
```

## Root cause

To order imports, `import/order` must rank each one, which classifies it as
external/internal via `isExternalPath`. That calls `getContextPackagePath`,
which walks **up from the file being linted** looking for a `package.json`
(`pkgUp`). Under Bazel the source is executed from `bazel-out/.../<pkg>/file.tsx`
(per-file aspect) or the test runfiles tree (whole-program test) — **there is no
`package.json` anywhere above it**, so `pkgUp` returns `null` and
`path.dirname(null)` throws.

This is **not** about resolving the _imports_ (the original code comment guessed
that); it's about locating the _current file's_ package. So resolver config
doesn't help.

## What was tried (all still crash)

1. Flip `import/order: "error"` in the gate config → crash (per-file aspect).
2. Add a node resolver with `.ts`/`.tsx` extensions (`import-x/resolver`) → crash.
3. Set both `import-x/resolver` **and** `import/resolver` namespaces → crash.
4. Move it to the **whole-program** test (`//augur/frontend:eslint_typed_test`,
   `parserOptions.projectService`, full tree + node_modules in one sandbox) →
   crash (same `getContextPackagePath`).
5. Add `//:package.json` to that test's `data` so `pkgUp` could find one →
   Bazel **analysis error** (js_test data staging conflict) — a separate yak to
   shave, and even if cleared, see below.

Even past the crash, enabling `import/order` would flag a repo-wide reordering
that only `eslint --fix` can apply — and `--fix` is impractical under RBE (the
lint aspect runs read-only; we don't have a hermetic local `eslint --fix` path).

## Conclusion / recommendation

`import-x`'s package-path walk fundamentally fights Bazel's `bazel-out`
execution layout. Not worth working around in ESLint.

A **Prettier import-sort plugin** (`@ianvs/` or `@trivago/prettier-plugin-sort-imports`)
was then evaluated as the alternative — and **parked**:

- It can sort `.ts` / `.tsx` / `.js` (these plugins hook Prettier's
  babel/typescript parsers — purely syntactic, no resolver / `package.json` /
  `bazel-out` issues), but **not `.svelte`**: they don't touch
  `prettier-plugin-svelte`'s parser, so the `.svelte` components in `props` and
  `airlock` (the bulk of those apps) wouldn't be sorted.
  Coverage would be inconsistent within a single frontend (a `.ts` sorted, the
  `.svelte` next to it not).
- Wiring spans **three build systems**: the Nix-bundled prettier
  (`nix/packages/prettier/` — `package.json` + an `npmDepsHash` rebuild, since
  pre-commit's `language: system` prettier resolves plugins off the Nix wrapper's
  `NODE_PATH`), the pnpm/Bazel `prettier_bin` (`//:prettierrc` deps + lockfile),
  and `.prettierrc.cjs` (`require()` + `importOrder` groups, with
  `importOrderSideEffects` care so a side-effect `import "./x.css"` / polyfill
  isn't reordered into a behavior change) — plus a one-time repo-wide reorder.

For that partial, frontend-inconsistent coverage at a multi-system cost, it
wasn't worth it. The import hygiene that DOES work under Bazel is already on
(`import/first`, `import/no-duplicates`, `import/newline-after-import`); only
grouping/ordering is missing. (`prettier-plugin-organize-imports` uses the TS
language service — likely the same project-context problems, so the syntactic
plugin is the only candidate.)
