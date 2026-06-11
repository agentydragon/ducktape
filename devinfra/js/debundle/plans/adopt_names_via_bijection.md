# `adopt_names` via the `source_match` identifier bijection

Proposal / WIP design note — **not yet implemented**. Spun out of #2038, which
gave `AstWildcardMatcher` a full identifier bijection.

## Where things stand

- **`adopt_names` today** (`binding_groups`): maps the **top-level binding
  names declared by the selector** to exported readable names. It is sugar for
  `exports` — the matched top-level binding is renamed to the readable name the
  author wrote in `source_match.match`. See
  `lowering/plans.rs::effective_binding_group_exports` and
  `source_match::source_match_declared_binding_names`. It does **not** reach
  params, locals, or nested bindings.
- **The bijection** (added in #2038): under `identifiers: alpha_all`,
  `AstWildcardMatcher` builds `ident_forward: needle_sym -> candidate_sym`
  (plus the inverse) at structurally-corresponding positions as it walks both
  trees. Holes (`EXPR` / `STMT` / `STMT_LIST` / `CLASS_REST`) never enter the
  bijection — they are absorbed, not recursed — so volatile subtrees do not
  pollute it. The map is currently **discarded** once the boolean match result
  is produced.

## Idea

A `source_match` selector written with readable names is already a near-complete
renaming template. The bijection pairs every readable needle identifier with the
minified candidate identifier at the same structural position, so exposing it
lets **one selector both locate a declaration and adopt readable names onto its
minified identifiers** — params, locals, and nested bindings, not just the
top-level binding.

```js
// selector (identifiers: alpha_all)
function compute(items, limit) {
  const total = items.reduce(EXPR, 0);
  return total > limit ? limit : total;
}
```

```js
// matched minified declaration
function f(a, b) {
  const c = a.reduce((x) => x.n, 0);
  return c > b ? b : c;
}
```

Bijection: `compute<->f`, `items<->a`, `limit<->b`, `total<->c`. Today
`adopt_names` can rename `f -> compute`; the extension also adopts
`a -> items`, `b -> limit`, `c -> total`.

## Sketch

1. **Expose the bijection.** Add a `source_match` entry point that, for a
   uniquely matched declaration, returns the `needle -> candidate` map (the
   matcher already holds it; surface it instead of dropping it). Leave the
   existing boolean / resolve APIs unchanged.
2. **Restrict to safe targets.** Only adopt names onto candidate identifiers
   that are **bindings introduced within the matched region** (params, and
   `const` / `let` / `var` / function / class locals declared inside the matched
   subtree) — never free references to outer or global names. The bijection
   also pairs _uses_, so a selector that writes `Math` pairs it with candidate
   `Math`; renaming that must be a no-op. This needs a small scope pass over the
   matched candidate to classify each paired symbol as local-binding vs. free.
3. **Spec surface.** Opt-in mode, default off — e.g. extend
   `BindingGroupAdoptNames` (and `members[].selector`) with `adopt_locals`
   (or an `adopt_names: deep` variant) that turns on bijection-based renaming in
   addition to today's top-level adoption.
4. **Apply as scoped renames.** Feed `{candidate_local -> needle_readable_name}`
   into the existing rename machinery, scoped to the matched declaration. Reuse
   member-rename collision handling; hard-error if a target name collides with
   another binding visible in that scope. (Two needle names mapping onto one
   candidate symbol cannot happen — the bijection is injective — so assert it
   rather than handle it.)
5. **Holes and exactness.** Names come only from explicit, non-hole
   identifiers, so volatile bodies behind `EXPR` / `STMT_LIST` are skipped for
   free. `exact` (non-alpha) selectors have no bijection to adopt from, so the
   mode requires `identifiers: alpha_all`.

## Open questions

- **Granularity.** Binding groups only, or also `members[].selector` and
  `anonymous_statements[]`? Members already adopt the top-level name; locals are
  the new bit.
- **Scope classification.** Cheapest reliable way to tell "local binding in the
  matched region" from "free reference" — likely a visitor over the matched
  candidate subtree collecting declared binding symbols, intersected with the
  bijection's candidate side.
- **Partial adoption.** Allow `adopt_locals: [items, total]` to adopt only some
  readable names, mirroring `adopt_names: [..]` for top-level?
- **Conflict policy** when an adopted name equals one the candidate already uses
  elsewhere in the same scope (rename-around vs. hard error).

## Why this is cheap now

#2038 already pays for building the bijection on every `alpha_all` match, so
this reuses work already done. It also pairs naturally with the multi-hole
fingerprint selectors #2038 enabled: "pin a few stable members, hole the rest,
**and adopt the readable names I wrote onto whatever matched**."
