//! Direct binding claim mismatches are rejected by pre-write edit gates.

use debundle_e2e_support::{GraphFixture, debundler_path};
use std::fs;
use std::process::Command;

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

fn assert_current_pipeline_rejects(fixture: &GraphFixture, diagnostics: &[&str]) {
    let root = fixture.root();
    let out_root = root.join("current-spec-validation");
    let output = Command::new(debundler_path())
        .args(["run", "--tree-config"])
        .arg(root.join("spec_config.yaml"))
        .arg("--tree-modules")
        .arg(&fixture.modules)
        .arg("--tree-vendor-marks")
        .arg(root.join("vendor_marks.yaml"))
        .arg("--tree-source-root")
        .arg(root)
        .arg("--out-root")
        .arg(out_root)
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        !output.status.success(),
        "current malformed spec unexpectedly passed the pipeline"
    );
    for diagnostic in diagnostics {
        assert!(stderr.contains(diagnostic), "{stderr}");
    }
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
    assert_current_pipeline_rejects(
        &fixture,
        &["Ghost", "no matching local declaration source unit exists"],
    );
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
    assert_current_pipeline_rejects(
        &fixture,
        &[
            "Ghost",
            "no source declaration or import with this kind was found",
        ],
    );
}

#[test]
fn import_specifier_source_claims_cover_each_import_form() {
    for source in [
        "import { dep } from \"external-package\";\nconsole.log(dep);\n",
        "import dep from \"external-package\";\nconsole.log(dep);\n",
        "import * as dep from \"external-package\";\nconsole.log(dep);\n",
    ] {
        let fixture = GraphFixture::new(
            source,
            &[
                (
                    "alpha.yaml",
                    "members: [{selector: {binding: {name: dep, kind: import_specifier}}}]\n",
                ),
                EMPTY,
            ],
        );
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
    assert_current_pipeline_rejects(
        &fixture,
        &[
            "class_declaration",
            "source occurrences have incompatible kinds",
        ],
    );
}
