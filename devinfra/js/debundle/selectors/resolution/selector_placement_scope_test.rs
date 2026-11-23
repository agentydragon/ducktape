use std::collections::BTreeSet;

use selector_outcome::Outcome;
use selector_resolve::{
    AnonymousStatement, Chunk, EntityIndex, FrozenImportAnchor, Member, MemberSelector,
    PlacementAnchors, SpecModule,
};
use source_identity::SourceUnitOrdinal;
use source_match::{ParsedSourceMatchSelector, SelectorField};
use spec::{
    AnonymousStatementSelector, BindingSelector, BindingSourceKind, CrossRefTarget,
    MemberSelectorSpec, SourceMatchIdentifierMode,
};

const SOURCE: &str = r#"const anchor = {x: 1};
const other = {x: 2};
const a = anchor.x;
const b = other.x;
console.log(a, b);
const useFixed = a;
"#;

fn binding_member(export_name: &str, binding: &str) -> Member {
    Member {
        export_name: export_name.to_string(),
        selector: MemberSelector::Authored(MemberSelectorSpec::Binding(BindingSelector {
            name: binding.to_string(),
            kind: None,
        })),
    }
}

fn source_match_member(export_name: &str, binding: &str, match_source: &str) -> Member {
    let selector = AnonymousStatementSelector {
        match_source: match_source.to_string(),
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding: Some(binding.to_string()),
    };
    Member {
        export_name: export_name.to_string(),
        selector: MemberSelector::SourceMatch(
            ParsedSourceMatchSelector::parse(
                "placement-scope-test",
                "<placement-scope-test>".to_string(),
                &selector,
                SelectorField::SourceMatch,
            )
            .expect("test selector parses"),
        ),
    }
}

fn module(path: &str, members: Vec<Member>) -> SpecModule {
    SpecModule {
        path: path.to_string(),
        members,
        anonymous_statements: Vec::new(),
    }
}

fn resolved_body(
    resolution: &selector_resolve::Resolution,
    module_index: usize,
    member_index: usize,
) -> usize {
    match &resolution
        .outcome(module_index, EntityIndex::Member(member_index))
        .expect("member has a resolution outcome")
        .outcome
    {
        Outcome::Resolved { body_index, .. } => *body_index,
        other => panic!("expected a resolved member, got {other:?}"),
    }
}

