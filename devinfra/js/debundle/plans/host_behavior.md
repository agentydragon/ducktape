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

Landed through step 2: the keys, their validation, and the loader-closed
derivation (SPEC.md § Loader-closed derivation). That derivation counts every
loader site in every analyzed chunk. On the canonical Claude CLI input with
hypothetical audited keys it keeps all 76 graphs conservative and leaves the
ten conflicts unchanged: the exported loader alias, two `module` imports and
`ws` in loaded closures keep every target open (dynamic `import()` is not a
synchronous loader site and holds nothing); all 476 literal
`import.meta.require` calls target application chunks, and their closures
reach back into 46 of the 76 targets (9 of the 10 conflict chunks); `ws`
remains an unresolved static request in 7 of them. Steps 3 and 4 narrow the
sites to those that can run while the target evaluates.

## Steps

### 3. Mandatory suspension

A site-scoped phase fact: a loader call that follows an `await` on every path
reaching it cannot run on the invoking synchronous stack. Model completion
kinds (normal, return, throw, break, continue), branch joins, `catch` and
`finally`; visit the await operand before marking suspension; unsupported
shapes stay unknown. Existing `past_await` summaries reset across `try` and
cannot prove the watcher shape the handoff describes.

### 4. Callable reachability and the loader alias

Prove at-initialization reachability of the callable containing a loader site,
including aliases, returned functions, getters, constructors and host-invoked
callbacks; syntactic nesting proves neither presence nor absence. An exported
loader alias is an independent escape and is classified for every guarded
chunk; proving only a target's small import closure is insufficient.

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
