//! Direct binding claim mismatches are rejected by pre-write edit gates.

use analysis_model::DepKind;
use debundle_e2e_graph_support::GraphFixture;
use std::fs;

const EMPTY: (&str, &str) = ("empty.yaml", "members: []\n");
const MERGE: &[&str] = &[
    "modules",
    "merge",
    "--target",
    "combined",
    "alpha.yaml",
    "empty.yaml",
];

fn assert_valid_merge_gate(fixture: &GraphFixture) {
    let mut args = MERGE.to_vec();
    args.push("--dry-run");
    let report = fixture.json(&args);
    assert_eq!(report["gate"], "claims_checked", "{report}");
    assert_eq!(report["needs_full_assessment"], true, "{report}");
}

#[test]
fn missing_local_direct_claim_is_rejected_without_writes() {
    let fixture = GraphFixture::new(
        "const live = 1;\nconsole.log(live);\n",
        &[
            (
                "alpha.yaml",
                "members: [{selector: {binding: {name: live}}}]\n",
            ),
            EMPTY,
        ],
    );
    assert_valid_merge_gate(&fixture);
    fs::write(
        fixture.modules.join("alpha.yaml"),
        "members: [{selector: {binding: {name: Ghost}}}]\n",
    )
    .unwrap();

    fixture.assert_rejected_unchanged(MERGE, &["graph compatibility mismatch"]);
    fixture.assert_current_pipeline_rejects(&[
        "Ghost",
        "no top-level declaration with that name exists",
    ]);
}

#[test]
fn missing_import_direct_claim_is_rejected_without_writes() {
    let fixture = GraphFixture::new(
        "import { dep } from \"external-package\";\nconsole.log(dep);\n",
        &[
            (
                "alpha.yaml",
                "members: [{selector: {binding: {name: dep, kind: import_specifier}}}]\n",
            ),
            EMPTY,
        ],
    );
    assert_valid_merge_gate(&fixture);
    fs::write(
        fixture.modules.join("alpha.yaml"),
        "members: [{selector: {binding: {name: Ghost, kind: import_specifier}}}]\n",
    )
    .unwrap();

    fixture.assert_rejected_unchanged(MERGE, &["graph compatibility mismatch"]);
    fixture.assert_current_pipeline_rejects(&[
        "no import specifier found for `Ghost` in source chunk",
    ]);
}

#[test]
fn import_specifier_claims_are_accepted_only_for_named_imports() {
    let named = GraphFixture::new(
        "import { dep } from \"external-package\";\nconsole.log(dep);\n",
        &[
            (
                "alpha.yaml",
                "members: [{selector: {binding: {name: dep, kind: import_specifier}}}]\n",
            ),
            EMPTY,
        ],
    );
    let mut preview = MERGE.to_vec();
    preview.push("--dry-run");
    let report = named.json(&preview);
    assert_eq!(report["gate"], "claims_checked", "{report}");
    assert_eq!(report["needs_full_assessment"], true, "{report}");

    for source in [
        "import dep from \"external-package\";\nconsole.log(dep);\n",
        "import * as dep from \"external-package\";\nconsole.log(dep);\n",
    ] {
        let stale_graph = GraphFixture::new(source, &[("alpha.yaml", "members: []\n"), EMPTY]);
        fs::write(
            stale_graph.modules.join("alpha.yaml"),
            "members: [{selector: {binding: {name: dep, kind: import_specifier}}}]\n",
        )
        .unwrap();
        stale_graph.assert_rejected_unchanged(MERGE, &["graph compatibility mismatch"]);

        let fixture = GraphFixture::rejected(
            source,
            &[
                (
                    "alpha.yaml",
                    "members: [{selector: {binding: {name: dep, kind: import_specifier}}}]\n",
                ),
                EMPTY,
            ],
        );
        fixture.assert_current_pipeline_rejects(&[
            "no named import specifier found for `dep` in source chunk",
        ]);
    }
}

#[test]
fn mixed_untyped_local_and_typed_import_claims_use_direct_source_gate() {
    let fixture = GraphFixture::new(
        "const live = 1;\nimport { dep } from \"external-package\";\nconsole.log(live, dep);\n",
        &[
            (
                "alpha.yaml",
                "members: [{selector: {binding: {name: live}}}, {selector: {binding: {name: dep, kind: import_specifier}}}]\n",
            ),
            EMPTY,
        ],
    );

    let mut preview = MERGE.to_vec();
    preview.push("--dry-run");
    let report = fixture.json(&preview);
    assert_eq!(report["gate"], "claims_checked", "{report}");
    assert_eq!(report["needs_full_assessment"], true, "{report}");
}

#[test]
fn repeated_declarations_require_connected_co_declaration_evidence_for_the_same_binding() {
    let fixture = GraphFixture::new_full_tree(
        "var counter = 1;\nvar counter;\nvar counter = counter + 1;\nconsole.log(counter);\n",
        &[
            (
                "alpha.yaml",
                "members: [{selector: {binding: {name: counter}}}]\n",
            ),
            EMPTY,
        ],
    );
    let original = fixture.dependency_graph();
    let representative = &original
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|declared| declared.binding.as_ref() == "counter")
        })
        .unwrap()
        .id;

    // Retain only edges directed toward the representative: claim identity is
    // an undirected equality even though the dependency graph is directed.
    let mut incoming_only = original.clone();
    incoming_only
        .edges
        .retain(|edge| edge.edge_kind != DepKind::CoDeclaration || edge.source != *representative);
    report_io::write_json_gzip(&fixture.graph, &incoming_only).unwrap();
    assert_valid_merge_gate(&fixture);

    // A dependency of another kind, another binding's equality, and an
    // unlabelled equality cannot connect this binding's declaration sites.
    for (kind, binding) in [
        (DepKind::LazyUse, Some("counter")),
        (DepKind::CoDeclaration, Some("other")),
        (DepKind::CoDeclaration, None),
    ] {
        let mut graph = incoming_only.clone();
        for edge in &mut graph.edges {
            if edge.edge_kind == DepKind::CoDeclaration {
                edge.edge_kind = kind;
                edge.binding = binding.map(Into::into);
            }
        }
        report_io::write_json_gzip(&fixture.graph, &graph).unwrap();
        fixture.assert_rejected_unchanged(MERGE, &["ambiguous declaration source units"]);
    }

    report_io::write_json_gzip(&fixture.graph, &original).unwrap();
    let assessment = fixture.stage_analysis();
    let report = fixture.json_with_assessment(MERGE, &assessment);
    assert_eq!(report["gate"], "passed", "{report}");
    fixture.assert_runs("2\n");
}

#[test]
fn mismatched_local_source_kind_is_rejected_without_writes() {
    let fixture = GraphFixture::new(
        "const live = 1;\nconsole.log(live);\n",
        &[
            (
                "alpha.yaml",
                "members: [{selector: {binding: {name: live, kind: variable_declarator}}}]\n",
            ),
            EMPTY,
        ],
    );
    assert_valid_merge_gate(&fixture);
    fs::write(
        fixture.modules.join("alpha.yaml"),
        "members: [{selector: {binding: {name: live, kind: class_declaration}}}]\n",
    )
    .unwrap();

    fixture.assert_rejected_unchanged(MERGE, &["graph compatibility mismatch"]);
    fixture.assert_current_pipeline_rejects(&[
        "did not match any top-level declaration",
        "ClassDeclaration",
    ]);
}
