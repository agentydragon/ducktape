//! Single-statement match entry points and their sound prefilters.

use std::collections::{BTreeMap, BTreeSet};

use chunk_facts::ChunkFacts;

use crate::bindings::Bindings;
use crate::holes::is_single_node_hole;
use crate::homo::homo;
use crate::index::Index;
use crate::list_align::init_node_of;
use crate::predicates::{descendant_string_pattern, regex_predicate_pattern};
use crate::validate::unsupported_needle_construct;
use crate::{Mode, Unsupported};

/// True iff the needle (one top-level statement) structurally matches the
/// subject statement under `mode`, with single-node and run holes as described
/// in the module docs. Errors (fail-closed) on un-lowered constructs (the regex
/// predicate, a misplaced run hole) rather than returning a weaker match. Both
/// inputs are single-statement `ChunkFacts`; `free` is the needle's free
/// identifiers (`source_match::free_identifiers`).
pub fn matches(
    needle: &ChunkFacts,
    subject: &ChunkFacts,
    mode: Mode,
    free: &BTreeSet<String>,
) -> Result<bool, Unsupported> {
    Ok(matches_indexed(&Index::build(needle), &Index::build(subject), mode, free)?.is_some())
}

/// Like [`matches`], but over **prebuilt** indices, so a caller resolving many
/// needles against one chunk body builds each subject `Index` once (cached in
/// `ChunkResolver`) rather than per `(needle, subject)` pair. A match returns its
/// free identifiers' bindings.
pub fn matches_indexed(
    needle: &Index,
    subject: &Index,
    mode: Mode,
    free: &BTreeSet<String>,
) -> Result<Option<BTreeMap<String, String>>, Unsupported> {
    if let Some(reason) = unsupported_needle_construct(needle) {
        return Err(Unsupported { reason });
    }
    Ok(matches_prepared(needle, subject, mode, free))
}

/// Like [`matches_indexed`], but **without** re-running the needle-only
/// [`unsupported_needle_construct`] faithful-subset check — the caller must have
/// already probed this exact needle (once, before the candidate loop). On the
/// corpus-wide resolve hot path the same needle is matched against every candidate
/// statement, so re-validating it per candidate is `O(selectors × candidates)`
/// redundant work (the dominant self-cost in the fact-resolver profile). Skipping
/// it once the needle is known-supported is behavior-preserving — the check is a
/// pure function of the needle, identical across subjects — and makes the result
/// infallible.
pub fn matches_prepared(
    needle: &Index,
    subject: &Index,
    mode: Mode,
    free: &BTreeSet<String>,
) -> Option<BTreeMap<String, String>> {
    let (&n_root, &s_root) = (needle.roots.first()?, subject.roots.first()?);
    let mut bindings = Bindings::new(free);
    // Probed-supported needle: a sub-node descent of an already-probed needle never
    // newly errors (see [`nodes_match`]), so the match is infallible here.
    homo(needle, n_root, subject, s_root, mode, &mut bindings)
        .expect("needle construct already probed as supported")
        .then(|| bindings.free_bindings())
}

/// A **sound** per-candidate prefilter for the single-statement `matches` scan:
/// the root node kind a subject must share for `matches` to possibly return true.
/// `Some(kind)` ⟹ every subject whose root kind differs is a guaranteed
/// `Ok(false)` (it is exactly the `nkind != subject kind` gate in [`homo`], which
/// runs after the hole/predicate special-cases), so the caller may skip it
/// without changing any verdict. `None` ⟹ no prefilter applies — the needle root
/// is a single-node hole or the regex predicate, which match subjects of *other*
/// kinds — so the caller must run the full match.
pub fn needle_root_kind_prefilter(needle: &ChunkFacts) -> Option<&'static str> {
    let index = Index::build(needle);
    let &root = index.roots.first()?;
    if is_single_node_hole(&index, root)
        || regex_predicate_pattern(&index, root).is_some()
        || descendant_string_pattern(&index, root).is_some()
    {
        return None;
    }
    subject_root_kind(needle)
}

/// The root (top-level) node kind of a single-statement `ChunkFacts`, for caching
/// subject root kinds against [`needle_root_kind_prefilter`]. `node_kind` is dense
/// in node id by construction, so this is an O(1) lookup of the `top_level` node.
pub fn subject_root_kind(facts: &ChunkFacts) -> Option<&'static str> {
    let (root_id, _) = facts.top_level.first()?;
    facts
        .node_kind
        .get(*root_id as usize)
        .map(|(_, kind)| kind.as_tag())
}

/// The init-expression node kind of a single-declarator var-decl statement's
/// first declarator (the `VarDeclarator` child at ordinal 1), or `None` when
/// there is no init. A **sound secondary prefilter** for the var-decl member
/// scan: a subject declarator can only match the needle declarator if their init
/// kinds agree (the root-kind prefilter does not discriminate — every var-decl
/// shares kind `VarDecl`). `None` returns fall through to the full match.
pub fn var_declarator_init_kind(facts: &ChunkFacts) -> Option<&'static str> {
    let index = Index::build(facts);
    init_node_of(&index).map(|init| index.kind_of(init).as_tag())
}

/// The subject init kind a needle declarator requires, or `None` when it
/// constrains nothing (a single-node `EXPR`/`ANYTHING` init hole matches any
/// init). A `STR_LITERAL_MATCHING_RE(...)` predicate is a `Call` in the needle,
/// but [`homo`] matches it only against a `StrLit` subject — so the kind it
/// *requires* is `StrLit`, not its own `Call` kind. Returning that (rather than
/// `None`) keeps the prefilter both sound and selective: the CSS-module
/// class-name members (`const x = STR_LITERAL_MATCHING_RE(…)`, the bulk of the
/// per-chunk cost) scan only string-literal declarators, not every declarator.
pub fn needle_var_declarator_init_kind_prefilter(needle: &ChunkFacts) -> Option<&'static str> {
    let index = Index::build(needle);
    let init = init_node_of(&index)?;
    if is_single_node_hole(&index, init) {
        return None;
    }
    if regex_predicate_pattern(&index, init).is_some() {
        return Some("StrLit");
    }
    if descendant_string_pattern(&index, init).is_some() {
        return None;
    }
    Some(index.kind_of(init).as_tag())
}
