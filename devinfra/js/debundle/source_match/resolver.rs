//! The candidate-resolution seam: the `SelectorResolver` trait.
//!
//! A resolver answers, for a parsed chunk and a JS-template selector: which
//! top-level statements does the selector match, and what binding(s) do they
//! declare? `ChunkResolver` (`chunk_resolver`) is the sole implementor and the
//! production candidate generator for shape (`source_match`) selectors —
//! materialization projects its candidates into the selector IR, and the global
//! solve picks one target per selector. See <docs/selector_resolution.md>.
//!
//! The output granularity is deliberately coarse: a member resolves to its
//! `ResolvedMemberBinding` (the claimed binding + kind), an anonymous selector to
//! the matched top-level body-index groups. Internal hole substitutions never
//! cross this boundary — they only constrain the match.

use super::*;

/// Resolve JS-template selectors against **one parsed chunk** the resolver is
/// already bound to: an implementor builds its per-chunk model once (the fact
/// resolver's EDB) and resolves many selectors against it, so a chunk with
/// thousands of selectors pays the per-chunk setup once — not once per selector.
/// The two failure modes of every method are no-match and ambiguous (more than
/// one claim); a `Result` `Ok` is the unique resolution.
pub trait SelectorResolver {
    /// Resolve a single-member `source_match` selector to its claimed binding.
    fn resolve_member(
        &self,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<ResolvedMemberBinding> {
        self.resolve_member_with_label(request_id, export_name, selector, "source_matches[]")
    }

    /// Resolve a member-shaped selector while rendering diagnostics under a
    /// caller-provided spec path. Binding-group diagnostics use this to preserve
    /// the same near-miss detail as member selectors without lying about origin.
    fn resolve_member_with_label(
        &self,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
        selector_label: &'static str,
    ) -> Result<ResolvedMemberBinding>;

    /// Enumerate every candidate a single-member `source_match` selector matches
    /// without collapsing to unique/no-match/ambiguous. The global selector solver
    /// consumes these rows as EDB facts, then performs final claim classification
    /// together with the rest of the selector program.
    fn member_candidates(
        &self,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<MemberBindingMatch>>;

    /// Enumerate every candidate alignment for a binding-group `source_match`
    /// selector without collapsing to unique/no-match/ambiguous. Each candidate
    /// carries per-target body indices so the global selector solver can claim
    /// the actual owner of every exported binding.
    fn member_group_candidates(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<Vec<MemberBindingGroupMatch>>;

    /// Resolve a binding-group `source_match` selector to its per-target
    /// bindings (selector-local target binding → matched binding).
    fn resolve_member_group(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<ResolvedMemberBindingGroup>;

    /// Resolve an anonymous-statement selector to the matched top-level
    /// body-index groups (one per matched alignment).
    fn resolve_anonymous_groups(
        &self,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<AnonymousGroupMatch>>;
}
