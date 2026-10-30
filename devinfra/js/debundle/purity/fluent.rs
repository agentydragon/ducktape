use super::*;
use crate::facts::{close_const_aliases, top_level_initializers};

/// Close the author-asserted fluent roots (`seed`, the projected
/// `fluent_exports` locals) over chunk-top `const X = <fluent chain>`
/// declarations: `const Base = k.object({...})` makes `Base` a fluent
/// root too, so `Base.extend({...})` downstream is admitted. `const`
/// only — a `let`/`var` cell can be rebound to an untrusted value.
///
/// Only the *value class* is derived here (the value came out of the
/// trusted API, so the author's deep-purity assertion covers it); the
/// init's own evaluation effects — impure arguments anywhere in the
/// chain — are classified separately at the declaration site by
/// `classify_fluent_chain`. This mirrors the
/// primitive-binding value-class/evaluation split.
///
/// Positive closure rather than a single source pass so a chain rooted
/// in a later-declared const still closes; bounded by one fact per
/// chunk-top const.
pub(crate) fn collect_fluent_const_bindings(
    body: &[TopLevelItemView<'_>],
    seed: &BTreeSet<String>,
) -> BTreeSet<String> {
    if seed.is_empty() {
        return BTreeSet::new();
    }
    let aliases = body.iter().flat_map(|item| {
        top_level_initializers(item.as_module_item())
            .into_iter()
            .filter_map(|(target, init, kind)| {
                (kind == VarDeclKind::Const)
                    .then(|| fluent_chain_root(init))
                    .flatten()
                    .map(|root| (target, Some(root.to_owned())))
            })
    });
    close_const_aliases(seed, aliases)
}

/// The root identifier of a fluent chain — a chain of static
/// (non-computed) member reads, calls, and optional-chaining forms of
/// those, hanging off a bare `Ident`. Returns `None` for any other
/// shape: computed members are excluded because admitting the lookup
/// would additionally require the key expression to be
/// ToPropertyKey-safe, and `new` is excluded because construction off
/// a fluent API is not part of the asserted contract (builder APIs
/// chain calls, not constructors).
pub(crate) fn fluent_chain_root(expr: &Expr) -> Option<&str> {
    match expr.unwrap_parens() {
        Expr::Ident(ident) => Some(ident.sym.as_ref()),
        Expr::Member(member) => match &member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => fluent_chain_root(&member.obj),
            MemberProp::Computed(_) => None,
        },
        Expr::Call(call) => match &call.callee {
            Callee::Expr(callee) => fluent_chain_root(callee),
            _ => None,
        },
        Expr::OptChain(opt) => match &*opt.base {
            OptChainBase::Member(member) => match &member.prop {
                MemberProp::Ident(_) | MemberProp::PrivateName(_) => fluent_chain_root(&member.obj),
                MemberProp::Computed(_) => None,
            },
            OptChainBase::Call(opt_call) => fluent_chain_root(&opt_call.callee),
        },
        _ => None,
    }
}
