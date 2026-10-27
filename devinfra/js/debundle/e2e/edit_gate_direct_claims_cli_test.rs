//! Direct binding claim mismatches are rejected by pre-write edit gates.

use debundle_e2e_support::GraphFixture;
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
    assert_eq!(report["gate"], "passed", "{report}");
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
fn import_specifier_source_claims_cover_each_import_form() {
    for (source, full_run_succeeds) in [
        (
            "import { dep } from \"external-package\";\nconsole.log(dep);\n",
            true,
        ),
        (
            "import dep from \"external-package\";\nconsole.log(dep);\n",
            false,
        ),
        (
            "import * as dep from \"external-package\";\nconsole.log(dep);\n",
            false,
        ),
    ] {
        let modules = &[
            (
                "alpha.yaml",
                "members: [{selector: {binding: {name: dep, kind: import_specifier}}}]\n",
            ),
            EMPTY,
        ];
        let fixture = if full_run_succeeds {
            GraphFixture::new(source, modules)
        } else {
            GraphFixture::rejected(source, modules)
        };
        assert!(fixture.graph.exists(), "analysis graph was not emitted");
        if !full_run_succeeds {
            fixture.assert_current_pipeline_rejects(&[
                "no named import specifier found for `dep` in source chunk",
            ]);
        }
        let report = fixture.json(MERGE);
        assert_eq!(report["gate"], "passed", "{report}");
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

    let report = fixture.json(MERGE);
    assert_eq!(report["gate"], "passed", "{report}");
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
