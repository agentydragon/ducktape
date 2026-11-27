use super::*;
use swc_common::SyntaxContext;

fn id(sym: &str) -> Id {
    (Atom::from(sym), SyntaxContext::empty())
}

fn intent(scope: RenameScope, from: &str, to: &str, origin: RenameOrigin) -> RenameIntent {
    RenameIntent {
        scope,
        from: id(from),
        to: Atom::from(to),
        origin,
    }
}

fn names(names: &[&str]) -> BTreeSet<String> {
    names.iter().map(|n| n.to_string()).collect()
}

fn body_occupancy(scope: RenameScope, root: &[&str], nested: &[&str]) -> SealValidation {
    captured_occupancy(scope, root, nested, &[])
}

fn captured_occupancy(
    scope: RenameScope,
    root: &[&str],
    nested: &[&str],
    captured: &[(&str, &str)],
) -> SealValidation {
    SealValidation {
        occupancy: BTreeMap::from([(
            scope,
            ScopeOccupancy::Body {
                label: "spec_x".to_string(),
                root: names(root),
                nested: names(nested),
                captured: captured
                    .iter()
                    .map(|(from, to)| (from.to_string(), to.to_string()))
                    .collect(),
            },
        )]),
        reserved: BTreeSet::new(),
    }
}

fn seal_error(ledger: RenameLedger, validation: &SealValidation) -> SealError {
    ledger.seal(validation).unwrap_err()
}

const A: RenameOrigin = RenameOrigin::Explicit {
    contributor: "contributor_a",
};
const B: RenameOrigin = RenameOrigin::Explicit {
    contributor: "contributor_b",
};
const HEURISTIC: RenameOrigin = RenameOrigin::Heuristic {
    contributor: "scope-local heuristic naturalizer",
};
const FREE: RenameOrigin = RenameOrigin::Heuristic {
    contributor: "return-object alias (free source)",
};
const MINT: RenameOrigin = RenameOrigin::ImportInduced {
    contributor: "entry import-local disambiguation",
};

#[test]
fn reports_every_conflict_in_one_error() {
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(RenameScope::Chunk, "a", "first", A));
    ledger.submit(intent(RenameScope::Chunk, "a", "second", B));
    ledger.submit(intent(RenameScope::Chunk, "b", "third", A));
    ledger.submit(intent(RenameScope::Chunk, "b", "fourth", B));
    let conflict = |from: &str, sides: [(&str, RenameOrigin); 2]| IntentConflict {
        scope: RenameScope::Chunk,
        from: from.to_string(),
        sides: sides
            .into_iter()
            .map(|(to, origin)| ConflictSide {
                to: to.to_string(),
                origins: BTreeSet::from([origin]),
            })
            .collect(),
    };
    let error = seal_error(ledger, &SealValidation::default());
    assert_eq!(
        error,
        SealError::ConflictingIntents(vec![
            conflict("a", [("first", A), ("second", B)]),
            conflict("b", [("fourth", B), ("third", A)]),
        ]),
    );
    // The rendered diagnostic names every target and both contributors.
    let message = error.to_string();
    for identifier in [
        "first",
        "second",
        "third",
        "fourth",
        "contributor_a",
        "contributor_b",
    ] {
        assert!(message.contains(identifier), "{identifier}: {message}");
    }
}

#[test]
fn identical_duplicate_intents_collapse() {
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(RenameScope::Chunk, "a", "readable", A));
    ledger.submit(intent(RenameScope::Chunk, "a", "readable", B));
    let sealed = ledger.seal(&SealValidation::default()).unwrap();
    assert_eq!(
        sealed.chunk_renames_by_name(),
        HashMap::from([("a".to_string(), "readable".to_string())]),
    );
}

