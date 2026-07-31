# `ts_project` migration — findings

Prompted by #3599, where `preview_harness.tsx` files carried a `satisfies
RegisteredToolPreviewFixture` guarantee that no type checker ever ran over, letting three gmail
fixtures drift until the arguments rendered as raw JSON.

**Root cause, stated structurally:** `js_library` does no type checking, so checking lived in one
separate `tsc_test` that re-listed every file — 60 `data` entries against 50 `js_library` targets
in `haku/console/frontend` alone. Two lists that must agree, with nothing enforcing agreement.
Adding a file to one and not the other was silent.

`haku/console/frontend` is now built from `ts_library` (`//devinfra/js:ts_library.bzl`, a thin
`ts_project` wrapper): one target per module, tsc emitting the `.js` and the `.d.ts` in the same
action that type-checks them. Below is what the conversion actually cost, all verified on real
builds — recorded so the next frontend to migrate does not re-derive it.

## What had to be dealt with

1. **The toolchain was never wired.** `aspect_rules_ts` had been a `bazel_dep` (3.8.7) and
   entirely unused. The `bazel_dep` alone yields nothing — `@npm_typescript` does not exist until
   `use_extension("@aspect_rules_ts//ts:extensions.bzl", "ext")` plus `use_repo`. This is
   presumably why the dep sat unused.
2. **`ts_version_from = "//:package.json"` fails.** The workspace pins the range `~5.8.3`, and
   rules_ts mirrors exact versions only: `typescript version ~5.8.3 is not mirrored in rules_ts`.
   `ts_version = "5.8.3"` works and is kept in step with `pnpm-lock.yaml` by hand.
3. **Pass the tsconfig as a dict, not a label.** Handed the shared `tsconfig.json` directly, every
   target inherits its `include: ["**/*.ts"]` and so compiles whatever else is in its sandbox —
   its deps' emitted declarations included. `ts_library` instead passes an empty
   `include`/`exclude` dict with `extends` pointing at the shared `ts_config`, which makes
   rules_ts write a per-target config whose `files` array is exactly that target's `srcs`. That
   sidesteps finding 4 below: the mirrored options land in the generated config, so they agree
   with the attributes by construction rather than by hand.
4. **`ts_project` mirrors some tsconfig options as rule attributes and validates they agree.** A
   first prototype was rejected until `resolve_json_module = True` matched
   `compilerOptions.resolveJsonModule`. Per-target generated configs (finding 3) make this a
   non-issue; sharing one hand-written tsconfig would have rippled every tsconfig change into
   every BUILD file.
5. **`no_emit = True` does not type-check on a plain build.** A deliberately mistyped constant
   left `bazel build` green; only the generated `:<name>_typecheck` caught it.
   With emit on, tsc's own action is the check, so `bazel build` fails on a type error — verified
   by injecting exactly that error into `server_ids.ts`. `no_emit` is used once, for
   `vitest.config.ts`, which vitest must load as `.ts` and so cannot be compiled away; it relies
   on the generated `:vitest_config_typecheck_test`.
6. **`no_emit` propagates no sources, so emit is not optional.** A no-emit `ts_project`
   contributes nothing downstream, so `:service_worker` fails to resolve `./server_ids` —
   esbuild has nothing to bundle. Emitting is therefore forced, which in turn
   forced dropping the codebase's explicit `.ts` import extensions (362 of them; legal only under
   `allowImportingTsExtensions`, itself legal only with `noEmit`). That is the first commit of
   this change.
7. **Every consumer of a `.ts`/`.tsx` entry point moves to the `.js`.** `spa_bundle`'s
   `entry_point`, the screenshot harness's `esbuild.config.mjs`, and each server's
   `preview_screenshots(entry = ...)`. `spa_bundle` itself needed no change, so airlock, augur,
   and rspcache were untouched.
8. **`sw.js` twice.** `:sw_lib` compiles `sw.ts` to `sw.js`, which collided with the esbuild
   bundle of the same name. The bundle moved to `bundled/sw.js`; `pkg_files` flattens it back.
9. **Undeclared deps surface, which is the point.** The old whole-project check had all of
   `//:node_modules` on hand, so missing `deps` never showed. Per-target programs turned up
   `@types/react`, `@types/react-dom`, `vitest`, and `//haku/console/frontend:mcp_result` missing
   from targets that import them.
10. **`js_library` does not hand a `.ts` to tsc.** `rules_js` classifies a plain `.ts` as a
    source rather than a type, and `ts_project` pulls only its deps' types into the compile. A
    generated `.ts` dep therefore reads as "cannot find module" — `//util:data_uri.bzl` had to
    produce a `ts_library` rather than a `js_library`.
11. **Specs are compiled too.** vitest now globs `**/*.test.js` and runs the emitted specs, so
    every spec is a `ts_library` declaring what it imports. Confirmed the same 37 files / 204
    tests run as before.

## Not addressed

- **ESLint never covered this tree, before or after.** `eslint.config.js`'s `projectGlobs` lists
  augur, props, airlock, and `x/agent_server` — not `haku/console/frontend`. The lint aspect does
  visit `ts_project` targets, since that kind is in `lint_eslint_aspect`'s default `rule_kinds`,
  so the conversion loses nothing; there was simply nothing configured to match. Separate gap,
  separate change.
- `airlock/frontend`, `props/frontend`, and `finance/augur/frontend` still use the `js_library` +
  whole-project-`tsc_test` shape. `ts_library` is repo-wide and ready for them.
