//! The selector-resolution seam: one interface, swappable matchers.
//!
//! Today's hand-rolled JS↔JS template matcher (`AstWildcardMatcher`, reached
//! through the `binding_resolution` free functions) and the future Datalog
//! matcher both answer the same question: given a parsed chunk and a JS-template
//! selector, which top-level statement does it claim and what binding(s) does it
//! resolve to? This module names that contract as a trait so either matcher can
//! be slotted in, and provides [`DifferentialResolver`] to run two of them in
//! parallel during the migration — trusting the primary while recording every
//! divergence. See `plans/selector_constraint_model.md` ("Landing the Datalog
//! matcher").
//!
//! The output granularity is deliberately coarse: a member resolves to its
//! `ResolvedMemberBinding` (the claimed binding + kind), an anonymous selector to
//! the matched top-level body-index groups. Internal hole substitutions never
//! cross this boundary — they only constrain the match — so two matchers agree
//! iff they claim the same owners, which is exactly what a differential run
//! compares.

use super::*;

/// Resolve JS-template selectors against a parsed chunk. The two failure modes
/// of every method are no-match and ambiguous (more than one claim); a `Result`
/// `Ok` is the unique resolution.
pub trait SelectorResolver {
    /// Resolve a single-member `source_match` selector to its claimed binding.
    fn resolve_member(
        &self,
        module: &Module,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<ResolvedMemberBinding>;

    /// Resolve a binding-group `source_match` selector to its per-target
    /// bindings (selector-local target binding → matched binding).
    fn resolve_member_group(
        &self,
        module: &Module,
        request_id: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<ResolvedMemberBindingGroup>;

    /// Resolve an anonymous-statement selector to the matched top-level
    /// body-index groups (one inner vec per matched alignment).
    fn resolve_anonymous_groups(
        &self,
        module: &Module,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<Vec<usize>>>;
}

/// Today's production matcher — the hand-rolled `AstWildcardMatcher`, exposed
/// through the seam by delegating to the `binding_resolution` free functions.
pub struct AstWildcardResolver;

impl SelectorResolver for AstWildcardResolver {
    fn resolve_member(
        &self,
        module: &Module,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<ResolvedMemberBinding> {
        resolve_member_binding(module, request_id, export_name, selector)
    }

    fn resolve_member_group(
        &self,
        module: &Module,
        request_id: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<ResolvedMemberBindingGroup> {
        resolve_member_binding_group_match(module, request_id, selector, exports_by_target)
    }

    fn resolve_anonymous_groups(
        &self,
        module: &Module,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<Vec<usize>>> {
        find_anonymous_statement_body_index_groups(module, request_id, selector)
    }
}

/// A resolution outcome rendered for a divergence record. `Resolved` carries the
/// debug-rendered claim; `Rejected` carries the matcher's error text (no-match
/// or ambiguous). Two matchers agree iff both `Resolved` and equal, or both
/// `Rejected` (the rejection text may differ — only the verdict is compared).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResolverOutcome {
    Resolved(String),
    Rejected(String),
}

impl ResolverOutcome {
    fn of<T: std::fmt::Debug>(result: &Result<T>) -> Self {
        match result {
            Ok(value) => Self::Resolved(format!("{value:?}")),
            Err(error) => Self::Rejected(format!("{error}")),
        }
    }
}

/// Which resolution call a divergence came from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResolverSite {
    Member { export_name: String },
    MemberGroup,
    AnonymousStatements,
}

/// One selector where the primary and shadow matchers disagreed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolverDisagreement {
    pub request_id: String,
    pub site: ResolverSite,
    pub primary: ResolverOutcome,
    pub shadow: ResolverOutcome,
}

/// Where a [`DifferentialResolver`] reports divergences. Production wires a
/// logging/metrics sink; the shadow equivalence gate collects them to assert
/// emptiness over a corpus. Takes `&self` so a shared sink survives the
/// differential's `&self` resolution calls (impls add interior mutability).
pub trait DisagreementSink {
    fn record(&self, disagreement: ResolverDisagreement);
}

/// Runs two matchers over every selector, returns the **primary's** result, and
/// reports any divergence to `sink`. This is the parallel-run harness the
/// migration rides on: the shadow never affects the answer, so it can be flipped
/// on in production safely, and the gate fails only when the two disagree.
pub struct DifferentialResolver<'sink, P, S> {
    pub primary: P,
    pub shadow: S,
    pub sink: &'sink dyn DisagreementSink,
}

impl<P, S> DifferentialResolver<'_, P, S> {
    fn check<T: PartialEq + std::fmt::Debug>(
        &self,
        request_id: &str,
        site: ResolverSite,
        primary: Result<T>,
        shadow: Result<T>,
    ) -> Result<T> {
        if !outcomes_agree(&primary, &shadow) {
            self.sink.record(ResolverDisagreement {
                request_id: request_id.to_string(),
                site,
                primary: ResolverOutcome::of(&primary),
                shadow: ResolverOutcome::of(&shadow),
            });
        }
        primary
    }
}

fn outcomes_agree<T: PartialEq>(primary: &Result<T>, shadow: &Result<T>) -> bool {
    match (primary, shadow) {
        (Ok(primary), Ok(shadow)) => primary == shadow,
        (Err(_), Err(_)) => true,
        _ => false,
    }
}

impl<P: SelectorResolver, S: SelectorResolver> SelectorResolver for DifferentialResolver<'_, P, S> {
    fn resolve_member(
        &self,
        module: &Module,
        request_id: &str,
        export_name: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<ResolvedMemberBinding> {
        let primary = self
            .primary
            .resolve_member(module, request_id, export_name, selector);
        let shadow = self
            .shadow
            .resolve_member(module, request_id, export_name, selector);
        self.check(
            request_id,
            ResolverSite::Member {
                export_name: export_name.to_string(),
            },
            primary,
            shadow,
        )
    }

    fn resolve_member_group(
        &self,
        module: &Module,
        request_id: &str,
        selector: &AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Result<ResolvedMemberBindingGroup> {
        let primary =
            self.primary
                .resolve_member_group(module, request_id, selector, exports_by_target);
        let shadow =
            self.shadow
                .resolve_member_group(module, request_id, selector, exports_by_target);
        self.check(request_id, ResolverSite::MemberGroup, primary, shadow)
    }

    fn resolve_anonymous_groups(
        &self,
        module: &Module,
        request_id: &str,
        selector: &AnonymousStatementSelector,
    ) -> Result<Vec<Vec<usize>>> {
        let primary = self
            .primary
            .resolve_anonymous_groups(module, request_id, selector);
        let shadow = self
            .shadow
            .resolve_anonymous_groups(module, request_id, selector);
        self.check(
            request_id,
            ResolverSite::AnonymousStatements,
            primary,
            shadow,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    #[derive(Default)]
    struct CollectingSink(RefCell<Vec<ResolverDisagreement>>);

    impl DisagreementSink for CollectingSink {
        fn record(&self, disagreement: ResolverDisagreement) {
            self.0.borrow_mut().push(disagreement);
        }
    }

    /// A resolver that rejects everything — stands in for a shadow matcher that
    /// disagrees with the production one, so the differential's divergence path
    /// is exercised without a second real matcher.
    struct RejectingResolver;

    impl SelectorResolver for RejectingResolver {
        fn resolve_member(
            &self,
            _module: &Module,
            _request_id: &str,
            _export_name: &str,
            _selector: &AnonymousStatementSelector,
        ) -> Result<ResolvedMemberBinding> {
            bail!("rejecting resolver: no match")
        }
        fn resolve_member_group(
            &self,
            _module: &Module,
            _request_id: &str,
            _selector: &AnonymousStatementSelector,
            _exports_by_target: &BTreeMap<String, String>,
        ) -> Result<ResolvedMemberBindingGroup> {
            bail!("rejecting resolver: no match")
        }
        fn resolve_anonymous_groups(
            &self,
            _module: &Module,
            _request_id: &str,
            _selector: &AnonymousStatementSelector,
        ) -> Result<Vec<Vec<usize>>> {
            bail!("rejecting resolver: no match")
        }
    }

    fn member_selector(match_source: &str) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: None,
            target_statement: None,
            target_statements: None,
            wildcard_string_literals: BTreeSet::new(),
        }
    }

    fn two_const_module() -> Module {
        js_ast::parse_js_module_ast("<test>", "const beta = 2;\nconst gamma = 3;\n").unwrap()
    }

    #[test]
    fn ast_resolver_resolves_member_through_the_seam() {
        js_ast::with_swc_globals(|| {
            let module = two_const_module();
            let selector = member_selector("const readable = 2;");
            let resolved = AstWildcardResolver
                .resolve_member(&module, "test", "Beta", &selector)
                .expect("selector should match exactly one const");
            assert_eq!(resolved.binding_name, "beta");
        });
    }

    #[test]
    fn differential_is_silent_when_matchers_agree() {
        js_ast::with_swc_globals(|| {
            let module = two_const_module();
            let selector = member_selector("const readable = 2;");
            let sink = CollectingSink::default();
            let differential = DifferentialResolver {
                primary: AstWildcardResolver,
                shadow: AstWildcardResolver,
                sink: &sink,
            };
            let resolved = differential
                .resolve_member(&module, "test", "Beta", &selector)
                .expect("primary resolves");
            assert_eq!(resolved.binding_name, "beta");
            assert!(
                sink.0.borrow().is_empty(),
                "agreeing matchers must record no divergence",
            );
        });
    }

    #[test]
    fn differential_records_divergence_but_returns_primary() {
        js_ast::with_swc_globals(|| {
            let module = two_const_module();
            let selector = member_selector("const readable = 2;");
            let sink = CollectingSink::default();
            let differential = DifferentialResolver {
                primary: AstWildcardResolver,
                shadow: RejectingResolver,
                sink: &sink,
            };
            // The primary's answer is still returned — the shadow never affects
            // the result, only the divergence record.
            let resolved = differential
                .resolve_member(&module, "mod/x", "Beta", &selector)
                .expect("primary resolves even when the shadow rejects");
            assert_eq!(resolved.binding_name, "beta");

            let recorded = sink.0.borrow();
            assert_eq!(recorded.len(), 1, "the disagreement must be recorded");
            assert_eq!(
                recorded[0].site,
                ResolverSite::Member {
                    export_name: "Beta".to_string()
                },
            );
            assert!(matches!(recorded[0].primary, ResolverOutcome::Resolved(_)));
            assert!(matches!(recorded[0].shadow, ResolverOutcome::Rejected(_)));
        });
    }

    #[test]
    fn differential_treats_mutual_rejection_as_agreement() {
        js_ast::with_swc_globals(|| {
            let module = two_const_module();
            // No const initialized to 99, so the real matcher also rejects:
            // both reject ⇒ agreement, nothing recorded.
            let selector = member_selector("const readable = 99;");
            let sink = CollectingSink::default();
            let differential = DifferentialResolver {
                primary: AstWildcardResolver,
                shadow: RejectingResolver,
                sink: &sink,
            };
            assert!(
                differential
                    .resolve_member(&module, "test", "Beta", &selector)
                    .is_err(),
            );
            assert!(
                sink.0.borrow().is_empty(),
                "mutual rejection is agreement, not divergence",
            );
        });
    }
}