#[test]
fn cross_scope_intents_do_not_leak_or_conflict() {
    let function_scope = RenameScope::Function(FunctionScopeId { lo: 10, hi: 20 });
    let module_scope = RenameScope::Module(ModuleId(0));
    let mut ledger = RenameLedger::default();
    // Same `from`, different targets — fine across scopes.
    ledger.submit(intent(
        function_scope,
        "e",
        "value",
        RenameOrigin::Heuristic {
            contributor: "destructure_unpack",
        },
    ));
    ledger.submit(intent(module_scope, "e", "registry", A));
    let sealed = ledger.seal(&SealValidation::default()).unwrap();
    assert_eq!(
        sealed.module_renames_by_name(ModuleId(0)),
        BTreeMap::from([("e".to_string(), "registry".to_string())]),
    );
    assert!(sealed.chunk_renames_by_name().is_empty());
    assert_eq!(
        sealed.scope_renames(&function_scope),
        Some(BTreeMap::from([(id("e"), Atom::from("value"))])),
    );
    assert_eq!(sealed.scope_renames(&RenameScope::Chunk), None);
}

#[test]
fn hygiene_distinct_ids_are_distinct_keys() {
    // Same sym, same scope, same priority, different targets: only the
    // SyntaxContext keeps these two from being a conflict.
    let scope = RenameScope::Module(ModuleId(0));
    let shadowing = (Atom::from("a"), SyntaxContext::from_u32(7));
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(scope, "a", "first", A));
    ledger.submit(RenameIntent {
        scope,
        from: shadowing.clone(),
        to: Atom::from("second"),
        origin: B,
    });
    let sealed = ledger.seal(&SealValidation::default()).unwrap();
    assert_eq!(
        sealed.scope_renames(&scope),
        Some(BTreeMap::from([
            (id("a"), Atom::from("first")),
            (shadowing, Atom::from("second")),
        ])),
    );
}

#[test]
fn mint_returns_base_when_untaken_and_suffixes_past_collisions() {
    let mut ledger = RenameLedger::default();
    ledger.seed_taken(RenameScope::Chunk, ["taken".to_string()]);
    assert_eq!(ledger.mint(RenameScope::Chunk, "fresh"), "fresh");
    // The minted name is claimed: a second request suffixes.
    assert_eq!(ledger.mint(RenameScope::Chunk, "fresh"), "fresh$1");
    assert_eq!(ledger.mint(RenameScope::Chunk, "fresh"), "fresh$2");
    assert_eq!(ledger.mint(RenameScope::Chunk, "taken"), "taken$1");
}

#[test]
fn mint_taken_sets_are_per_scope() {
    let mut ledger = RenameLedger::default();
    ledger.seed_taken(RenameScope::Chunk, ["name".to_string()]);
    assert_eq!(ledger.mint(RenameScope::Chunk, "name"), "name$1");
    assert_eq!(
        ledger.mint(RenameScope::Module(ModuleId(0)), "name"),
        "name",
    );
}

#[test]
fn claim_reserves_a_name_for_later_mints() {
    let mut ledger = RenameLedger::default();
    assert!(ledger.claim(RenameScope::Chunk, "readable"));
    assert!(!ledger.claim(RenameScope::Chunk, "readable"));
    assert_eq!(ledger.mint(RenameScope::Chunk, "readable"), "readable$1");
}

#[test]
fn chunk_explicit_target_colliding_with_root_binding_is_a_hard_error() {
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(RenameScope::Chunk, "a", "delta", A));
    assert_eq!(
        seal_error(
            ledger,
            &body_occupancy(RenameScope::Chunk, &["a", "delta"], &[]),
        ),
        SealError::InvalidChunkRenames(vec![ChunkRenameViolation::CollidesWithLocal {
            from: "a".to_string(),
            to: "delta".to_string(),
        }]),
    );
}

