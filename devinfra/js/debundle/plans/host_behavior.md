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
classification (SPEC.md § Loader-closed derivation). On the canonical Claude
CLI input with hypothetical audited keys all 76 graphs stay conservative and
the ten conflicts are unchanged. Every target is held open by: five
non-literal calls of the exported loader alias (native addon paths), 119
literal alias calls of names outside the closed builtin list (`assert`,
`querystring`, `bun:ffi`, `undici`, embedded asset files), three `module`
requests, and `ws` in loaded closures. Literal requests into application
chunks reach back into 44 of the 76 targets; no loader site is unreachable.

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
