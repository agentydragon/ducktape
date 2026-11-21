//! Exact residual discovery outcomes, selector preflight, and checked plans.

use super::support::CHAIN_SOURCE;
use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::read_tree;
use serde_json::json;
use std::fs;

fn chain() -> GraphFixture {
    GraphFixture::new(
        CHAIN_SOURCE,
        &[(
            "home.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    )
}

#[test]
fn unseeded_required_closure_is_checked_previewed_applied_and_runs() {
    let fixture = chain();
    let [consumer, dep] = fixture.source_units_for_bindings(["consumer", "dep"]);
    let only = fixture.save_json("only.json", &json!([consumer, dep]));
    let before = read_tree(&fixture.modules);
    let config = fixture.tree_config().to_str().unwrap().to_owned();
    let result = fixture.json(&[
        "modules",
        "propose",
        "--tree-config",
        &config,
        "--only-units",
        &only,
        "--require",
        &consumer,
    ]);
    assert_eq!(result["status"], "found", "{result}");
    assert_eq!(result["residual_units_extracted"], 2);
    assert_eq!(result["plan"]["import_order"].as_array().unwrap().len(), 2);
    assert_eq!(read_tree(&fixture.modules), before);

    let plan = fixture.save_json("plan.json", &result["plan"]);
    fixture.assert_plan_checks_previews_applies_and_runs(
        &plan,
        Some(&fixture.root().join("modules")),
        "1 3\n",
    );
}

#[test]
fn a_peel_with_repeated_initializers_uses_its_named_reference() {
    let fixture = GraphFixture::new(
        "function make(callback) { return callback; }\n\
         const first = make(() => {});\n\
         const second = make(() => {});\n\
         const third = make(() => {});\n\
         const fourth = make(() => {});\n\
         const fifth = make(() => {});\n\
         const roles = { primary: first, secondary: second };\n\
         console.log(typeof roles.primary);\n",
        &[(
            "factory.yaml",
            "members: [{selector: {binding: {name: make}}}]\n",
        )],
    );
    let first = fixture.source_unit_for_binding("first");
    let only = fixture.save_json("only.json", &json!([first]));
    let config = fixture.tree_config().to_str().unwrap().to_owned();
    let before_modules = read_tree(&fixture.modules);
    let before_config = fs::read(&config).unwrap();
    let result = fixture.json(&[
        "modules",
        "propose",
        "--tree-config",
        &config,
        "--only-units",
        &only,
    ]);
    assert_eq!(result["status"], "found", "{result}");
    assert_eq!(result["residual_units_extracted"], 1);
    assert_eq!(read_tree(&fixture.modules), before_modules);
    assert_eq!(fs::read(&config).unwrap(), before_config);
    let plan = fixture.save_json("plan.json", &result["plan"]);
    fixture.assert_plan_checks_previews_applies_and_runs(
        &plan,
        Some(&fixture.modules),
        "function\n",
    );
}

#[test]
fn unseeded_peels_can_claim_an_indistinguishable_run_jointly() {
    let fixture = GraphFixture::new(
        "function make(callback) { return callback; }\n\
         const first = make(() => {});\n\
         const second = make(() => {});\n\
         const third = make(() => {});\n\
         const fourth = make(() => {});\n\
         const fifth = make(() => {});\n\
         console.log(typeof first, first !== second, second !== third, third !== fourth, fourth !== fifth);\n",
        &[(
            "factory.yaml",
            "members: [{selector: {binding: {name: make}}}]\n",
        )],
    );
    let config = fixture.tree_config().to_str().unwrap().to_owned();
    let before = read_tree(&fixture.modules);
    let result = fixture.json(&["modules", "propose", "--tree-config", &config]);
    assert_eq!(result["status"], "found", "{result}");
    assert_eq!(result["residual_units_extracted"], 6);
    assert_eq!(read_tree(&fixture.modules), before);
    let plan = fixture.save_json("plan.json", &result["plan"]);
    fixture.assert_plan_checks_previews_applies_and_runs(
        &plan,
        Some(&fixture.modules),
        "function true true true true\n",
    );
}

#[test]
fn required_closure_blocked_by_fixed_entry_is_reported_without_a_plan() {
    let fixture = chain();
    let consumer = fixture.source_unit_for_binding("consumer");
    let only = fixture.save_json("only.json", &json!([consumer]));
    let before = read_tree(&fixture.modules);
    let report = fixture.json(&[
        "modules",
        "propose",
        "--into",
        "chain",
        "--only-units",
        &only,
        "--require",
        &consumer,
    ]);
    assert_eq!(report["status"], "exhausted_no_peel", "{report}");
    assert!(report["plan"].is_null());
    assert!(!report["blockers"].as_array().unwrap().is_empty());
    assert_eq!(read_tree(&fixture.modules), before);
}

#[test]
fn a_cut_atomic_group_is_a_scope_error_not_a_no_peel_result() {
    let fixture = GraphFixture::new("let value = 0;\nvalue += 1;\nconsole.log(value);\n", &[]);
    let only = fixture.save_json(
        "cut.json",
        &json!([fixture.source_unit_for_binding("value")]),
    );
    let output = fixture.command(&[
        "modules",
        "propose",
        "--only-units",
        &only,
        "--format",
        "json",
    ]);
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("cuts an atomic group"));
    assert!(
        output.stdout.is_empty(),
        "scope error must not report exhaustion"
    );
}

#[test]
fn saved_plan_cannot_be_retargeted_to_a_refreshed_program_with_reused_ids() {
    let fixture = chain();
    let [consumer, dep] = fixture.source_units_for_bindings(["consumer", "dep"]);
    let only = fixture.save_json("only.json", &json!([consumer, dep]));
    let config = fixture.tree_config().to_str().unwrap().to_owned();
    let result = fixture.json(&[
        "modules",
        "propose",
        "--tree-config",
        &config,
        "--only-units",
        &only,
        "--require",
        &consumer,
    ]);
    let plan = fixture.save_json("plan.json", &result["plan"]);
    fs::write(
        fixture.source_path(),
        "const anchor = 1;\nconst dep = 3;\nconst consumer = dep + 1;\nconsole.log(anchor, consumer);\n",
    )
    .unwrap();
    fixture.refresh_graph();
    fixture.assert_rejected_unchanged(
        &[
            "modules",
            "apply",
            "--plan",
            &plan,
            "--modules",
            fixture.root().join("modules").to_str().unwrap(),
            "--tree-config",
            fixture.tree_config().to_str().unwrap(),
        ],
        &["assignment plan is stale"],
    );
    let checked = fixture.command(&["modules", "check", "--plan", &plan]);
    assert_eq!(checked.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&checked.stderr).contains("assignment plan is stale"));
}
