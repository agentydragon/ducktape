# Host behavior

Declare what the embedding runtime does, as explicit behavior keys, and derive
the export-reentry policy from those keys plus source facts. Dispatch order is
<../TODO.md>; this plan burns down as steps land.

## Problem

Host facts enter the spec in three unrelated places: `execution_runtime:
generic | native_browser_esm`, `host_no_reentry` (a Bun build identity plus
audited operations and builtin leaves), and browser-specific slots. The
export-reentry policy (`facts/analyze.rs::ExportReentryPolicy`) is a single
boolean: a chunk with CommonJS interop under `generic` is `Conservative`
(`lowering/mod.rs::export_reentry_reports`). Under that policy every at-init
statement with incomplete synchronous execution gets an `eager_use` edge to
every later export and a mirrored `sequenced` edge back. This one rule binds
all ten atomic conflicts of the canonical Claude CLI spec (gaffer-private
`claude/cli/re/2.1.292/debug/2026-10-29-conflict-inventory.md`): the same 100
`common_js_interop` blockers plus the unresolved `ws` request select it in
every chunk.

## Principle

The spec declares behaviors, never a runtime name or version. Each key is one
axiom with one engine reader (STYLE.md § General, explicit policy). An omitted
key takes the conservative value. The identity of the executable a behavior was
audited against is provenance in `note:` and in the project's pinning doc; the
engine does not read it. A named preset, if ever wanted, is sugar that expands
to these keys with the expansion visible in the resolved invocation record, and
is a separate decision.

## Spec surface

```yaml
host_behavior:
  synchronous_module_loader: none | present
  # Only with `present`:
  loader_result_for_evaluating_module: throws | returns_partial_namespace | unknown
  synchronous_load_runs_app_callbacks: never | unknown
  synchronous_load_drains_microtasks: never | unknown
  reentry_free_builtin_modules: [path, fs/promises]
  reentry_free_operations: [async_local_storage_construct, promise_resolve]
  note: "Audited against Bun 1.3.10 standalone, revision …, sha256 …"
```

Landed through step 4: the keys, their validation, the loader-closed
derivation, mandatory suspension, callable reachability and loader-alias
classification (SPEC.md § Loader-closed derivation). Gaffer-private #667
declares the audited block on the canonical Claude CLI 2.1.292 spec; all 76
graphs stay conservative and the ten conflicts are unchanged. Evidence and
per-graph numbers: <../debug/2026_10_30_claude_export_reentry_blockers.md>.

## Remaining loader blockers

Site failures hold every target open, so a graph leaves conservative only once
classes 1–4 all clear and its own static closure clears. Even then class 5 keeps
44 graphs open, including 9 of the 10 conflicting ones (all but `sessionSDK`).
Those 44 graphs carry 91% of the 7.16M edges. Clearing classes 1–4, `process`
and `ws` frees at most the other 32 graphs: 0.67M edges and 692 of the 3,322
`callback_pool_limit` losses, with no conflict guaranteed to move.

1. **Literal requests outside the closed list** (119 sites).
   - **Node builtins** `assert`, `constants`, `querystring` and **Bun
     modules** `bun:ffi`, `undici`, `ws` (`reentry_free_bun_modules`) are in
     the closed lists. Admitting them is the project's audit.
   - **Embedded assets** (about 100: `.md`, `.txt`, two `.mjs`, two `.node`).
     The rule depends on the loader Bun assigns each embedded file, which is
     recorded in the standalone graph's per-file loader byte; the extractor
     drops it today. A `text`, `file` or `json` asset evaluates no code and is
     a reentry-free leaf. A `js` asset outside the analyzed chunks keeps
     targets open. A `napi` asset is class 3. This needs the extractor to emit
     the loader per file (an input fact) and the probe to show the host honours
     it.
2. **`process`** (6 loader requests, 13 loaded-closure requests, and the static
   blocker in 70 graphs) and `worker_threads` are admitted under
   `program_premises.process_prototype_chain_accessors: none` (SPEC.md
   § Program premises). The engine can't derive it: the bundle's own export
   helpers install getters on arbitrary receivers.
3. **Non-literal loader calls** (5 sites, 4 distinct). `computer-use-swift`'s
   environment-path addon load follows an unconditional `throw`, so callable
   reachability excludes it.
   - `audio-capture` and `clipboard-napi` loop over literal `.node` candidate
     paths, and protobufjs `inquire` loads its parameter. They are excluded
     under `program_premises.non_literal_loader_calls: native_addons_only`.
4. **`module`** (3 sites). The sites are a `builtinModules` read
   (stack-utils), a `createRequire` result used only for `.resolve`, and
   fflate's top-level `createRequire("/")("worker_threads")`.
   - The engine can classify these uses. A data-member read and a `.resolve`
     load nothing. A `createRequire` result called with a literal is a literal
     request, resolved against its base. Any other use stays opaque.
   - The host facts needed are that a `createRequire` loader shares
     `import.meta.require`'s audited behavior, and that loading `module` runs
     no application code.
   - fflate's site still requests `worker_threads`, which needs class 2.
5. **Reach-back** (44 targets). This is genuine reentry, not a gap in the
   proof. A request for a chunk L that has not run yet, made while the target
   evaluates, runs L's body there. If L imports the target, L sees the live
   partial namespace: it can call hoisted exports and read initialized
   bindings. `returns_partial_namespace` covers only a request for the
   evaluating module itself. Today's rule (every site counts, failure when the
   loaded closure contains the target) is the right one.
   - A sound tighter bound counts a top-level site only when its chunk can
     evaluate inside the target's window (the target's static closure, or a
     chunk loaded in it), and a nested site only when its chunk can run before
     the target finishes (it does not transitively import the target). Modelled,
     it frees 8 of 46 targets (one conflicting: `taskCancel`) and narrows the
     program-wide classes to 62–75 targets. Build it only once classes 1–4 can
     clear; until then it moves no graph.
   - Narrowing further means bounding which callbacks an incomplete at-init
     call can run (<abstract_interpreter.md>), not changing the loader rule.

## Steps

### 5. Adopt

Canonical Claude CLI replay on an engine with steps 1–4; adopt `host_behavior`
in both private specs with the audit recorded in the pinning doc; report the
gate result as observed. Class B pivots (`YC`, `Gj`, `Ujr`, `xe`) belong to
<synchronous_execution_fallback.md> and `native_operation_inputs`, not here.

## Audit obligation

A `never` or `throws` value is an author assertion about the host. The
project's probe harness in gaffer-private verifies each asserted behavior
against the exact executable named in `note:` and records it beside the pin;
re-audit on every executable change. The engine reports every consumed key in
`inference_uses`.

## Not in scope

Modeling a specific runtime by name; executing the host; the fallback blast
radius (<synchronous_execution_fallback.md>).
