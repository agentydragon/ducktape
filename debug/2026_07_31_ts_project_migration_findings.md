# `ts_project` migration — prototype findings

Investigation prompted by #3599, where `preview_harness.tsx` files carried a
`satisfies RegisteredToolPreviewFixture` guarantee that no type checker ever ran over, letting
three gmail fixtures drift until the arguments rendered as raw JSON.

**Root cause, stated structurally:** `js_library` does no type checking, so checking lives in one
separate `tsc_test` that re-lists every file — 60 `data` entries against 50 `js_library` targets
in `haku/console/frontend` alone. Two lists that must agree, with nothing enforcing agreement.
Adding a file in one place and not the other is silent.

`aspect_rules_ts` is already a `bazel_dep` (3.8.7) and **entirely unused**. A prototype converting
two leaf targets works, but is not a drop-in. Findings, all verified on a real build:

1. **The toolchain was never wired.** The `bazel_dep` alone yields nothing — `@npm_typescript`
   does not exist until `use_extension("@aspect_rules_ts//ts:extensions.bzl", "ext")` plus
   `use_repo`. This is presumably why the dep sat unused.
2. **`ts_version_from = "//:package.json"` fails.** The workspace pins the range `~5.8.3`, and
   rules_ts mirrors exact versions only: `typescript version ~5.8.3 is not mirrored in rules_ts`.
   Use `ts_version = "5.8.3"` and keep it in step with `pnpm-lock.yaml` by hand.
3. **Subpackages need a shared `ts_config` target.** `tool_rendering/` and its seven server dirs
   are separate Bazel packages, so each `ts_project` references
   `//haku/console/frontend:tsconfig` rather than the file.
4. **`ts_project` mirrors some tsconfig options as rule attributes and validates they agree.** The
   prototype was rejected until `resolve_json_module = True` was added to match
   `compilerOptions.resolveJsonModule`. Every target carries those attributes, so a tsconfig
   change ripples into BUILD files (or you set `validate = False` and lose the check).
5. **`no_emit = True` does not type-check on a plain build.** A deliberate
   `const x: number = "not a number"` in `action_entry.ts` left
   `bazel build //haku/console/frontend/tool_rendering:action_entry` green. Only the generated
   `:action_entry_typecheck` caught it.

6. **Blocking: `no_emit` propagates no sources, and emitting conflicts with this codebase's
   import style.** With `no_emit = True` a `ts_project` contributes nothing downstream, so
   `//haku/console/frontend:service_worker` fails with
   `Could not resolve "./server_ids.ts" [plugin bazel-sandbox]` — esbuild bundles the `.ts`
   sources today and a no-emit target has none to give. Turning emit on demands a transpiler
   selection, and beyond that the sources import each other with explicit `.ts` extensions
   (362 such imports under `haku/console/frontend`), which TypeScript permits only under
   `allowImportingTsExtensions`, which in turn is legal only with `noEmit` or
   `emitDeclarationOnly`. The convention and the emit path are mutually exclusive.

Point 5 is the one to weigh. rules_ts generates `<name>_typecheck` and `<name>_typecheck_test`
per `ts_project`, so the safety comes from those being **wildcard-discoverable** by
`bazel test //...`, not from the rule checking anything on its own. The win is real but narrower
than it first appears: the enumeration becomes generated rather than hand-written. It is not
"type checking is now unconditional" — a CI that built targets without running tests would be
just as blind as the setup this replaces.

## Verdict

**A straight conversion does not work.** Point 6 is not a detail to work around: the codebase's
`.ts`-extension imports pin it to `noEmit`, and `noEmit` gives esbuild nothing to bundle. The
three ways forward, in increasing order of honesty about the cost:

1. **Drop the `.ts` extensions** (362 imports; `moduleResolution: bundler` resolves
   extensionless), then `ts_project` with an esbuild transpiler works as intended. A large but
   mechanical codemod, and the extensions are non-standard anyway — but it is a source change
   justified by a build concern, which deserves its own decision.
2. **Side-car type-check targets**: keep `js_library` feeding esbuild and add one
   `ts_project(no_emit = True)` per package purely for its generated `_typecheck_test`. Works,
   but each package's `srcs` is still a hand-maintained list — the same failure mode as today,
   with a smaller blast radius. Little real gain.
3. **Keep the current shape.** The concrete gaps are closed (#3599, #3604, #3605), and the
   residual risk is a new file being added to a `js_library` but not to `:tsc_test`'s data.

Scale, if option 1 is ever taken: ~50 targets in `haku/console/frontend` plus 8 subpackages;
`airlock/frontend`, `props/frontend`, and `finance/augur/frontend` would follow.

## Interim state

The two gaps that motivated this are already closed by narrower means: the preview harnesses and
`code_block.test.ts` are in `:tsc_test`'s data (#3599, #3604), and `airlock/frontend` gained a
`svelte_check_test` (#3605). Those keep the current shape and its drift risk; this note exists so
a later migration does not re-derive the six points above.

Prototype branch: `claude/console-ts-project` (unmerged; the two converted leaf targets and the
`MODULE.bazel` toolchain wiring are there as a starting point).
