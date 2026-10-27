//! Source-backed edit gates must resolve the same claims as the run pipeline.

use debundle_e2e_support::{GraphFixture, write_text_file};
use std::fs;

const GAMMA: (&str, &str) = (
    "solo/gamma.yaml",
    "members: [{selector: {binding: {name: gamma}}}]\n",
);
const MEMBER: &str = "members: [{selector: {binding: {name: beta}}}]\n";
const MATCH: &str =
    "source_matches: [{match: 'let alpha = 0;', bindings: [{local: alpha, name: Alpha}]}]\n";

fn atomic_fixture(yaml: &str) -> GraphFixture {
    GraphFixture::new(
        "let alpha = 0;\nfunction beta() { alpha = 1; }\nconst gamma = 3;\nconsole.log(alpha, typeof beta, gamma);\n",
        &[("home/atom.yaml", yaml), GAMMA],
    )
}

#[test]
fn source_match_siblings_cannot_be_split_in_either_yaml_order() {
    for yaml in [format!("{MEMBER}{MATCH}"), format!("{MATCH}{MEMBER}")] {
        atomic_fixture(&yaml).assert_rejected_unchanged(
            &["bindings", "unassign", "beta"],
            &["splits one or more atomic groups"],
        );
    }
}

#[test]
fn independent_unassign_resolves_source_match_claims_and_still_runs() {
    let fixture = atomic_fixture(&format!("{MEMBER}{MATCH}"));
    fixture.assert_success(&["bindings", "unassign", "gamma"]);
    assert!(!fixture.modules.join("solo/gamma.yaml").exists());
    fixture.assert_runs("0 function 3\n");
}

#[test]
fn missing_sources_are_a_hard_error_without_writes() {
    let fixture = atomic_fixture(&format!("{MEMBER}{MATCH}"));
    fs::remove_file(fixture.source_path()).unwrap();
    fixture.assert_rejected_unchanged(&["bindings", "unassign", "gamma"], &["source"]);
}

#[test]
fn relational_member_still_counts_when_editing_an_anchor_module() {
    let fixture = GraphFixture::new(
        "class Anchor { value() { return 1; } }\nconst helper = Anchor;\nconsole.log(new helper().value());\nexport { Anchor, helper };\n",
        &[
            (
                "anchor.yaml",
                "members: [{name: AnchorClass, selector: {binding: {name: Anchor}}}]\n",
            ),
            (
                "helper.yaml",
                "members: [{name: Helper, selector: {cross_ref: {aliases: AnchorClass}}}]\n",
            ),
        ],
    );
    fixture.assert_rejected_unchanged(
        &["modules", "delete", "anchor.yaml", "--force"],
        &["selector anchor `@AnchorClass` does not name a lowered member"],
    );
}

#[test]
fn malformed_manual_claim_edits_are_refused_before_unassigning() {
    // Generate genuine graph facts first, then require stale-graph refusal for
    // an author's malformed YAML edit before unassign can write anything.
    for (source, yaml, diagnostic) in [
        (
            "const alpha = 1; const beta = 1; const gamma = 3;",
            "source_matches: [{match: 'const x = 1;', bindings: [{local: x, name: X}]}]",
            "is ambiguous — matched 2 declarations (alpha, beta)",
        ),
        (
            "const alpha = 1; const beta = 2; const gamma = 3;",
            "source_matches: [{match: 'const x = 1;', bindings: [{local: x, name: X}]}, {match: 'const y = 1;', bindings: [{local: y, name: Y}]}]",
            "participates in ownership conflict",
        ),
        (
            r#"import { dep } from "external"; const gamma = 3;"#,
            r#"source_matches: [{match: 'import { dep } from "external";', bindings: [{local: dep, name: Dep}]}]"#,
            "binding `dep` does not map to a dependency graph node",
        ),
        (
            "const alpha = {}; const gamma = 3; alpha.x = 1; alpha.x = 1;",
            "anonymous_statements: [{match: 'alpha.x = 1;'}]",
            "anonymous statement selector matched 2 source statements",
        ),
    ] {
        let fixture = GraphFixture::new(source, &[GAMMA]);
        write_text_file(&fixture.modules.join("home/atom.yaml"), yaml);
        fixture.assert_rejected_unchanged(
            &["bindings", "unassign", "gamma"],
            &["graph compatibility mismatch"],
        );
        fixture.assert_current_pipeline_rejects(&[diagnostic]);
    }
}