#[test]
fn chunk_explicit_violations_surface_together() {
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(RenameScope::Chunk, "alpha", "1-bad-ident", A));
    ledger.submit(intent(RenameScope::Chunk, "bravo", "delta", A));
    ledger.submit(intent(RenameScope::Chunk, "charlie", "shared", A));
    ledger.submit(intent(RenameScope::Chunk, "delta", "shared", A));
    // One violation per failing rename: `alpha`'s target is no identifier,
    // `bravo`'s repeats `delta`, `delta`'s collides with the target
    // `charlie` took, and `charlie` itself is accepted.
    assert_eq!(
        seal_error(
            ledger,
            &body_occupancy(
                RenameScope::Chunk,
                &["alpha", "bravo", "charlie", "delta"],
                &[],
            ),
        ),
        SealError::InvalidChunkRenames(vec![
            ChunkRenameViolation::NotAnIdentifier {
                from: "alpha".to_string(),
                to: "1-bad-ident".to_string(),
            },
            ChunkRenameViolation::DuplicatesTarget {
                from: "bravo".to_string(),
                to: "delta".to_string(),
            },
            ChunkRenameViolation::CollidesWithLocal {
                from: "delta".to_string(),
                to: "shared".to_string(),
            },
        ]),
    );
}

#[test]
fn chunk_chain_rename_onto_vacated_name_is_rejected() {
    // Chain renames a→b, b→c at Chunk scope: `b`'s vacated root slot
    // routes the violation past the "collides" branch, but the
    // growing occupied set (which holds every root name) still
    // reports it — the pre-ledger loop's exact semantics.
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(RenameScope::Chunk, "a", "b", A));
    ledger.submit(intent(RenameScope::Chunk, "b", "c", A));
    assert_eq!(
        seal_error(
            ledger,
            &body_occupancy(RenameScope::Chunk, &["a", "b"], &[]),
        ),
        SealError::InvalidChunkRenames(vec![ChunkRenameViolation::DuplicatesTarget {
            from: "a".to_string(),
            to: "b".to_string(),
        }]),
    );
}

#[test]
fn module_swap_renames_are_allowed_via_vacating() {
    // Module-scope explicit renames may swap two bindings' names:
    // each target's root slot is vacated by the other rename, and
    // module-style validation has no duplicate-against-root rule.
    let module = RenameScope::Module(ModuleId(0));
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(module, "a", "b", A));
    ledger.submit(intent(module, "b", "a", A));
    let sealed = ledger
        .seal(&body_occupancy(module, &["a", "b"], &[]))
        .unwrap();
    assert_eq!(
        sealed.module_renames_by_name(ModuleId(0)),
        BTreeMap::from([
            ("a".to_string(), "b".to_string()),
            ("b".to_string(), "a".to_string()),
        ]),
    );
}

#[test]
fn module_explicit_target_colliding_with_root_binding_is_a_hard_error() {
    let module = RenameScope::Module(ModuleId(0));
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(module, "a", "readable", A));
    assert_eq!(
        seal_error(ledger, &body_occupancy(module, &["a", "readable"], &[])),
        SealError::InvalidModuleRenames {
            label: "spec_x".to_string(),
            violations: vec![ModuleRenameViolation::CollidesWithBinding {
                from: "a".to_string(),
                to: "readable".to_string(),
            }],
        },
    );
}

#[test]
fn minted_target_colliding_with_occupancy_is_rejected() {
    // Mints come from the ledger's own taken set; a collision means
    // the caller seeded the wrong occupancy — an internal bug, not a
    // spec error.
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(RenameScope::Chunk, "x", "x$1", MINT));
    assert_eq!(
        seal_error(
            ledger,
            &body_occupancy(RenameScope::Chunk, &["x", "x$1"], &[]),
        ),
        SealError::MintedTargetOccupied {
            scope: RenameScope::Chunk,
            from: "x".to_string(),
            to: "x$1".to_string(),
        },
    );
}

