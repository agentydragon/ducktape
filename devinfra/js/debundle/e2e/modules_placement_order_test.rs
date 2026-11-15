//! Characterize current ESM import placement and complete-search policy.
//!
//! These acceptance results describe the current import-order implementation.
//! A future scheduler may accept a different set of placements; the rejected
//! placements below are not claims of semantic impossibility.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use serde_json::{Value, json};
use std::fs;

fn check_batch(fixture: &GraphFixture, changes: Value, accepted: bool) -> Value {
    let before = read_tree(&fixture.modules);
    let path = fixture.root().join("placement.json");
    fs::write(&path, serde_json::to_vec(&changes).unwrap()).unwrap();
    let output = fixture.command(&[
        "modules",
        "check",
        "--batch",
        path.to_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_eq!(
        output.status.success(),
        accepted,
        "{changes}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result = parse_stdout_json(&output);
    assert_eq!(result["accepted"], accepted, "{result}");
    assert_eq!(
        read_tree(&fixture.modules),
        before,
        "check changed the spec"
    );
    result
}

fn ordering_fixture() -> GraphFixture {
    GraphFixture::new(
        "const seed = 'alpha';\n\
         function readFirst() { return first; }\n\
         const first = seed + '-first';\n\
         function readSecond() { return second; }\n\
         const second = seed + '-second';\n\
         console.log(first, second);\n",
        &[
            (
                "b_base.yaml",
                "members: [{selector: {binding: {name: seed}}}, {selector: {binding: {name: readFirst}}}]\n",
            ),
            (
                "m_first.yaml",
                "members: [{selector: {binding: {name: first}}}, {selector: {binding: {name: readSecond}}}]\n",
            ),
        ],
    )
}

#[test]
fn current_import_order_policy_accepts_before_middle_and_fixed_owner_placements() {
    let fixture = ordering_fixture();
    let second = fixture.source_unit_for_binding("second");

    for module in ["a_before", "c_middle", "b_base", "m_first"] {
        check_batch(
            &fixture,
            json!([{"unit_id": second, "module": module}]),
            true,
        );
    }
}

#[test]
fn current_import_order_policy_rejects_the_after_placement_with_tdz_diagnostic() {
    let fixture = ordering_fixture();
    let result = check_batch(
        &fixture,
        json!([{
            "unit_id": fixture.source_unit_for_binding("second"),
            "module": "z_after",
        }]),
        false,
    );
    let cycles = result["validation"]["cycles"].as_array().unwrap();
    assert!(
        cycles
            .iter()
            .any(|cycle| cycle["rejection"] == "esm_evaluation_tdz"),
        "{result}"
    );
}

#[test]
fn current_import_order_policy_accepts_a_joint_batch_and_emits_runtime_order() {
    let fixture = GraphFixture::new(
        "const seed = 'alpha';\n\
         function readFirst() { return first; }\n\
         const first = seed + '-first';\n\
         function readSecond() { return second; }\n\
         const second = seed + '-second';\n\
         const support = second + '-support';\n\
         console.log(first, second, support);\n",
        &[
            (
                "b_base.yaml",
                "comment: Base metadata\nmembers: [{selector: {binding: {name: seed}}}, {selector: {binding: {name: readFirst}}}]\n",
            ),
            (
                "m_first.yaml",
                "comment: Preserve this module metadata\nnote: Authored owner\nmembers: [{selector: {binding: {name: first}}}, {selector: {binding: {name: readSecond}}}]\n",
            ),
        ],
    );
    let second = fixture.source_unit_for_binding("second");
    let support = fixture.source_unit_for_binding("support");

    let second_alone = check_batch(
        &fixture,
        json!([{"unit_id": second, "module": "z_after"}]),
        false,
    );
    assert!(
        second_alone["validation"]["cycles"]
            .as_array()
            .unwrap()
            .iter()
            .any(|cycle| cycle["rejection"] == "esm_evaluation_tdz"),
        "{second_alone}"
    );
    let support_alone = check_batch(
        &fixture,
        json!([{"unit_id": support, "module": "m_first"}]),
        false,
    );
    assert!(
        !support_alone["accepted"].as_bool().unwrap(),
        "{support_alone}"
    );

    let joint = json!([
        {"unit_id": second, "module": "z_after"},
        {"unit_id": support, "module": "m_first"},
    ]);
    check_batch(&fixture, joint.clone(), true);
    let path = fixture.root().join("apply.json");
    fs::write(&path, serde_json::to_vec(&joint).unwrap()).unwrap();
    fixture.assert_success(&["modules", "apply", "--assignments", path.to_str().unwrap()]);

    let first_module = fs::read_to_string(fixture.modules.join("m_first.yaml")).unwrap();
    assert!(
        first_module.contains("comment: Preserve this module metadata"),
        "{first_module}"
    );
    assert!(
        first_module.contains("note: Authored owner"),
        "{first_module}"
    );
    fixture.assert_runs("alpha-first alpha-second alpha-second-support\n");
}

#[test]
fn current_complete_search_policy_bounds_required_residuals_by_new_owner_count() {
    let fixture = GraphFixture::new(
        "const seedOne = 'one';\n\
         function readFirstOne() { return firstOne; }\n\
         const firstOne = seedOne + '-first';\n\
         function readSecondOne() { return secondOne; }\n\
         const secondOne = seedOne + '-second';\n\
         const seedTwo = 'two';\n\
         function readGoodTwo() { return goodTwo; }\n\
         const badTwo = seedTwo + '-bad';\n\
         const goodTwo = seedTwo + '-good';\n\
         function readBadTwo() { return badTwo; }\n\
         const extraTwo = goodTwo + '-extra';\n\
         console.log(firstOne, secondOne, badTwo, goodTwo, extraTwo);\n",
        &[
            (
                "b_base_one.yaml",
                "members: [{selector: {binding: {name: seedOne}}}, {selector: {binding: {name: readFirstOne}}}]\n",
            ),
            (
                "m_first_one.yaml",
                "members: [{selector: {binding: {name: firstOne}}}, {selector: {binding: {name: readSecondOne}}}]\n",
            ),
            (
                "w_base_two.yaml",
                "members: [{selector: {binding: {name: seedTwo}}}, {selector: {binding: {name: readGoodTwo}}}]\n",
            ),
            (
                "x_bad_two.yaml",
                "members: [{selector: {binding: {name: badTwo}}}]\n",
            ),
            (
                "z_good_two.yaml",
                "members: [{selector: {binding: {name: goodTwo}}}, {selector: {binding: {name: readBadTwo}}}]\n",
            ),
        ],
    );
    let second_one = fixture.source_unit_for_binding("secondOne");
    let extra_two = fixture.source_unit_for_binding("extraTwo");
    let only = fixture.root().join("only-units.json");
    fs::write(
        &only,
        serde_json::to_vec(&json!([second_one, extra_two])).unwrap(),
    )
    .unwrap();
    let common = [
        "modules",
        "propose",
        "--strategy",
        "complete",
        "--destinations",
        "new",
        "--only-units",
        only.to_str().unwrap(),
        "--require",
        &second_one,
        "--require",
        &extra_two,
    ];

    let mut one_owner = common.to_vec();
    one_owner.extend(["--max-new-modules", "1"]);
    let exhausted = fixture.json(&one_owner);
    assert_eq!(exhausted["status"], "exhausted_no_peel", "{exhausted}");
    assert!(exhausted["plan"].is_null(), "{exhausted}");

    let mut two_owners = common.to_vec();
    two_owners.extend(["--max-new-modules", "2"]);
    let found = fixture.json(&two_owners);
    assert_eq!(found["status"], "found", "{found}");
    assert_eq!(found["residual_units_extracted"], 2, "{found}");

    // This is the current fixed-owner/new-path policy's result. It does not
    // claim that an alternate scheduler could not place both in one owner.
    let explicit = json!([
        {"unit_id": second_one, "module": "a_before"},
        {"unit_id": extra_two, "module": "zz_after"},
    ]);
    check_batch(&fixture, explicit.clone(), true);
    let path = fixture.root().join("apply.json");
    fs::write(&path, serde_json::to_vec(&explicit).unwrap()).unwrap();
    fixture.assert_success(&["modules", "apply", "--assignments", path.to_str().unwrap()]);
    fixture.assert_runs("one-first one-second two-bad two-good two-good-extra\n");
}
