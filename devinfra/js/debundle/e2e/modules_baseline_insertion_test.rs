//! The append-last constructor preserves the old projected traversal across a
//! multi-owner import component and produces an applicable exact plan.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, process};
use serde_json::{json, to_vec};
use std::fs;

#[test]
fn extracts_lazy_consumer_from_multi_anchor_import_scc() {
    let fixture = GraphFixture::new(
        "const left = 'L';\n\
         function readRight() { return right; }\n\
         const right = 'R';\n\
         function readLeft() { return left; }\n\
         function extracted() { return left + right; }\n\
         console.log(extracted());\n",
        &[
            (
                "a_left.yaml",
                "members: [{selector: {binding: {name: left}}}, {selector: {binding: {name: readRight}}}]\n",
            ),
            (
                "b_right.yaml",
                "members: [{selector: {binding: {name: right}}}, {selector: {binding: {name: readLeft}}}]\n",
            ),
        ],
    );
    assert_eq!(
        json!(fixture.dependency_graph().import_order),
        json!(["a_left", "b_right"])
    );
    let extracted = fixture.source_unit_for_binding("extracted");
    let tree_config = fixture.root().join("spec_config.yaml");
    let only = fixture.root().join("only-units.json");
    fs::write(&only, to_vec(&[&extracted]).unwrap()).unwrap();

    let output = fixture.command(&[
        "modules",
        "propose",
        "--tree-config",
        tree_config.to_str().unwrap(),
        "--only-units",
        only.to_str().unwrap(),
        "--require",
        &extracted,
        "--format",
        "json",
    ]);
    process::assert_success(&output);
    let proposal = parse_stdout_json(&output);
    assert_eq!(proposal["status"], "found", "{proposal}");
    assert_eq!(proposal["residual_units_extracted"], 1, "{proposal}");
    assert_eq!(
        proposal["work"]["selected_closures_materialized"], 1,
        "{proposal}"
    );
    assert_eq!(proposal["work"]["final_certifications"], 1, "{proposal}");
    assert_eq!(proposal["plan"]["changes"].as_array().unwrap().len(), 1);
    assert_eq!(proposal["plan"]["changes"][0]["unit_id"], extracted);

    let plan = fixture.root().join("plan.json");
    fs::write(&plan, to_vec(&proposal["plan"]).unwrap()).unwrap();
    let a_left_before = fs::read_to_string(fixture.modules.join("a_left.yaml")).unwrap();
    let b_right_before = fs::read_to_string(fixture.modules.join("b_right.yaml")).unwrap();
    fixture.assert_success(&[
        "modules",
        "apply",
        "--plan",
        plan.to_str().unwrap(),
        "--modules",
        fixture.root().join("modules").to_str().unwrap(),
        "--tree-config",
        tree_config.to_str().unwrap(),
    ]);
    assert_eq!(
        fs::read_to_string(fixture.modules.join("a_left.yaml")).unwrap(),
        a_left_before
    );
    assert_eq!(
        fs::read_to_string(fixture.modules.join("b_right.yaml")).unwrap(),
        b_right_before
    );
    fixture.assert_runs("LR\n");
}
