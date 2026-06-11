//! Proptest suite for the public `RenameLedger` seal contract
//! (rename pipeline PR 2 surface, #2091): seal determinism under
//! intent permutation and duplication, priority dominance,
//! same-priority conflicts always erroring naming both origins, and
//! per-scope validation isolation.
//!
//! Deliberately written against the public `submit`/`seal`/
//! `SealedRenames` query API only — seal-internal validation is in
//! flux (PR 3 moves occupancy/capture validation into seal), and these
//! properties must survive that change unedited or with mechanical
//! signature updates only. Case counts are bounded for CI (see
//! [`ci_config`]); override locally via
//! `bbr test //devinfra/js/debundle:lowering_test
//! --test_env=PROPTEST_CASES=2000`.

use std::collections::BTreeMap;

use analysis::ModuleId;
use proptest::prelude::*;
use proptest::sample::select;
use proptest::test_runner::TestCaseError;
use swc_atoms::Atom;
use swc_common::SyntaxContext;
use swc_ecma_ast::Id;

use super::rename_ledger::{
    FunctionScopeId, RenameIntent, RenameLedger, RenameOrigin, RenameScope, SealedRenames,
};

/// Source-binding pool, disjoint from [`TARGETS`] so a generated
/// rename never maps a name to itself.
const SOURCES: [&str; 4] = ["a", "b", "c", "d"];
const TARGETS: [&str; 3] = ["n0", "n1", "n2"];
const CONTRIBUTORS: [&str; 3] = ["alpha", "beta", "gamma"];

fn id(sym: &str) -> Id {
    (Atom::from(sym), SyntaxContext::empty())
}

fn origin_at(priority_kind: usize, contributor: &'static str) -> RenameOrigin {
    match priority_kind {
        0 => RenameOrigin::Explicit { contributor },
        1 => RenameOrigin::ImportInduced { contributor },
        _ => RenameOrigin::Heuristic { contributor },
    }
}

fn arb_origin() -> impl Strategy<Value = RenameOrigin> {
    (0..3usize, select(&CONTRIBUTORS[..]))
        .prop_map(|(kind, contributor)| origin_at(kind, contributor))
}

fn arb_scope() -> impl Strategy<Value = RenameScope> {
    prop_oneof![
        Just(RenameScope::Chunk),
        (0..3usize).prop_map(|index| RenameScope::Module(ModuleId::logical(index))),
        prop_oneof![
            Just(FunctionScopeId { lo: 1, hi: 9 }),
            Just(FunctionScopeId { lo: 10, hi: 20 }),
        ]
        .prop_map(RenameScope::Function),
        Just(RenameScope::EntryPublicExports),
    ]
}

fn arb_intent() -> impl Strategy<Value = RenameIntent> {
    (
        arb_scope(),
        select(&SOURCES[..]),
        select(&TARGETS[..]),
        arb_origin(),
    )
        .prop_map(|(scope, from, to, origin)| RenameIntent {
            scope,
            from: id(from),
            to: Atom::from(to),
            origin,
        })
}

/// A pair of distinct indices into `0..n`.
fn distinct_pair(n: usize) -> impl Strategy<Value = (usize, usize)> {
    (0..n, 0..n - 1)
        .prop_map(|(first, second)| (first, if second >= first { second + 1 } else { second }))
}

/// Submit all intents into a fresh ledger and seal. Errors projected
/// to their message so results are comparable.
fn seal_all(intents: Vec<RenameIntent>) -> Result<SealedRenames, String> {
    let mut ledger = RenameLedger::default();
    for intent in intents {
        ledger.submit(intent);
    }
    ledger.seal().map_err(|error| error.to_string())
}

/// Bounded case count for CI; a `PROPTEST_CASES` env override still
/// wins for longer local runs (`ProptestConfig::default()` reads it).
fn ci_config(cases: u32) -> ProptestConfig {
    let mut config = ProptestConfig::default();
    if std::env::var_os("PROPTEST_CASES").is_none() {
        config.cases = cases;
    }
    config
}

