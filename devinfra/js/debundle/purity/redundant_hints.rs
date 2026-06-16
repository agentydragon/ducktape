use super::*;

/// One author-declared `purity: pure` hint the analyzer determines
/// would be inferred automatically — the binding's body classifies
/// `Pure` (or the binding admits as `PlainData`) even with the hint
/// itself removed from `declared_pure`. Surfaced as a chunk-level
/// warning so spec authors can delete the load-free hint and keep
/// only the genuinely-load-bearing ones (vendor-shape impurity
/// overrides, etc.).
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct RedundantPurityHint {
    pub binding_name: String,
    pub reason: RedundantPurityReason,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RedundantPurityReason {
    /// Chunk-local function/arrow whose body classifies `Pure` by
    /// recursive analysis (with the hint on this binding removed
    /// from `declared_pure`; hints on other bindings still apply).
    InferredPureFunction,
    /// Chunk-local binding that admits as `PlainData`. The
    /// `purity: pure` callsite-override is a no-op because the
    /// binding isn't called as `binding(...)` in any pure-relevant
    /// way that the override would gate.
    InferredPlainDataBinding,
}

/// One redundant `pure_members: [<prop>]` entry — the
/// `<binding>.<prop>(args)` call would already classify pure
/// without the spec hint (e.g. the receiver is `Array` and the
/// property is `isArray`, already covered by `PURE_STATIC_CALLS`).
/// Surfaced so spec authors can prune the redundant entry.
#[derive(Debug, Clone, Eq, PartialEq, Serialize, Deserialize)]
pub struct RedundantPureMemberHint {
    pub binding_name: String,
    pub property: String,
    pub reason: RedundantPureMemberReason,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RedundantPureMemberReason {
    /// `(<binding>, <prop>)` is already in `PURE_STATIC_CALLS` AND
    /// `<binding>` is a whitelist receiver name (e.g. `Array`,
    /// `Number`) — the call classifies pure on its own with no
    /// `pure_members` annotation. The hint is a no-op.
    WhitelistedStaticCall,
}

/// For each name in `declared_pure`, ask "would the analyzer infer
/// this binding as Pure without the hint on itself?" by building a
/// fresh `ChunkCodeGraph` with that one name removed from
/// `declared_pure` (hints on other bindings still apply) and checking
/// the binding's classification. Hints whose answer is Yes are
/// reported as redundant.
///
/// **Semantics — per-hint independent removal, hints on other names
/// kept in place.** Removing only the hint under test catches "the
/// analyzer would have figured this out on its own given the rest
/// of the current spec." When a chain `a → b → c → …` carries
/// hints at multiple points, the per-hint check correctly reports
/// the *transitively redundant* members at the head of the chain
/// (their inference relies on hints further down) and keeps the
/// *load-bearing* members (the ones whose own body is what's
/// genuinely impure). Authors removing hints in successive
/// `/followups` rounds will see previously-redundant hints become
/// load-bearing as the supporting hints are pruned, and the loop
/// terminates when only genuinely impure bodies retain hints.
///
/// **Soundness for the surrounding debundle reshuffle:** the check
/// has no effect on classification of statements — it only emits
/// a warning. Dropping a hint based on the warning is the spec
/// author's decision and re-runs the full analysis next build.
/// The per-hint check itself is a read-only side query.
///
/// Cost: O(|declared_pure| × graph_build_cost). For typical spec
/// hint counts (single digits per chunk) this is negligible
/// compared to the per-chunk analysis itself.
pub(crate) fn detect_redundant_purity_hints(
    body: &[TopLevelItemView<'_>],
    shadowed: &BTreeSet<&'static str>,
    declared_pure: &BTreeSet<String>,
) -> Vec<RedundantPurityHint> {
    let mut out = Vec::new();
    for name in declared_pure {
        let mut without = declared_pure.clone();
        without.remove(name);
        let probe = ChunkCodeGraph::build(body, shadowed, &without);
        let reason = match probe.bindings.get(name) {
            Some(ChunkBinding::Function { purity }) if purity.is_pure() => {
                Some(RedundantPurityReason::InferredPureFunction)
            }
            Some(ChunkBinding::PlainData) => Some(RedundantPurityReason::InferredPlainDataBinding),
            _ => None,
        };
        if let Some(reason) = reason {
            out.push(RedundantPurityHint {
                binding_name: name.clone(),
                reason,
            });
        }
    }
    out
}

/// Walk `declared_pure_members` and flag entries the analyzer would
/// classify pure without the hint. Currently the only auto-pure path
/// for member calls is the `PURE_STATIC_CALLS` whitelist for
/// `(WHITELIST_RECEIVERS, prop)` pairs — so an entry like
/// `pure_members: [isArray]` on a binding named `Array` is a no-op
/// (`Array.isArray(...)` is already in `PURE_STATIC_CALLS`).
///
/// The `PURE_OBJECT_CALLS_ON_PLAIN_DATA` admission rule has
/// per-callsite argument-shape gates — without inspecting every
/// callsite for `<binding>.<prop>(...)` arg shapes, we can't claim
/// the spec hint is a no-op (the hint covers ALL arg shapes, while
/// the whitelist only covers plain-data args). To stay sound under
/// the "report only confirmed-redundant" contract, we don't flag
/// `pure_members: [entries|keys|values|freeze|fromEntries]` on
/// `Object` here. Spec authors can drop them manually once they've
/// verified every callsite uses a plain-data arg.
pub(crate) fn detect_redundant_pure_member_hints(
    declared_pure_members: &BTreeMap<String, BTreeSet<String>>,
) -> Vec<RedundantPureMemberHint> {
    let mut out = Vec::new();
    for (binding, props) in declared_pure_members {
        // Only whitelist-receiver bindings can ride on
        // `PURE_STATIC_CALLS`. A user-named binding (e.g. a vendor
        // namespace `b`) doesn't reach the whitelist regardless of
        // shadowing — so the hint is load-bearing there.
        let recv = WHITELIST_RECEIVERS
            .iter()
            .copied()
            .find(|r| *r == binding.as_str());
        let Some(recv) = recv else {
            continue;
        };
        for prop in props {
            if PURE_STATIC_CALLS
                .iter()
                .any(|(r, p)| *r == recv && *p == prop.as_str())
            {
                out.push(RedundantPureMemberHint {
                    binding_name: binding.clone(),
                    property: prop.clone(),
                    reason: RedundantPureMemberReason::WhitelistedStaticCall,
                });
            }
        }
    }
    out
}
