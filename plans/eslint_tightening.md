# ESLint tightening & standardization

Plan to standardize and tighten the repo-wide ESLint config (`eslint.config.js`)
now that `--@aspect_rules_lint//lint:fail_on_violation` (added in #1794) makes
lint findings a hard `bazel build` gate.

## The reframe: `warn` is now dead weight

`fail_on_violation` only fails the build on ESLint **errors**, not warnings. So
the gate changed what severity means:

|         | before the gate | after the gate (now)                                          |
| ------- | --------------- | ------------------------------------------------------------- |
| `error` | report-only     | **blocks `bazel build`**                                      |
| `warn`  | report-only     | **invisible** — prints to a log nobody reads on a green build |

Several rules are currently `warn`, so they no longer do anything at the gate:

| Rule                                | Current | Where                     |
| ----------------------------------- | ------- | ------------------------- |
| `@typescript-eslint/no-unused-vars` | `warn`  | `coreRules` (TS + Svelte) |
| `svelte/no-unused-svelte-ignore`    | `warn`  | Svelte block              |
| `no-console`                        | `warn`  | `coreRules`               |
| `no-unused-vars` (study_casino)     | `warn`  | study_casino block        |

"Tightening" is therefore mostly: decide which `warn` rules become `error`.

## Version skew: none for ESLint

Unlike ruff (multitool `0.15.8` in the lint aspect vs `pkgs.ruff 0.14.6` in the
devshell/pre-commit), ESLint is **single-sourced**: the Bazel aspect uses the
pnpm-locked `eslint@9.39.4` (`//devinfra/lint:eslint`), and ESLint is
deliberately **not** in pre-commit (see `.pre-commit-config.yaml` — "handled by
Bazel"). There is nothing to reconcile. (A stray transitive `eslint@8.4.1` in
`pnpm-lock.yaml` belongs to another tool and is unused by the aspect — cosmetic.)

## Proposed PRs (sequenced by value × ease)

### P1 — Promote dead-code `warn`s → `error` — Low effort, High value

Flip to `error` and fix the existing violations first:

- `@typescript-eslint/no-unused-vars` (`coreRules`; TS + Svelte)
- `svelte/no-unused-svelte-ignore`
- `no-unused-vars` (study_casino block)

Keep the existing `argsIgnorePattern` / `varsIgnorePattern: ^_` escape hatch;
only the severity changes.

Known violations (representative, enumerated exhaustively at apply time): augur
`currencyDisplay`, `bucketLabel`; props `App.svelte` `goto`, `SnapshotsPage`
`resolve`, `RunDetail` `llmRequestsFetched` (assigned twice, never read);
`RunTriggerModal` stale `svelte-ignore`.

**Rationale:** ruff's unused-import / unused-var (`F401` / `F841`) are already
hard errors in this repo — TS/Svelte should match. Dead code shouldn't build.

### P2 — Enable `typescript-eslint` recommended — Medium effort, High value

Replace the two hand-picked `@typescript-eslint/*` rules with the recommended
set (keep `consistent-type-imports`, which isn't in it). Adds, among others:

- `ban-ts-comment`, `no-explicit-any`, `no-empty-object-type`, `no-misused-new`

**✅ Done (#1797).** Implementation note: adding the `typescript-eslint`
meta-package is a trap — it bundles its own plugin copy at a newer patch (8.60
vs the repo's 8.58), tripping `aspect_rules_js`'s public-hoist conflict (two
versions hoisted to `/node_modules/@typescript-eslint/eslint-plugin`), and once
that hit the lockfile the build wedged. Instead we spread the already-present
plugin's eslintrc `recommended.rules` (self-contained: TS rules + the
core-rule-off pairs it supersedes) — **zero dependency/lockfile change**. Only 8
violations surfaced (7 `no-explicit-any` + 1 `no-unused-expressions`, all in the
props frontend; `ban-ts-comment` does not fire on `@ts-ignore` inside `.svelte`
`<script>` blocks). Follow-up: dropped the now-redundant `no-unused-vars: off`.

### P3 — Type-aware linting + `no-floating-promises` — High effort, High value (spike first)

Wire `parserOptions.projectService` so type-aware rules run, then enable a
curated subset: `no-floating-promises`, `no-misused-promises`, `await-thenable`.

**Risk (anticipated):** the same class of problem that disabled `import/order`
(the resolver needs tsconfig/filesystem access under `bazel-out`).

**✅ Spike succeeded — augur landed (#1799).** The feared resolver risk didn't
materialize; the real constraint is _architectural_: type-aware rules need the
full TS program, which the **per-file lint-gate aspect can't supply** (each lint
action sees only one file + its deps). The fix is to run type-aware lint as a
**whole-program test target** — `eslint` with `parserOptions.projectService` and
inputs mirroring `tsc_test` — _not_ a gate rule. `//augur/frontend:eslint_typed_test`
does this (`no-floating-promises`, `no-misused-promises`, `await-thenable`) and
passes clean.

**Rollout is heterogeneous** — only augur fit the pattern directly:

- augur — React/TSX, Bazelized → ✅ done.
- `x/rspcache/admin_ui` — React/TSX but **not Bazelized** (empty BUILD, manual
  `npx` build/typecheck) → blocked on Bazelizing it first.
- props / airlock / `x/agent_server/web` — Svelte, Bazelized → need a separate
  **type-aware-svelte** mini-spike (whether `eslint-plugin-svelte` + the TS
  project service work through the Svelte parser under Bazel is an open question).

### P4 — De-dupe / standardize config structure — Low–Medium effort, Medium value

Pure refactor (verify zero behavior change with a build):

- Factor the repeated `plugins` / browser-`globals` / react-`settings` /
  unused-vars blocks into named consts (the TS and Svelte blocks duplicate them).
- Decide study_casino's fate: **(a)** share its react/unused-vars config now
  (stays JS), or **(b)** migrate to TS and add to `projectGlobs` (bigger — like
  the augur migration). Recommend (a) now, track (b) as a follow-up.

### P5 — `no-console`: promote → `error` or drop — Low effort, Low–Medium value

It's invisible under the gate. `console.warn` / `console.error` are already
allowed, so promoting to `error` only blocks stray `console.log`. Recommend
promote; quick count + fix.

## Category A — minor cleanups (fold into the PRs above)

- `augur/frontend/app.tsx:425` `react-hooks/exhaustive-deps` disable has **no
  justification comment** — add one (or refactor the effect).
- A couple `svelte/no-at-html-tags` disables in `x/agent_server/web` lack
  comments (`FileViewer.svelte`'s is well-justified) — add brief rationale.

## Dependency to watch

- **#1790** (open, augur `/api/bootstrap` split from another session) touches
  `augur/frontend/client.ts`. P1/P2 touch the augur frontend — sequence after
  #1790 lands or expect a trivial rebase.

## Status

- [x] P1 — promote dead-code warns → errors (#1796)
- [x] P2 — typescript-eslint recommended (#1797)
- [x] P3 — type-aware linting: spike + augur landed (#1799). Rollout pending —
      svelte frontends need a type-aware-svelte spike; rspcache needs Bazelizing.
- [ ] P4 — de-dupe / standardize config
- [ ] P5 — `no-console`

Follow-up cleanups (post-P2, #1798): dropped the redundant `no-unused-vars: off`,
and loosened the eslint-family dep pins to major-floor carets — safe because
ESLint is single-sourced via Bazel (see "Version skew" above), so there's no
second copy to drift against.

Tombstone this plan once P1–P5 are resolved (applied or documented-as-rejected).
