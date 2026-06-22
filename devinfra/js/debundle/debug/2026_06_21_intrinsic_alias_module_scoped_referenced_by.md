# intrinsic_alias: `referenced_by: @Helper` resolved chunk-globally — FIXED

**Status: fixed** (this branch). Root cause and fix below; kept as an RCA.

Found while preparing the gaffer decorate-trio conversion (2026-06-21). esbuild
emits a byte-identical `__decorate` helper copy per module; the readable `name:`
those helpers carry is generic and repeats across modules (e.g.
`applyDecorators`, shared by ~11 modules' helpers). Each helper's two companions
(`Object.defineProperty` / `Object.getOwnPropertyDescriptor` aliases) are pinned
by `intrinsic_alias { referenced_by: @<helper> }`.

## Symptom

- One decorate trio in a chunk (helper with a unique `name:`) → `intrinsic_alias`
  companions resolve fine.
- Two-or-more trios whose helpers share a generic `name:` (`applyDecorators`) →
  every companion's anchor fails closed before resolution ran:

  ```
  logical_module static/app::alpha: members[].selector.intrinsic_alias
  referenced_by `@applyDecorators` (for member `alphaDefineProp`) is ambiguous —
  several members resolve to it, so it cannot identify a single referencing
  helper. Anchor on a uniquely-named member.
  ```

  This blocked converting the 19 `__defineProperty` + 16
  `__getOwnPropertyDescriptor` gaffer companions whose helper is generically
  named — even though each companion's helper is co-located in its own module and
  is unambiguous there.

## Root cause

`resolve_and_claim_intrinsic_aliases` (in `lowering/materialize/plan_builder.rs`)
built the `referenced_by` anchor map **once per chunk** via
`claimed_member_bindings()` and reused it for every module. That helper iterated
`for plan in &self.module_plans` — i.e. `export_name → binding` over **all**
modules — and its ambiguity-collapse logic mapped any export name claimed by ≥2
distinct bindings to `None`. Two modules' `makes_decorate_call` helpers both
claimed under the shared export name `applyDecorators` (distinct bindings `da`,
`db`), so the chunk-global map collapsed `applyDecorators → None`. `resolve_anchor`
then bailed "ambiguous" before `intrinsic_alias::resolve_intrinsic_alias` ever ran.

The decorate trio is always co-located by esbuild: `makes_decorate_call` (run
before the intrinsic-alias pass) claims the helper into `module_plans[index]` —
the same module index the companion resolves into. So the helper is already
unambiguous _within that module_; only the cross-module merge made it ambiguous.

## Fix

`claimed_member_bindings()` is replaced by `claimed_member_bindings_in_module(index)`,
which iterates only `self.module_plans[index].bindings` (same ambiguity-collapse:
a name claimed by ≥2 distinct bindings _within one module_ still maps to `None`,
fail-closed). `resolve_and_claim_intrinsic_aliases` builds the anchor map **per
module inside the `for (index, request)` loop** and passes it to
`resolve_intrinsic_alias_member`. Module-scoping is the semantically correct
resolution, not a workaround: a cross-module `referenced_by` is not a real esbuild
shape, and the companion's helper is always in its own module. Uniquely-named
helpers are unaffected — their helper is co-located too.

`index` from `enumerate(explicit_requests)` indexes `module_plans` 1:1 (the same
index `claim_post_stage_a_binding` uses for `module_plans[index]` and
`ModuleId(LogicalModuleIndex(index))`), so the scoped map sees exactly the
companion's own module.

Regression test:
`e2e/intrinsic_alias_lowering_test.rs::intrinsic_alias_referenced_by_generic_helper_name_resolves_per_module`
— a chunk with two modules, each a decorate trio whose helper is pinned by
`makes_decorate_call` under the **same** readable name `applyDecorators`, plus an
`intrinsic_alias { property: defineProperty, referenced_by: applyDecorators }`
companion. Asserts each companion resolves to its own module's helper (distinct
minified bindings `pa` / `pb`, never crossing) and the tree runs under Node. Fails
before the fix (chunk-global map collapses `applyDecorators → None` → ambiguous
bail), passes after.

## Follow-up (gaffer, separate pass)

The gaffer decorate-trio conversion (19 `__defineProperty` + 16
`__getOwnPropertyDescriptor` companions, ~35 of the ~72 decorate-trio pins) is no
longer blocked by a generic helper `name:`. Converting them is a separate
gaffer-repo pass once gaffer repins to this ducktape commit; this note records
only the ducktape-side fix.