#[test]
fn captured_explicit_renames_are_refused_and_captured_heuristics_dropped() {
    let module = RenameScope::Module(ModuleId(0));
    let pair = BTreeSet::from([("a".to_string(), "b".to_string())]);

    let mut ledger = RenameLedger::default();
    ledger.submit(intent(RenameScope::Chunk, "a", "b", A));
    assert_eq!(
        seal_error(
            ledger,
            &captured_occupancy(RenameScope::Chunk, &["a"], &["b"], &[("a", "b")]),
        ),
        SealError::ChunkRenamesCaptured {
            chunk: "spec_x".to_string(),
            captured: pair.clone(),
        },
    );

    let mut ledger = RenameLedger::default();
    ledger.submit(intent(module, "a", "b", A));
    assert_eq!(
        seal_error(
            ledger,
            &captured_occupancy(module, &["a"], &["b"], &[("a", "b")]),
        ),
        SealError::ModuleRenamesCaptured {
            label: "spec_x".to_string(),
            captured: pair,
        },
    );

    let mut ledger = RenameLedger::default();
    ledger.submit(intent(module, "a", "b", HEURISTIC));
    let sealed = ledger
        .seal(&captured_occupancy(module, &["a"], &["b"], &[("a", "b")]))
        .unwrap();
    assert!(sealed.module_renames_by_name(ModuleId(0)).is_empty());
}

#[test]
fn module_heuristic_targeting_an_explicit_target_is_dropped() {
    let module = RenameScope::Module(ModuleId(0));
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(module, "a", "winner", A));
    ledger.submit(intent(module, "b", "winner", FREE));
    let sealed = ledger.seal(&body_occupancy(module, &["a"], &[])).unwrap();
    assert_eq!(
        sealed.module_renames_by_name(ModuleId(0)),
        BTreeMap::from([("a".to_string(), "winner".to_string())]),
    );
    assert_eq!(
        sealed.module_explicit_renames_by_name(ModuleId(0)),
        BTreeMap::from([("a".to_string(), "winner".to_string())]),
    );
}

#[test]
fn module_explicit_query_excludes_heuristic_survivors() {
    let module = RenameScope::Module(ModuleId(0));
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(module, "a", "plan_name", A));
    ledger.submit(intent(module, "t", "options", FREE));
    let sealed = ledger.seal(&body_occupancy(module, &["a"], &[])).unwrap();
    assert_eq!(
        sealed.module_renames_by_name(ModuleId(0)),
        BTreeMap::from([
            ("a".to_string(), "plan_name".to_string()),
            ("t".to_string(), "options".to_string()),
        ]),
    );
    assert_eq!(
        sealed.module_explicit_renames_by_name(ModuleId(0)),
        BTreeMap::from([("a".to_string(), "plan_name".to_string())]),
    );
}

fn function_validation(
    scope: RenameScope,
    bound: &[&str],
    mentions: &[&str],
    reserved: &[&str],
) -> SealValidation {
    SealValidation {
        occupancy: BTreeMap::from([(
            scope,
            ScopeOccupancy::Subtree {
                bound: names(bound),
                mentions: names(mentions),
            },
        )]),
        reserved: names(reserved),
    }
}

#[test]
fn bound_source_heuristic_dropped_when_target_mentioned_in_subtree() {
    let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(scope, "e", "value", HEURISTIC));
    let sealed = ledger
        .seal(&function_validation(scope, &["e"], &["e", "value"], &[]))
        .unwrap();
    assert!(sealed.scope_renames_by_name(&scope).is_empty());
}

#[test]
fn bound_source_heuristic_dropped_when_target_reserved_module_wide() {
    let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(scope, "e", "options", HEURISTIC));
    let sealed = ledger
        .seal(&function_validation(scope, &["e"], &["e"], &["options"]))
        .unwrap();
    assert!(sealed.scope_renames_by_name(&scope).is_empty());
}

