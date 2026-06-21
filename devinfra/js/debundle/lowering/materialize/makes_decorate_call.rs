//! `makes_decorate_call` resolution: the production-lowering bridge from the
//! `spec::MakesDecorateCallTarget` surface to the `selector_solve` owner-graph
//! kernel. This is the **inverse-direction sibling of `passed_to_call`**: it pins a
//! target by the target binding being the **callee** of an esbuild/TypeScript
//! `__decorate`-style decorator application (`H([decorators], C.prototype, "m")`),
//! rather than by the target being *passed to* a call (`passed_to_call`), by its own
//! body / use sites (`reads_member` / `member_of_module`), or by its own minified
//! name.
//!
//! ## Why the trio needs this
//!
//! esbuild emits a byte-identical `__decorate` helper copy per module, reading its
//! `Object.defineProperty` / `Object.getOwnPropertyDescriptor` companions off the
//! global `Object`. The helper definitions therefore have no anchor in their own
//! body — no `source_match` can pin them, and they are stuck as fragile
//! `binding.name` pins. The one re-minify-invariant edge each helper carries is the
//! decorator application it *makes*: the decorated class `C` is a separately-pinned
//! entity reachable through `resolves_to`, and the decorated-member string literal
//! is a source-level identifier. `selector_constraint_model.md` calls this "the
//! use-site disambiguates the copy."
//!
//! ## Where the fact comes from (the deviation from `passed_to_call`)
//!
//! Both ride a call expression derived from the chunk AST, but the join runs the
//! opposite direction. `passed_to_call` keys the fact by the call *argument* (the
//! target is passed in); here the target is the *callee*, so
//! `chunk_facts::decorate_call_uses` keys rows by the callee binding, and the kernel
//! joins each to its declaring owner via `name_owner`. The whole chunk is scanned
//! because a decorator application is a bare top-level statement adjacent to its
//! class.
//!
//! ## The class anchor (ordering decision, shared with `passed_to_call`)
//!
//! A `makes_decorate_call` selector pins by `class: @Anchor` (another member named
//! by its readable `name:` — the decorated class). The kernel needs the anchor's
//! *minified* binding to match the AST decorate-call's class base. As with a
//! `passed_to_call` object anchor, that binding comes from the already-resolved
//! members of the same chunk, not the owner graph's `export_name` — see
//! `materialize::cross_ref` for why that anchor-first ordering is forced.

use analysis::OwnerGraph as FactOwnerGraph;
use spec::MakesDecorateCallTarget;
use swc_ecma_ast::Module;

use super::kind_labels::statement_kind_str_for_spec;
use super::owner_graph_projection::solve_projected;

/// Project the in-memory `analysis::OwnerGraph` + the chunk's AST decorator
/// applications onto the lean owner graph the `selector_solve` kernel consumes, then
/// run the phase-1 solve. The chunk-level `decorate_calls` come from
/// `chunk_facts::decorate_call_uses` (every top-level `H([..], C.prototype, "m")` /
/// `H([..], C)` decorator application, keyed by the callee binding) — the
/// `makes_decorate_call` EDB rows the owner graph alone cannot supply. Per-node
/// `member_reads` / `module_member_uses` are unused by this resolver (it rides the
/// chunk-level decorate-call edge instead), so they stay empty. The shared skeleton
/// lives in `owner_graph_projection::solve_projected`.
pub(super) fn build_resolution(
    graph: &FactOwnerGraph,
    module: &Module,
) -> selector_solve::Resolution {
    let decorate_calls = chunk_facts::decorate_call_uses(module)
        .into_iter()
        .map(|fact| selector_solve::DecorateCallUse {
            callee: fact.callee,
            class_anchor: fact.class_anchor,
            member: fact.member,
        })
        .collect();
    solve_projected(graph, Vec::new(), decorate_calls, Vec::new(), |_node| {
        (Vec::new(), Vec::new())
    })
}

/// The minified binding the `makes_decorate_call` target resolves to, or `None`
/// (fail-closed) when the relation does not pick out exactly one declaring owner.
///
/// `class_binding` is the *minified* name the selector's `class: @Anchor` constraint
/// resolved to (the anchor-first handle) — the decorated class. The kernel narrows
/// over the flat `decorate_call_passes` list by that class anchor, the optional
/// member literal, and the optional `kind`, then `binding_for_owner`. Every step is
/// categorical: zero-or-several survivors collapse to `None`, so an unresolvable
/// `makes_decorate_call` errors at the call site rather than guessing.
pub(super) fn resolve_makes_decorate_call<'a>(
    resolution: &'a selector_solve::Resolution,
    target: &MakesDecorateCallTarget,
    class_binding: &str,
) -> Option<&'a str> {
    let kind = target.kind.map(statement_kind_str_for_spec);
    let owner = resolution.decorate_call_owner(class_binding, target.member.as_deref(), kind)?;
    resolution.binding_for_owner(owner)
}