fn with_chunk<T>(test: impl FnOnce(&Chunk<'_>) -> T) -> T {
    with_source(SOURCE, test)
}

fn with_source<T>(source: &str, test: impl FnOnce(&Chunk<'_>) -> T) -> T {
    js_ast::with_swc_globals(|| {
        let parsed = js_ast::parse_js_module_ast("placement-scope.js", source).unwrap();
        let chunk = Chunk::analyze("main", &parsed);
        test(&chunk)
    })
}

fn frozen_anchors(chunk: &Chunk<'_>, base: &[SpecModule]) -> PlacementAnchors {
    let base_resolution = chunk.resolve(base).unwrap();
    chunk
        .freeze_placement_anchors(base, &base_resolution)
        .unwrap()
}

fn anonymous_statement(index: usize, match_source: &str) -> AnonymousStatement {
    let selector = AnonymousStatementSelector {
        match_source: match_source.to_string(),
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding: None,
    };
    AnonymousStatement {
        index,
        selector: ParsedSourceMatchSelector::parse(
            "placement-scope-test",
            "<placement-scope-test>".to_string(),
            &selector,
            SelectorField::SourceMatch,
        )
        .expect("anonymous selector parses"),
    }
}

fn resolved_base_modules() -> Vec<SpecModule> {
    vec![
        module("anchor-owner", vec![binding_member("anchor", "anchor")]),
        module(
            "fixed-owner",
            vec![source_match_member("FixedA", "a", "const a = anchor.x;")],
        ),
        module(
            "retained-owner",
            vec![source_match_member(
                "Kept",
                "value",
                "const value = ANYTHING.x;",
            )],
        ),
    ]
}

#[test]
fn overlay_uses_frozen_full_resolution_and_only_excludes_occupied_units() {
    with_chunk(|chunk| {
        let base = resolved_base_modules();
        let base_resolution = chunk.resolve(&base).unwrap();
        // Full-base all_different assigns `FixedA` to a and forces `Kept` to b.
        assert_eq!(resolved_body(&base_resolution, 1, 0), 2);
        assert_eq!(resolved_body(&base_resolution, 2, 0), 3);

        let anchors = chunk
            .freeze_placement_anchors(&base, &base_resolution)
            .unwrap();
        let overlay = [module(
            "new-owner",
            vec![source_match_member(
                "Placed",
                "value",
                "const value = FixedA;",
            )],
        )];
        // The anchor and retained `Kept` stay owned. `FixedA` is omitted, so
        // its source unit is available, while its export remains a frozen
        // binding anchor for the overlay's free identifier.
        let occupied = BTreeSet::from([SourceUnitOrdinal(0), SourceUnitOrdinal(3)]);
        let placement = chunk
            .resolve_placement(&overlay, &anchors, &occupied)
            .unwrap();
        assert_eq!(resolved_body(&placement, 0, 0), 5);
    });
}

#[test]
fn occupied_candidate_is_removed_before_selector_uniqueness_is_decided() {
    with_chunk(|chunk| {
        let base = resolved_base_modules();
        let anchors = frozen_anchors(chunk, &base);
        let overlay = [module(
            "new-owner",
            vec![source_match_member(
                "Placed",
                "value",
                "const value = ANYTHING.x;",
            )],
        )];
        // Both `a` and `b` match. With `b` already owned, the placement-only
        // resolver proves the unique remaining candidate `a`.
        let placement = chunk
            .resolve_placement(
                &overlay,
                &anchors,
                &BTreeSet::from([SourceUnitOrdinal(0), SourceUnitOrdinal(3)]),
            )
            .unwrap();
        assert_eq!(resolved_body(&placement, 0, 0), 2);
    });
}

#[test]
fn duplicate_export_names_are_ambiguous_even_when_anchors_share_a_source_unit() {
    with_chunk(|chunk| {
        let base = vec![
            module("first-owner", vec![binding_member("anchor", "anchor")]),
            module("second-owner", vec![binding_member("anchor", "anchor")]),
        ];
        let anchors = frozen_anchors(chunk, &base);
        let overlay = [module(
            "new-owner",
            vec![source_match_member(
                "Placed",
                "value",
                "const value = anchor.x;",
            )],
        )];
        let placement = chunk
            .resolve_placement(&overlay, &anchors, &BTreeSet::new())
            .unwrap();
        assert!(matches!(
            placement
                .outcome(0, EntityIndex::Member(0))
                .unwrap()
                .outcome,
            Outcome::Invalid { .. }
        ));
    });
}

#[test]
fn grouped_source_match_rows_are_removed_as_whole_placements_when_occupied() {
    let source = "const a = 1, b = 2; const c = 1, d = 2; console.log(a, b, c, d);";
    with_source(source, |chunk| {
        let base = [];
        let anchors = frozen_anchors(chunk, &base);
        let overlay = [module(
            "new-owner",
            vec![
                source_match_member("First", "first", "const first = 1, second = 2;"),
                source_match_member("Second", "second", "const first = 1, second = 2;"),
            ],
        )];
        let placement = chunk
            .resolve_placement(&overlay, &anchors, &BTreeSet::from([SourceUnitOrdinal(0)]))
            .unwrap();
        assert_eq!(resolved_body(&placement, 0, 0), 1);
        assert_eq!(resolved_body(&placement, 0, 1), 1);
    });
}

#[test]
fn anonymous_source_match_obeys_occupied_units() {
    let source = "console.log(1); console.log(1);";
    with_source(source, |chunk| {
        let base = [];
        let anchors = frozen_anchors(chunk, &base);
        let overlay = [SpecModule {
            path: "new-owner".to_string(),
            members: Vec::new(),
            anonymous_statements: vec![anonymous_statement(0, "console.log(1);")],
        }];
        let placement = chunk
            .resolve_placement(&overlay, &anchors, &BTreeSet::from([SourceUnitOrdinal(0)]))
            .unwrap();
        assert!(matches!(
            &placement
                .outcome(0, EntityIndex::AnonymousStatement(0))
                .unwrap()
                .outcome,
            Outcome::Resolved { body_index: 1, .. }
        ));
    });
}

#[test]
fn relational_overlay_uses_a_frozen_source_unit_anchor() {
    let source = "const anchor = 1; const first = anchor + 1; const second = anchor + 2; console.log(first, second);";
    with_source(source, |chunk| {
        let base = [module(
            "anchor-owner",
            vec![binding_member("anchor", "anchor")],
        )];
        let anchors = frozen_anchors(chunk, &base);
        let overlay = [module(
            "new-owner",
            vec![Member {
                export_name: "Consumer".to_string(),
                selector: MemberSelector::Authored(MemberSelectorSpec::CrossRef(CrossRefTarget {
                    anchor: "anchor".to_string(),
                    kind: Some(BindingSourceKind::VariableDeclarator),
                })),
            }],
        )];
        let placement = chunk
            .resolve_placement(
                &overlay,
                &anchors,
                &BTreeSet::from([SourceUnitOrdinal(0), SourceUnitOrdinal(1)]),
            )
            .unwrap();
        assert_eq!(resolved_body(&placement, 0, 0), 2);
    });
}

#[test]
fn imported_base_anchor_keeps_binding_identity_without_a_source_unit() {
    let source = "import { value as imported } from './base.js'; const local = imported.x; console.log(local);";
    with_source(source, |chunk| {
        let base = [module("base-owner", Vec::new())];
        let mut anchors = frozen_anchors(chunk, &base);
        chunk
            .add_frozen_import_anchors(
                &mut anchors,
                &[FrozenImportAnchor {
                    logical_module: "base-owner".to_string(),
                    export_name: "Anchor".to_string(),
                    imported_binding: "imported".to_string(),
                }],
            )
            .unwrap();
        let overlay = [module(
            "new-owner",
            vec![source_match_member(
                "Consumer",
                "consumer",
                "const consumer = Anchor.x;",
            )],
        )];
        let placement = chunk
            .resolve_placement(&overlay, &anchors, &BTreeSet::new())
            .unwrap();
        assert_eq!(resolved_body(&placement, 0, 0), 1);
    });
}

#[test]
fn import_anchor_rejects_relational_use_without_a_source_unit() {
    let source = "import { value as imported } from './base.js'; const local = imported.x;";
    with_source(source, |chunk| {
        let base = [module("base-owner", Vec::new())];
        let mut anchors = frozen_anchors(chunk, &base);
        chunk
            .add_frozen_import_anchors(
                &mut anchors,
                &[FrozenImportAnchor {
                    logical_module: "base-owner".to_string(),
                    export_name: "Anchor".to_string(),
                    imported_binding: "imported".to_string(),
                }],
            )
            .unwrap();
        let overlay = [module(
            "new-owner",
            vec![Member {
                export_name: "Consumer".to_string(),
                selector: MemberSelector::Authored(MemberSelectorSpec::CrossRef(CrossRefTarget {
                    anchor: "Anchor".to_string(),
                    kind: Some(BindingSourceKind::VariableDeclarator),
                })),
            }],
        )];
        assert!(
            chunk
                .resolve_placement(&overlay, &anchors, &BTreeSet::new())
                .is_err()
        );
    });
}

#[test]
fn repeated_declaration_anchor_uses_the_resolvers_canonical_first_site() {
    let source = "var anchor = 0; var value = 1; var value = 2; const consumer = value;";
    with_source(source, |chunk| {
        let base = [
            module("anchor-owner", vec![binding_member("anchor", "anchor")]),
            module(
                "repeated-owner",
                vec![source_match_member("Repeated", "target", "var target = 2;")],
            ),
        ];
        let base_resolution = chunk.resolve(&base).unwrap();
        // The matcher finds the later `var value = 2`, but `member_place`
        // uses the first declaration site as the repeated binding's
        // canonical source unit, just as it does for ordinary resolution.
        assert_eq!(resolved_body(&base_resolution, 1, 0), 1);
        let anchors = chunk
            .freeze_placement_anchors(&base, &base_resolution)
            .unwrap();
        let overlay = [module(
            "new-owner",
            vec![Member {
                export_name: "Consumer".to_string(),
                selector: MemberSelector::Authored(MemberSelectorSpec::CrossRef(CrossRefTarget {
                    anchor: "Repeated".to_string(),
                    kind: Some(BindingSourceKind::VariableDeclarator),
                })),
            }],
        )];
        let placement = chunk
            .resolve_placement(&overlay, &anchors, &BTreeSet::from([SourceUnitOrdinal(0)]))
            .unwrap();
        assert_eq!(resolved_body(&placement, 0, 0), 3);
    });
}

#[test]
fn overlay_cannot_reuse_a_frozen_module_path() {
    with_chunk(|chunk| {
        let base = [module(
            "existing-owner",
            vec![binding_member("anchor", "anchor")],
        )];
        let anchors = frozen_anchors(chunk, &base);
        assert!(
            chunk
                .resolve_placement(
                    &[module("existing-owner", Vec::new())],
                    &anchors,
                    &BTreeSet::new()
                )
                .is_err()
        );
    });
}

#[test]
fn placement_rejects_an_occupied_ordinal_outside_the_chunk() {
    with_chunk(|chunk| {
        let base = [module(
            "existing-owner",
            vec![binding_member("anchor", "anchor")],
        )];
        let anchors = frozen_anchors(chunk, &base);
        assert!(
            chunk
                .resolve_placement(
                    &[module("new-owner", Vec::new())],
                    &anchors,
                    &BTreeSet::from([SourceUnitOrdinal(99)]),
                )
                .is_err()
        );
    });
}

#[test]
fn an_unresolved_base_cannot_be_frozen_as_a_wildcard_anchor() {
    with_chunk(|chunk| {
        let base = [module(
            "unresolved-owner",
            vec![source_match_member(
                "Missing",
                "missing",
                "const missing = 9;",
            )],
        )];
        let base_resolution = chunk.resolve(&base).unwrap();
        assert!(
            chunk
                .freeze_placement_anchors(&base, &base_resolution)
                .is_err()
        );
    });
}