proptest! {
    #![proptest_config(ci_config(128))]

    /// Seal output (or the full conflict error) is independent of
    /// intent submission order.
    #[test]
    fn seal_is_deterministic_under_intent_permutation(
        (intents, shuffled) in proptest::collection::vec(arb_intent(), 0..12)
            .prop_flat_map(|intents| {
                let shuffled = Just(intents.clone()).prop_shuffle();
                (Just(intents), shuffled)
            }),
    ) {
        prop_assert_eq!(seal_all(intents), seal_all(shuffled));
    }

    /// Submitting every intent twice seals identically to submitting
    /// it once — identical duplicates collapse.
    #[test]
    fn duplicate_submissions_do_not_change_seal(
        intents in proptest::collection::vec(arb_intent(), 0..10),
    ) {
        let doubled: Vec<RenameIntent> = intents
            .iter()
            .flat_map(|intent| [intent.clone(), intent.clone()])
            .collect();
        prop_assert_eq!(seal_all(intents), seal_all(doubled));
    }

    /// A single explicit intent dominates any set of disagreeing
    /// lower-priority (import-induced / heuristic) intents on the same
    /// `(scope, from)`: seal succeeds and resolves to the explicit
    /// target, regardless of how the lower-priority intents conflict
    /// among themselves at their own priority tiers.
    #[test]
    fn explicit_intent_dominates_lower_priority_disagreement(
        scope in arb_scope(),
        from in select(&SOURCES[..]),
        explicit_to in select(&TARGETS[..]),
        lower in proptest::collection::vec(
            (select(&TARGETS[..]), 1..3usize, select(&CONTRIBUTORS[..])),
            0..6,
        ),
    ) {
        let mut ledger = RenameLedger::default();
        for (to, priority_kind, contributor) in lower {
            ledger.submit(RenameIntent {
                scope,
                from: id(from),
                to: Atom::from(to),
                origin: origin_at(priority_kind, contributor),
            });
        }
        ledger.submit(RenameIntent {
            scope,
            from: id(from),
            to: Atom::from(explicit_to),
            origin: RenameOrigin::Explicit { contributor: "explicit_winner" },
        });
        let sealed = ledger
            .seal()
            .map_err(|error| TestCaseError::fail(error.to_string()))?;
        let expected = BTreeMap::from([(id(from), Atom::from(explicit_to))]);
        prop_assert_eq!(sealed.scope_renames(&scope), Some(&expected));
    }

    /// Two intents at the same priority disagreeing on one
    /// `(scope, from)` target always seal to a hard error whose
    /// message names both origins' contributors and both targets.
    #[test]
    fn same_priority_disagreement_errors_naming_both_origins(
        scope in arb_scope(),
        from in select(&SOURCES[..]),
        priority_kind in 0..3usize,
        (target_a, target_b) in distinct_pair(TARGETS.len()),
        (contributor_a, contributor_b) in distinct_pair(CONTRIBUTORS.len()),
    ) {
        let mut ledger = RenameLedger::default();
        ledger.submit(RenameIntent {
            scope,
            from: id(from),
            to: Atom::from(TARGETS[target_a]),
            origin: origin_at(priority_kind, CONTRIBUTORS[contributor_a]),
        });
        ledger.submit(RenameIntent {
            scope,
            from: id(from),
            to: Atom::from(TARGETS[target_b]),
            origin: origin_at(priority_kind, CONTRIBUTORS[contributor_b]),
        });
        let message = match ledger.seal() {
            Ok(_) => return Err(TestCaseError::fail(
                "same-priority disagreement sealed successfully",
            )),
            Err(error) => error.to_string(),
        };
        for needle in [
            CONTRIBUTORS[contributor_a],
            CONTRIBUTORS[contributor_b],
            TARGETS[target_a],
            TARGETS[target_b],
        ] {
            prop_assert!(
                message.contains(needle),
                "conflict error omits `{}`: {}",
                needle, message,
            );
        }
    }

    /// Scopes validate independently: the combined ledger seals iff
    /// every per-scope subset seals on its own, and a successful
    /// combined seal answers each scope's queries exactly as the
    /// scope's intents sealed in isolation — no cross-scope leakage.
    #[test]
    fn seal_validates_each_scope_independently(
        intents in proptest::collection::vec(arb_intent(), 0..14),
    ) {
        let mut by_scope: BTreeMap<RenameScope, Vec<RenameIntent>> = BTreeMap::new();
        for intent in &intents {
            by_scope.entry(intent.scope).or_default().push(intent.clone());
        }
        let per_scope: BTreeMap<RenameScope, Result<SealedRenames, String>> = by_scope
            .into_iter()
            .map(|(scope, scope_intents)| (scope, seal_all(scope_intents)))
            .collect();
        match seal_all(intents) {
            Ok(combined) => {
                for (scope, result) in &per_scope {
                    let solo = match result {
                        Ok(solo) => solo,
                        Err(error) => return Err(TestCaseError::fail(format!(
                            "combined seal succeeded but {scope} alone fails: {error}",
                        ))),
                    };
                    prop_assert_eq!(combined.scope_renames(scope), solo.scope_renames(scope));
                }
            }
            Err(_) => {
                prop_assert!(
                    per_scope.values().any(|result| result.is_err()),
                    "combined seal failed but every scope seals in isolation",
                );
            }
        }
    }
}
