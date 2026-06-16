use super::*;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResolvedMemberBinding {
    pub binding_name: String,
    pub kind: Option<BindingSourceKind>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SourceMatchNearMiss {
    pub body_idx: usize,
    pub declared_bindings: Vec<String>,
    pub score: usize,
    pub reason: String,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SourceMatchBodyDebt {
    pub exact_groups: Vec<Vec<Option<usize>>>,
    pub near_misses: Vec<SourceMatchNearMiss>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct MemberBindingMatch {
    pub body_idx: usize,
    pub binding: ResolvedMemberBinding,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResolvedMemberBindingGroup {
    pub body_idx: usize,
    pub bindings: BTreeMap<String, ResolvedMemberBinding>,
}

/// Expand one `binding_groups[]` entry into `(export_name,
/// member-form selector)` pairs — each selector is the group's
/// `source_match` with `target_binding` set to one selector-local
/// binding. This is the single expansion both the run pipeline's
/// member assembly (`lowering::build_members`) and the CLI edit gate
/// consume, so the two always agree on which owners a binding group
/// claims.
pub struct BindingGroupMemberSelector {
    pub export_name: String,
    pub selector: AnonymousStatementSelector,
    pub comment: Option<String>,
}

/// Restricts which top-level body indices the matcher inspects as match anchors.
///
/// This is a pure performance prefilter, never a correctness gate: a caller may
/// supply a candidate body-index set (e.g. from `SelectorCandidateIndex`) that
/// must be a *sound superset* of every index the full scan would match. The
/// matcher still proves every reported match structurally, so over-inclusion is
/// harmless and under-inclusion would silently drop real matches — the caller is
/// responsible for the superset invariant.
#[derive(Clone, Copy)]
pub enum BodyIndexFilter<'a> {
    All,
    Restricted(&'a BTreeSet<usize>),
}

impl BodyIndexFilter<'_> {
    pub(crate) fn allows(&self, body_idx: usize) -> bool {
        match self {
            BodyIndexFilter::All => true,
            BodyIndexFilter::Restricted(indices) => indices.contains(&body_idx),
        }
    }
}
