//! Rejection reports connect split claims with bounded paths through G_atomic.

use debundle_e2e_support::*;
use serde_json::Value;
use spec::{BindingSelector, Member as SpecMember, RawMemberSelector};
use std::fmt::Write as _;

fn conflict_witness(rejected: &RejectedFixture, expected_candidate_status: &str) -> Value {
    let assessment = rejected.candidate_relocation_assessment();
    assert_eq!(
        assessment["status"], expected_candidate_status,
        "unexpected candidate relocation assessment: {assessment:#}"
    );
    let conflicts = rejected.atomic_group_conflicts();
    assert_eq!(conflicts.len(), 1, "{conflicts:#?}");
    conflicts.into_iter().next().unwrap()["witness"].clone()
}

#[test]
fn eager_cycle_witness_keeps_directed_initialization_dependencies_forward() {
    let rejected = run_rejection_fixture(FixtureOpts::new(
        "const alpha = beta;\nconst beta = alpha;\n",
        vec![
            logical_module("left", &[Member::new("alpha")]),
            logical_module("right", &[Member::new("beta")]),
        ],
    ));
    let witness = conflict_witness(&rejected, "blocked_by_effect");
    let claims = witness["claim_pair"].as_array().unwrap();
    assert_eq!(claims.len(), 2, "{witness}");
    assert_ne!(claims[0]["module"], claims[1]["module"], "{witness}");

    let paths = witness["paths"].as_array().unwrap();
    assert_eq!(paths.len(), 2, "{witness}");
    for (index, path) in paths.iter().enumerate() {
        assert_eq!(path["from_source_unit"], claims[index]["source_unit"]);
        assert_eq!(path["to_source_unit"], claims[1 - index]["source_unit"]);
        assert_eq!(path["total_step_count"], 1, "{path}");
        assert_eq!(path["truncated"], false, "{path}");
        let steps = path["steps"].as_array().unwrap();
        assert_eq!(steps.len(), 1, "{path}");
        let step = &steps[0];
        assert_eq!(step["projection"], "directed_init_order", "{step}");
        assert_eq!(step["orientation"], "forward", "{step}");
        assert_eq!(step["edge"]["edge_kind"], "eager_use", "{step}");
        assert_eq!(step["edge"]["source"], path["from_source_unit"]);
        assert_eq!(step["edge"]["target"], path["to_source_unit"]);
    }
}

#[test]
fn symmetric_rebind_path_excludes_lazy_and_hoisted_uses() {
    let rejected = run_rejection_fixture(FixtureOpts::new(
        "let alpha = beta;\nfunction beta() { alpha = 1; return alpha; }\n",
        vec![
            logical_module("left", &[Member::new("alpha")]),
            logical_module("right", &[Member::new("beta")]),
        ],
    ));
    let witness = conflict_witness(&rejected, "blocked_by_placement");
    let paths = witness["paths"].as_array().unwrap();
    assert_eq!(paths.len(), 2, "{witness}");
    let expected_orientations = ["reverse", "forward"];
    for (path, orientation) in paths.iter().zip(expected_orientations) {
        let steps = path["steps"].as_array().unwrap();
        assert_eq!(steps.len(), 1, "{path}");
        let step = &steps[0];
        assert_eq!(step["projection"], "symmetric_colocation", "{step}");
        assert_eq!(step["orientation"], orientation, "{step}");
        assert_eq!(step["edge"]["edge_kind"], "lazy_rebind", "{step}");
        assert_eq!(step["edge"]["binding"], "alpha", "{step}");
        assert_ne!(step["edge"]["edge_kind"], "lazy_use", "{step}");
        assert_ne!(step["edge"]["edge_kind"], "hoisted_use", "{step}");
    }
}

#[test]
fn long_witness_paths_report_total_length_and_truncate_steps() {
    const SOURCE_UNIT_COUNT: usize = 40;
    const SPLIT_AT: usize = 20;
    let mut source = String::new();
    let mut modules = vec![logical_module("left", &[]), logical_module("right", &[])];
    for index in 0..SOURCE_UNIT_COUNT {
        let next = (index + 1) % SOURCE_UNIT_COUNT;
        writeln!(source, "const a{index} = a{next};").unwrap();
        let module = if index == SPLIT_AT {
            &mut modules[1].1
        } else {
            &mut modules[0].1
        };
        let name = format!("a{index}");
        module.members.push(SpecMember {
            name: Some(name.clone()),
            selector: RawMemberSelector {
                binding: Some(BindingSelector { name, kind: None }),
                ..Default::default()
            },
        });
    }

    let rejected = run_rejection_fixture(
        FixtureOpts::new(&source, modules).with_unassigned_mode(unassigned_mode_inline()),
    );
    let witness = conflict_witness(&rejected, "blocked_by_effect");
    let claims = witness["claim_pair"].as_array().unwrap();
    assert_eq!(claims[0]["source_unit"], "unit:0", "{witness}");
    assert_eq!(claims[1]["source_unit"], "unit:20", "{witness}");
    let paths = witness["paths"].as_array().unwrap();
    assert_eq!(paths.len(), 2, "{witness}");
    for path in paths {
        assert_eq!(path["total_step_count"], 20, "{path}");
        assert_eq!(path["truncated"], true, "{path}");
        let steps = path["steps"].as_array().unwrap();
        assert_eq!(steps.len(), 12, "{path}");
        // Sequencing constraints can tie the eager-read chain for shortest
        // length. Both are valid forward arcs in the atomic projection.
        assert!(
            steps.iter().all(|step| {
                step["projection"] == "directed_init_order"
                    && step["orientation"] == "forward"
                    && step["edge"]["constrains_init_order"] == true
                    && matches!(
                        step["edge"]["edge_kind"].as_str(),
                        Some("eager_use" | "sequenced")
                    )
            }),
            "{path}"
        );
    }
}
