//! The declared proposal family is one appended owner with closure constraints.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::read_tree;
use serde_json::{Value, json};
use std::fs;

fn save(fixture: &GraphFixture, name: &str, value: &Value) -> String {
    let path = fixture.root().join(name);
    fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
    path.to_str().unwrap().to_owned()
}

fn tree_config(fixture: &GraphFixture) -> String {
    fixture
        .root()
        .join("spec_config.yaml")
        .to_string_lossy()
        .into_owned()
}

#[test]
fn an_unseeded_residual_finds_one_appended_owner_and_runs() {
    let mut source = "const seed = 7;\n".to_owned();
    for index in 0..32 {
        source.push_str(&format!("const item{index} = seed + {index};\n"));
    }
    source.push_str("console.log(item0, item31);\n");
    let fixture = GraphFixture::new(&source, &[]);
    let before = read_tree(&fixture.modules);
    let config = tree_config(&fixture);
    let proposal = fixture.json(&["modules", "propose", "--tree-config", &config]);
    assert_eq!(proposal["status"], "found", "{proposal}");
    assert_eq!(
        proposal["scope"]["policy"],
        "one_fresh_owner_appended_to_fixed_priority"
    );
    assert_eq!(proposal["work"]["final_certifications"], 1, "{proposal}");
    assert!(!proposal["plan"]["changes"].as_array().unwrap().is_empty());
    assert_eq!(read_tree(&fixture.modules), before);

    let plan = save(&fixture, "plan.json", &proposal["plan"]);
    let config = fixture
        .root()
        .join("spec_config.yaml")
        .to_string_lossy()
        .into_owned();
    fixture.assert_success(&["modules", "check", "--plan", &plan]);
    fixture.assert_success(&[
        "modules",
        "apply",
        "--plan",
        &plan,
        "--tree-config",
        &config,
        "--dry-run",
    ]);
    assert_eq!(read_tree(&fixture.modules), before);
    fixture.assert_success(&[
        "modules",
        "apply",
        "--plan",
        &plan,
        "--tree-config",
        &config,
    ]);
    fixture.assert_runs("7 38\n");
}

#[test]
fn lazy_import_star_successors_are_claimed_together() {
    let fixture = GraphFixture::new(
        "const base = {left: () => left, right: () => right};\n\
         const left = base;\n\
         const right = base;\n\
         console.log(left === base, right === base);\n",
        &[],
    );
    let units: Vec<_> = ["base", "left", "right"]
        .iter()
        .map(|binding| fixture.source_unit_for_binding(binding))
        .collect();
    let only = save(&fixture, "only.json", &json!(units));
    let config = tree_config(&fixture);
    let proposal = fixture.json(&[
        "modules",
        "propose",
        "--tree-config",
        &config,
        "--only-units",
        &only,
        "--require",
        &units[0],
        "--require",
        &units[1],
        "--require",
        &units[2],
    ]);
    assert_eq!(proposal["status"], "found", "{proposal}");
    assert_eq!(proposal["residual_units_extracted"], 3, "{proposal}");
    assert_eq!(proposal["work"]["final_certifications"], 1, "{proposal}");

    let plan = save(&fixture, "plan.json", &proposal["plan"]);
    fixture.assert_success(&[
        "modules",
        "apply",
        "--plan",
        &plan,
        "--tree-config",
        &config,
    ]);
    fixture.assert_runs("true true\n");
}
