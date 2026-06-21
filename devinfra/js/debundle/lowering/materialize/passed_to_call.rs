//! `passed_to_call` resolution: the production-lowering bridge from the
//! `spec::PassedToCallTarget` surface to the `selector_solve` owner-graph kernel.
//! This is the **`resolves_to`-of-argument** primitive: it pins a target by the
//! target binding being **passed as an argument** to a call of a known callee —
//! "the class passed to `@registry.register(...)`" — rather than by the target's
//! own body, its own use sites (X2 `reads_member` / X3 `member_of_module`), or its
//! own minified name. It is the documented gap in `member_of_module`'s scope: that
//! primitive pins the owner whose *own* subtree consumes `mod.X`, and explicitly
//! does **not** reach a target distinguished only by an external
//! `registry.register(Target)` statement (the call site declares nothing, so the
//! `declares` conjunct excludes it). This primitive closes that gap.
//!
//! ## Where the fact comes from (the deviation from `member_of_module`)
//!
//! Like the use-site primitives, the raw evidence is a call expression derived
//! from the chunk AST — but the join runs the **opposite direction**. The use-site
//! primitives key the fact by the owner that *contains* the construct (the target
//! is that owner); here the target is the call *argument*, a separately-declared
//! owner, while the call that names it lives in a different statement (typically an
//! anonymous registration the owner graph models as a no-declaration side-effect
//! owner). So `chunk_facts::call_argument_uses` produces rows keyed by the
//! **argument binding**, and the kernel joins each to its declaring owner via
//! `name_owner`. The whole chunk is scanned (not per claimed module), because the
//! registration site can be anywhere.
//!
//! ## The object handle (ordering decision, shared with `reads_member`)
//!
//! A `passed_to_call` selector may constrain the callee's receiver object
//! (`object: @Anchor`, naming another member by its readable `name:` — the
//! registry singleton being the canonical object); the kernel needs the object's
//! *minified* binding to match the AST callee. As with a `reads_member` object
//! anchor, that binding comes from the already-resolved members of the same chunk,
//! not the owner graph's `export_name` — see `materialize::cross_ref` for why that
//! anchor-first ordering is forced.

use analysis::OwnerGraph as FactOwnerGraph;
use spec::PassedToCallTarget;
use swc_ecma_ast::Module;

use super::kind_labels::statement_kind_str_for_spec;
use super::owner_graph_projection::solve_projected;

/// Project the in-memory `analysis::OwnerGraph` + the chunk's AST call-argument
/// passes onto the lean owner graph the `selector_solve` kernel consumes, then run
/// the phase-1 solve. The chunk-level `call_arguments` come from
/// `chunk_facts::call_argument_uses` (every bare-identifier argument of a
/// member-callee call, keyed by the argument binding) — the `passed_to_call` EDB
/// rows the owner graph alone cannot supply. Per-node `member_reads` /
/// `module_member_uses` are unused by the `passed_to_call` resolver (it rides the
/// chunk-level argument edge instead), so they stay empty. The shared skeleton
/// lives in `owner_graph_projection::solve_projected`.
pub(super) fn build_resolution(
    graph: &FactOwnerGraph,
    module: &Module,
) -> selector_solve::Resolution {
    let call_arguments = chunk_facts::call_argument_uses(module)
        .into_iter()
        .map(|fact| selector_solve::CallArgumentUse {
            argument: fact.argument,
            callee_member: fact.callee_member,
            callee_object: fact.callee_object,
            arg_index: fact.arg_index,
        })
        .collect();
    solve_projected(graph, call_arguments, Vec::new(), Vec::new(), |_node| {
        (Vec::new(), Vec::new())
    })
}

/// The minified binding the `passed_to_call` target resolves to, or `None`
/// (fail-closed) when the relation does not pick out exactly one declaring owner.
///
/// `object_binding` is the *minified* name the `object: @Anchor` constraint
/// resolved to (the anchor-first handle), `None` when the selector has no object
/// constraint (then the callee object is unconstrained). The kernel narrows over
/// the flat `call_argument_passes` list by callee member, the optional object, the
/// optional `arg_index`, and the optional `kind`, then `binding_for_owner`. Every
/// step is categorical: zero-or-several survivors collapse to `None`, so an
/// unresolvable `passed_to_call` errors at the call site rather than guessing.
pub(super) fn resolve_passed_to_call<'a>(
    resolution: &'a selector_solve::Resolution,
    target: &PassedToCallTarget,
    object_binding: Option<&str>,
) -> Option<&'a str> {
    let kind = target.kind.map(statement_kind_str_for_spec);
    let owner = resolution.passed_to_call_owner(
        &target.callee_member,
        object_binding,
        target.arg_index,
        kind,
    )?;
    resolution.binding_for_owner(owner)
}
