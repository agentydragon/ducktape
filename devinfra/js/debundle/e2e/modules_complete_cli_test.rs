//! Public complete-search outcomes, exact witnesses and failure boundaries.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use serde_json::{Value, json};
use std::fs;

fn save(fixture: &GraphFixture, name: &str, value: &Value) -> String {
    let path = fixture.root().join(name);
    fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
    path.to_str().unwrap().to_string()
}

fn chain() -> GraphFixture {
    GraphFixture::new(
        "const anchor = 1;\nconst dep = 2;\nconst consumer = dep + 1;\nconsole.log(anchor, consumer);\n",
        &[(
            "home.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    )
}

#[test]
fn finds_required_joint_peel_checks_and_applies_the_same_plan() {
    let fixture = chain();
    let consumer = fixture.source_unit_for_binding("consumer");
    let dep = fixture.source_unit_for_binding("dep");
    let only = save(&fixture, "only.json", &json!([consumer, dep]));
    let before = read_tree(&fixture.modules);
    let result = fixture.json(&[
        "modules",
        "propose",
        "--only-units",
        &only,
        "--require",
        &consumer,
    ]);
    assert_eq!(result["status"], "found", "{result}");
    assert_eq!(result["residual_units_extracted"], 2);
    assert_eq!(read_tree(&fixture.modules), before);
    let plan = save(&fixture, "plan.json", &result["plan"]);
    fixture.assert_success(&["modules", "check", "--plan", &plan]);
    fixture.assert_success(&["modules", "apply", "--plan", &plan, "--dry-run"]);
    assert_eq!(read_tree(&fixture.modules), before);
    fixture.assert_success(&["modules", "apply", "--plan", &plan]);
    fixture.assert_runs("1 3\n");
}

#[test]
fn exhaustive_negative_and_explicit_budget_exhaustion_are_distinct() {
    let fixture = chain();
    let consumer = fixture.source_unit_for_binding("consumer");
    let only = save(&fixture, "only.json", &json!([consumer]));
    let args = [
        "modules",
        "propose",
        "--into",
        "chain",
        "--only-units",
        &only,
        "--require",
        &consumer,
    ];
    let negative = fixture.json(&args);
    assert_eq!(negative["status"], "exhausted_no_peel", "{negative}");
    assert!(negative["checks"].as_u64().unwrap() > 0);
    assert!(negative["plan"].is_null());
    let before = read_tree(&fixture.modules);
    for budget in [["--max-checks", "0"], ["--time-budget-ms", "0"]] {
        let mut args = args.to_vec();
        args.extend(budget);
        args.extend(["--format", "json"]);
        let output = fixture.command(&args);
        assert_eq!(output.status.code(), Some(1));
        let result = parse_stdout_json(&output);
        assert_eq!(result["status"], "incomplete", "{result}");
        assert!(result["plan"].is_null());
        assert_eq!(result["checks"], 0);
        assert_eq!(read_tree(&fixture.modules), before);
    }
}

#[test]
fn owner_caps_use_domains_before_scheduling_an_out_of_scope_partition() {
    let modules: Vec<_> = (0..100)
        .map(|index| (format!("unrelated_{index:03}.yaml"), "members: []\n"))
        .collect();
    let modules: Vec<_> = modules
        .iter()
        .map(|(name, text)| (name.as_str(), *text))
        .collect();
    let fixture = GraphFixture::new(
        "const first = 'alpha';\nconst second = 'beta';\nconsole.log(first, second);\n",
        &modules,
    );
    let only = save(
        &fixture,
        "only.json",
        &json!([
            fixture.source_unit_for_binding("first"),
            fixture.source_unit_for_binding("second"),
        ]),
    );
    let output = fixture.command(&[
        "modules",
        "propose",
        "--destinations",
        "new",
        "--only-units",
        &only,
        "--max-new-modules",
        "1",
        "--max-checks",
        "1",
        "--format",
        "json",
    ]);
    assert_eq!(output.status.code(), Some(1));
    let report = parse_stdout_json(&output);
    assert_eq!(report["status"], "incomplete", "{report}");
    assert_eq!(report["work"]["branch_clauses"]["fresh_owner_cap"], 1);
    // Two fresh atoms need either one mutual merge or one atom staying at
    // entry. The hundred unrelated authored owners are outside their domains.
    assert_eq!(report["work"]["contraction_attempts"], 3, "{report}");
    assert_eq!(report["work"]["schedule_checkpoints"], 0, "{report}");
    assert!(report["plan"].is_null());
}

#[test]
fn entry_constraints_exhaust_a_large_required_scope_without_filtered_enumeration() {
    let mut source = String::from("const seed = 'alpha';\n");
    for index in 0..30 {
        let operand = if index == 0 {
            "seed".to_string()
        } else {
            format!("value{}", index - 1)
        };
        source.push_str(&format!("const value{index} = {operand} + '-beta';\n"));
    }
    source.push_str("function lazyBack() { return value29; }\nexport { seed, lazyBack };\n");
    let fixture = GraphFixture::new(&source, &[]);
    let units: Vec<_> = (0..30)
        .map(|index| fixture.source_unit_for_binding(&format!("value{index}")))
        .collect();
    let only = save(&fixture, "only.json", &json!(units));
    let mut args = vec![
        "modules".to_string(),
        "propose".to_string(),
        "--into".to_string(),
        "a_before".to_string(),
        "--only-units".to_string(),
        only,
    ];
    for unit in units {
        args.extend(["--require".to_string(), unit]);
    }
    // Every required consumer depends on a fixed true-entry seed. The direct
    // graph constraint proves this impossible; no scan of the other billion
    // stay/move combinations is necessary. A zero budget remains covered above.
    let before = read_tree(&fixture.modules);
    let mut args: Vec<_> = args.iter().map(String::as_str).collect();
    args.extend(["--max-checks", "2", "--time-budget-ms", "1000"]);
    let report = fixture.json(&args);
    assert_eq!(report["status"], "exhausted_no_peel", "{report}");
    assert!(report["checks"].as_u64().unwrap() <= 2, "{report}");
    assert!(report["plan"].is_null());
    assert_eq!(read_tree(&fixture.modules), before);
}

#[test]
fn existing_destination_is_checked_in_place_and_fixed_owners_stay_fixed() {
    let fixture = GraphFixture::dependency_chain();
    let blocked = fixture.json(&[
        "modules",
        "propose",
        "--destinations",
        "existing",
        "--into",
        "b",
    ]);
    assert_eq!(blocked["status"], "exhausted_no_peel", "{blocked}");
    let accepted = fixture.json(&[
        "modules",
        "propose",
        "--destinations",
        "existing",
        "--into",
        "a",
    ]);
    assert_eq!(accepted["status"], "found", "{accepted}");
    let log = fixture.source_unit_for_line(4);
    assert_eq!(
        accepted["plan"]["changes"],
        json!([{"unit_id": log, "module": "a"}])
    );
    let plan = save(&fixture, "plan.json", &accepted["plan"]);
    fixture.assert_success(&["modules", "apply", "--plan", &plan]);
    fixture.assert_runs("3\n");
}

#[test]
fn baseline_three_unit_atom_is_preserved_and_cut_scope_is_an_error() {
    let fixture = GraphFixture::new(
        "let first = 0;\nlet second = 0;\nfunction update() { first += 1; second += 1; }\nconst helper = 3;\nconsole.log(first, second, helper);\n",
        &[(
            "home.yaml",
            "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: second}}}, {selector: {binding: {name: update}}}]\n",
        )],
    );
    let helper = fixture.source_unit_for_binding("helper");
    let result = fixture.json(&["modules", "propose", "--require", &helper]);
    assert_eq!(result["status"], "found", "{result}");
    for row in result["plan"]["changes"].as_array().unwrap() {
        assert_ne!(row["unit_id"], fixture.source_unit_for_binding("first"));
        assert_ne!(row["unit_id"], fixture.source_unit_for_binding("second"));
        assert_ne!(row["unit_id"], fixture.source_unit_for_binding("update"));
    }
    let atom = GraphFixture::new("let value = 0;\nvalue += 1;\nconsole.log(value);\n", &[]);
    let only = save(
        &atom,
        "cut.json",
        &json!([atom.source_unit_for_binding("value")]),
    );
    let output = atom.command(&[
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
        "scope error must not claim no peel"
    );
}

#[test]
fn saved_plan_cannot_be_retargeted_to_a_refreshed_program_with_reused_ids() {
    let fixture = chain();
    let result = fixture.json(&["modules", "propose"]);
    let plan = save(&fixture, "plan.json", &result["plan"]);
    fs::write(fixture.source_path(), "const anchor = 1;\nconst dep = 3;\nconst consumer = dep + 1;\nconsole.log(anchor, consumer);\n").unwrap();
    fixture.refresh_graph();
    fixture.assert_rejected_unchanged(
        &["modules", "apply", "--plan", &plan],
        &["assignment plan is stale"],
    );
    let checked = fixture.command(&["modules", "check", "--plan", &plan]);
    assert_eq!(checked.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&checked.stderr).contains("assignment plan is stale"));
}

#[test]
fn plan_logical_destinations_work_with_a_configured_physical_tree_root() {
    let fixture = chain();
    let modules = fixture.root().join("modules");
    let config = fixture.root().join("spec_config.yaml");
    let scope = [
        "--modules",
        modules.to_str().unwrap(),
        "--tree-config",
        config.to_str().unwrap(),
    ];
    let mut args = vec!["modules", "propose"];
    args.extend(scope);
    let result = fixture.json(&args);
    assert_eq!(result["status"], "found");
    let plan = save(&fixture, "plan.json", &result["plan"]);
    for command in ["check", "apply"] {
        let mut args = vec!["modules", command, "--plan", &plan];
        args.extend(scope);
        fixture.assert_success(&args);
    }
    fixture.assert_runs("1 3\n");
}

#[test]
fn contradictory_or_unsupported_options_never_report_no_peel() {
    let fixture = chain();
    let before = read_tree(&fixture.modules);
    for tail in [
        vec!["--destinations", "existing", "--into", "absent"],
        vec!["--size-cap-lines", "1"],
        vec!["--strategy", "auto"],
    ] {
        let mut args = vec!["modules", "propose", "--format", "json"];
        args.extend(tail);
        let output = fixture.command(&args);
        assert!(matches!(output.status.code(), Some(1 | 2)));
        assert!(output.stdout.is_empty());
        assert_eq!(read_tree(&fixture.modules), before);
    }
}

#[test]
fn empty_authored_owner_is_an_existing_destination() {
    let fixture = GraphFixture::new(
        "const value = 1;\nconsole.log(value);\n",
        &[("empty.yaml", "comment: Authored owner\nmembers: []\n")],
    );
    let result = fixture.json(&[
        "modules",
        "propose",
        "--destinations",
        "existing",
        "--into",
        "empty",
    ]);
    assert_eq!(result["status"], "found", "{result}");
    let plan = save(&fixture, "plan.json", &result["plan"]);
    fixture.assert_success(&["modules", "apply", "--plan", &plan]);
    fixture.assert_runs("1\n");
}
