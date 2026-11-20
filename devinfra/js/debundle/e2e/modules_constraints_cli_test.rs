//! The declared proposal family is one appended owner with closure constraints.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::read_tree;
use serde_json::json;

#[test]
fn an_unseeded_residual_finds_one_appended_owner_and_runs() {
    let mut source = "const seed = 7;\n".to_owned();
    for index in 0..32 {
        source.push_str(&format!("const item{index} = seed + {index};\n"));
    }
    source.push_str("console.log(item0, item31);\n");
    let fixture = GraphFixture::new(&source, &[]);
    let before = read_tree(&fixture.modules);
    let config = fixture.tree_config().to_string_lossy().into_owned();
    let proposal = fixture.json(&["modules", "propose", "--tree-config", &config]);
    assert_eq!(proposal["status"], "found", "{proposal}");
    assert_eq!(
        proposal["scope"]["policy"],
        "one_fresh_owner_appended_to_fixed_priority"
    );
    assert_eq!(proposal["work"]["final_certifications"], 1, "{proposal}");
    assert!(!proposal["plan"]["changes"].as_array().unwrap().is_empty());
    assert_eq!(read_tree(&fixture.modules), before);

    let plan = fixture.save_json("plan.json", &proposal["plan"]);
    fixture.assert_plan_checks_previews_applies_and_runs(&plan, None, "7 38\n");
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
    let only = fixture.save_json("only.json", &json!(units));
    let config = fixture.tree_config().to_string_lossy().into_owned();
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

    let plan = fixture.save_json("plan.json", &proposal["plan"]);
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
