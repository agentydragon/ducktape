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
    fs::write(
        fixture.modules.join("alpha.yaml"),
        "members: [{selector: {binding: {name: Ghost}}}]\n",
    )
    .unwrap();

    fixture.assert_rejected_unchanged(
        MERGE,
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
    fs::write(
        fixture.modules.join("alpha.yaml"),
        "members: [{selector: {binding: {name: Ghost, kind: import_specifier}}}]\n",
    )
    .unwrap();

    fixture.assert_rejected_unchanged(
        MERGE,
        &[
            "ImportSpecifier",
            "no source declaration or import with this kind was found",
        ],
    );
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
    fs::write(
        fixture.modules.join("alpha.yaml"),
        "members: [{selector: {binding: {name: live, kind: class_declaration}}}]\n",
    )
    .unwrap();

    fixture.assert_rejected_unchanged(
        MERGE,
        &[
            "ClassDeclaration",
            "source occurrences have incompatible kinds",
        ],
    );
}
