//! `intrinsic_alias` resolution: the production-lowering bridge from the
//! `spec::IntrinsicAliasTarget` surface to the `selector_solve` owner-graph kernel.
//! This is the **follow-on companion of `makes_decorate_call`**: it pins a target
//! that is an alias of an intrinsic method off the unshadowed global `Object`
//! (`var X = Object.defineProperty` / `var X = Object.getOwnPropertyDescriptor`) by
//! the helper that **references** it, rather than by the target making a decorator
//! call (`makes_decorate_call`), being passed to one (`passed_to_call`), its own
//! body / use sites (`reads_member` / `member_of_module`), or its own minified name.
//!
//! ## Why the trio's companions need this
//!
//! esbuild emits a byte-identical `__decorate` helper copy per module, reading its
//! `Object.defineProperty` / `Object.getOwnPropertyDescriptor` companions off the
//! global `Object`. `makes_decorate_call` retires the helper itself (pinned by the
//! class it decorates), but the two companions have *no* such call to ride: they are
//! plain `var X = Object.<method>` aliases with no anchor in their own body — no
//! `source_match` can pin them (N byte-identical copies, the anchor is the global
//! `Object`, not a spec member). The one re-minify-invariant edge each carries is
//! that it is read **only inside** its trio's `__decorate` helper body (exactly one
//! referencer). So this primitive pairs (a) the structural recognition of
//! `var X = Object.<property>` off the unshadowed global `Object` with (b) an
//! inverse-`references` edge — "the alias referenced by `@<decorateHelper>`" —
//! narrowed by the intrinsic property name. `selector_constraint_model.md` calls
//! this "the `Object.<method>` intrinsic alias referenced by `@<decorateHelper>`".
//!
//! ## Where the fact comes from (the deviation from `makes_decorate_call`)
//!
//! Both retire decorate-trio members, but the join runs differently.
//! `makes_decorate_call` keys its fact by the helper callee and rides the AST
//! decorate-call edge; here the target is the *aliased binding*, so
//! `chunk_facts::intrinsic_alias_uses` keys rows by the alias binding (joined to its
//! declaring owner via `name_owner`) and carries the intrinsic property. The
//! referencer edge it rides is the owner graph's own `references` edge (the same
//! relational edge `cross_ref` uses), not a new AST scan. The chunk-level rows are
//! emitted only for the **unshadowed** global `Object` (the EDB's fail-closed
//! identity guard), so a shadowed / reassigned / imported `Object` yields no rows
//! and the selector fails closed.
//!
//! ## The helper anchor (ordering decision, shared with `makes_decorate_call`)
//!
//! An `intrinsic_alias` selector pins by `referenced_by: @Helper` (another member
//! named by its readable `name:` — the trio's `__decorate` helper, itself pinned by
//! `makes_decorate_call`). The kernel needs the helper's *owner* to match the AST
//! `references` edge's source. That owner comes from the helper's already-resolved
//! minified binding — `resolution.owner_for(helper_binding)` — not the owner graph's
//! `export_name`; see `materialize::cross_ref` for why that anchor-first ordering is
//! forced.

use analysis::OwnerGraph as FactOwnerGraph;
use spec::IntrinsicAliasTarget;
use swc_ecma_ast::Module;

use super::owner_graph_projection::solve_projected;

/// Project the in-memory `analysis::OwnerGraph` + the chunk's AST intrinsic-alias
/// declarations onto the lean owner graph the `selector_solve` kernel consumes, then
/// run the phase-1 solve. The chunk-level `intrinsic_aliases` come from
/// `chunk_facts::intrinsic_alias_uses` (every top-level `var X = Object.<property>`
/// off the unshadowed global `Object`, keyed by the alias binding) — the
/// `intrinsic_alias` EDB rows the owner graph alone cannot supply. The referencer
/// edge it rides is the owner graph's own `references` edge (projected by
/// `solve_projected`), so per-node `member_reads` / `module_member_uses` stay empty.
pub(super) fn build_resolution(
    graph: &FactOwnerGraph,
    module: &Module,
) -> selector_solve::Resolution {
    let intrinsic_aliases = chunk_facts::intrinsic_alias_uses(module)
        .into_iter()
        .map(|fact| selector_solve::IntrinsicAliasUse {
            binding: fact.binding,
            property: fact.property,
        })
        .collect();
    solve_projected(graph, Vec::new(), Vec::new(), intrinsic_aliases, |_node| {
        (Vec::new(), Vec::new())
    })
}

/// The minified binding the `intrinsic_alias` target resolves to, or `None`
/// (fail-closed) when the relation does not pick out exactly one declaring owner.
///
/// `referenced_by_binding` is the *minified* name the selector's
/// `referenced_by: @Helper` constraint resolved to (the anchor-first handle — the
/// `__decorate` helper). The kernel first resolves that binding to its declaring
/// owner (`owner_for`), then narrows the flat `intrinsic_alias_references` list by
/// the intrinsic `property` and that referencer owner, then `binding_for_owner`.
/// Every step is categorical: zero-or-several survivors collapse to `None`, so an
/// unresolvable `intrinsic_alias` errors at the call site rather than guessing — in
/// particular a shadowed `Object` (no EDB rows) and a helper whose binding is itself
/// ambiguous both fail closed.
pub(super) fn resolve_intrinsic_alias<'a>(
    resolution: &'a selector_solve::Resolution,
    target: &IntrinsicAliasTarget,
    referenced_by_binding: &str,
) -> Option<&'a str> {
    let referencer = resolution.owner_for(referenced_by_binding)?;
    let owner = resolution.intrinsic_alias_owner(&target.property, referencer)?;
    resolution.binding_for_owner(owner)
}