#[test]
fn bound_source_heuristics_sharing_a_target_are_both_dropped() {
    let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(scope, "e", "value", HEURISTIC));
    ledger.submit(intent(scope, "n", "value", HEURISTIC));
    ledger.submit(intent(scope, "k", "kept", HEURISTIC));
    let sealed = ledger
        .seal(&function_validation(
            scope,
            &["e", "n", "k"],
            &["e", "n", "k"],
            &[],
        ))
        .unwrap();
    assert_eq!(
        sealed.scope_renames_by_name(&scope),
        BTreeMap::from([("k".to_string(), "kept".to_string())]),
    );
}

#[test]
fn bound_source_heuristic_dropped_when_target_is_another_source() {
    let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(scope, "e", "n", HEURISTIC));
    ledger.submit(intent(scope, "n", "fresh", HEURISTIC));
    let sealed = ledger
        .seal(&function_validation(scope, &["e", "n"], &["e", "n"], &[]))
        .unwrap();
    assert_eq!(
        sealed.scope_renames_by_name(&scope),
        BTreeMap::from([("n".to_string(), "fresh".to_string())]),
    );
}

#[test]
fn free_alias_copy_dropped_when_target_bound_in_subtree() {
    // Free-source aliases (source NOT bound in the subtree) are
    // exempt from the mentions rule but must not target a name the
    // subtree binds.
    let module = RenameScope::Module(ModuleId(0));
    let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(module, "t", "options", FREE));
    ledger.submit(intent(scope, "t", "options", FREE));
    let mut validation = function_validation(scope, &["options"], &["t", "options"], &[]);
    validation.occupancy.insert(
        module,
        ScopeOccupancy::Body {
            label: "spec_x".to_string(),
            root: BTreeSet::new(),
            nested: BTreeSet::new(),
            captured: BTreeSet::new(),
        },
    );
    let sealed = ledger.seal(&validation).unwrap();
    // The Module-scope intent survives (merged-map bridge); the
    // Function-scope application copy is suppressed.
    assert_eq!(
        sealed.module_renames_by_name(ModuleId(0)),
        BTreeMap::from([("t".to_string(), "options".to_string())]),
    );
    assert!(sealed.scope_renames_by_name(&scope).is_empty());
}

#[test]
fn free_alias_copy_survives_when_target_only_mentioned() {
    let module = RenameScope::Module(ModuleId(0));
    let scope = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(module, "t", "options", FREE));
    ledger.submit(intent(scope, "t", "options", FREE));
    let mut validation = function_validation(scope, &[], &["t", "options"], &[]);
    validation.occupancy.insert(
        module,
        ScopeOccupancy::Body {
            label: "spec_x".to_string(),
            root: BTreeSet::new(),
            nested: BTreeSet::new(),
            captured: BTreeSet::new(),
        },
    );
    let sealed = ledger.seal(&validation).unwrap();
    assert_eq!(
        sealed.scope_renames_by_name(&scope),
        BTreeMap::from([("t".to_string(), "options".to_string())]),
    );
}

#[test]
fn free_alias_copy_dropped_when_module_intent_lost_collision() {
    // Two deriving functions free-alias different sources onto one
    // target: both Module-scope intents drop (target collision), so
    // both Function-scope application copies must drop too.
    let module = RenameScope::Module(ModuleId(0));
    let f1 = RenameScope::Function(FunctionScopeId { lo: 1, hi: 9 });
    let mut ledger = RenameLedger::default();
    ledger.submit(intent(module, "t", "options", FREE));
    ledger.submit(intent(module, "u", "options", FREE));
    ledger.submit(intent(f1, "t", "options", FREE));
    let mut validation = function_validation(f1, &[], &["t"], &[]);
    validation.occupancy.insert(
        module,
        ScopeOccupancy::Body {
            label: "spec_x".to_string(),
            root: BTreeSet::new(),
            nested: BTreeSet::new(),
            captured: BTreeSet::new(),
        },
    );
    let sealed = ledger.seal(&validation).unwrap();
    assert!(sealed.module_renames_by_name(ModuleId(0)).is_empty());
    assert!(sealed.scope_renames_by_name(&f1).is_empty());
}
