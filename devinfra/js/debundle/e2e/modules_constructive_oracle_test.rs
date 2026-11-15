//! Compare complete peeling with raw source-unit placements accepted by the
//! normal transform pipeline and Node, across real eager, lazy and mutation
//! shapes.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::PathBuf;

const DESTINATIONS: [&str; 4] = ["stay", "middle", "a_before", "z_after"];

#[derive(Clone, Copy)]
enum Selector {
    Binding(&'static str),
    Anonymous(&'static str),
}

struct Family {
    name: &'static str,
    source: &'static str,
    expected: &'static str,
    units: [Selector; 3],
}

#[derive(Default)]
struct ModuleDoc<'a> {
    members: Vec<&'a str>,
    anonymous_statements: Vec<&'a str>,
}

#[derive(Debug)]
struct AcceptedModel {
    destinations: [u8; 3],
    moved_units: u8,
    fresh_owners: usize,
}

fn families() -> [Family; 4] {
    [
        Family {
            name: "eager chain",
            source: "const anchor = 'eager';\nconst first = 2;\nconst second = first + 11;\nconst third = second + 100;\nconsole.log(anchor, first, second, third);\n",
            expected: "eager 2 13 113\n",
            units: [
                Selector::Binding("first"),
                Selector::Binding("second"),
                Selector::Binding("third"),
            ],
        },
        Family {
            name: "lazy call chain",
            source: "const anchor = 'lazy';\nfunction first() { return second() + 2; }\nfunction second() { return 5; }\nconst third = first() + second();\nconsole.log(anchor, first(), second(), third);\n",
            expected: "lazy 7 5 12\n",
            units: [
                Selector::Binding("first"),
                Selector::Binding("second"),
                Selector::Binding("third"),
            ],
        },
        Family {
            name: "anonymous rebind",
            source: "const anchor = 'mutation';\nlet counter = 3;\ncounter += 4;\nfunction read() { return counter * 10; }\nconsole.log(anchor, counter, read());\n",
            expected: "mutation 7 70\n",
            units: [
                Selector::Binding("counter"),
                Selector::Anonymous("counter += 4;"),
                Selector::Binding("read"),
            ],
        },
        Family {
            name: "at-init callback propagation",
            source: "const anchor = 'callback';\nconst seed = 17;\nfunction read() { return seed + 1; }\nconst value = [read].map((fn) => fn())[0];\nconsole.log(anchor, seed, value);\n",
            expected: "callback 17 18\n",
            units: [
                Selector::Binding("seed"),
                Selector::Binding("read"),
                Selector::Binding("value"),
            ],
        },
    ]
}

#[test]
fn complete_matches_raw_runtime_accepted_placements_for_required_subsets_and_owner_caps() {
    for family in families() {
        check_family(family);
    }
}

fn check_family(family: Family) {
    let fixture = GraphFixture::new(
        family.source,
        &[(
            "middle.yaml",
            "members: [{selector: {binding: {name: anchor}}}]\n",
        )],
    );
    let baseline = read_tree(&fixture.modules);
    let units: Vec<_> = family
        .units
        .iter()
        .map(|selector| match selector {
            Selector::Binding(binding) => fixture.source_unit_for_binding(binding),
            Selector::Anonymous(_) => fixture.source_unit_for_line(3),
        })
        .collect();
    assert_eq!(units.len(), 3);
    assert_eq!(
        units.iter().collect::<BTreeSet<_>>().len(),
        3,
        "{} must expose three independent raw source units: {units:?}",
        family.name
    );

    // Enumerate the four raw choices per statement directly. The only
    // acceptance oracle is current_spec_runs: normal selector resolution,
    // the complete transform, and Node execution. Planner atoms, closures,
    // checker results and search pruning do not shape this candidate set.
    let mut accepted = Vec::new();
    for encoded in 0u8..64 {
        let mut cursor = encoded;
        let mut destinations = [0u8; 3];
        for destination in &mut destinations {
            *destination = cursor % DESTINATIONS.len() as u8;
            cursor /= DESTINATIONS.len() as u8;
        }

        restore_modules(&fixture, &baseline);
        write_assignment(&fixture, &family.units, &destinations);
        if fixture.current_spec_runs(family.expected) {
            let moved_units = destinations
                .iter()
                .enumerate()
                .fold(0u8, |mask, (index, destination)| {
                    mask | if *destination == 0 { 0 } else { 1 << index }
                });
            let fresh_owners = [2u8, 3u8]
                .iter()
                .filter(|owner| destinations.contains(owner))
                .count();
            accepted.push(AcceptedModel {
                destinations,
                moved_units,
                fresh_owners,
            });
        }
    }
    restore_modules(&fixture, &baseline);
    assert!(
        accepted.iter().any(|model| model.moved_units == 0),
        "{} baseline should be runtime accepted",
        family.name
    );

    let only_path = fixture.root().join("only-units.json");
    fs::write(&only_path, serde_json::to_vec(&units).unwrap()).unwrap();
    let mut validated_witness = false;

    for required_mask in 1u8..8 {
        for owner_cap in 0usize..=2 {
            let expected_found = accepted.iter().any(|model| {
                model.moved_units & required_mask == required_mask
                    && model.fresh_owners <= owner_cap
            });
            let mut args = vec![
                "modules".to_string(),
                "propose".to_string(),
                "--strategy".to_string(),
                "complete".to_string(),
                "--only-units".to_string(),
                only_path.to_string_lossy().into_owned(),
            ];
            for (index, unit) in units.iter().enumerate() {
                if required_mask & (1 << index) != 0 {
                    args.extend(["--require".to_string(), unit.clone()]);
                }
            }
            if owner_cap == 0 {
                // The CLI requires an explicit destination policy when a
                // zero fresh-owner cap is combined with --into.
                args.extend([
                    "--destinations".to_string(),
                    "existing".to_string(),
                    "--into".to_string(),
                    "middle".to_string(),
                ]);
            } else {
                for destination in ["middle", "a_before", "z_after"] {
                    args.extend(["--into".to_string(), destination.to_string()]);
                }
            }
            args.extend([
                "--max-new-modules".to_string(),
                owner_cap.to_string(),
                "--format".to_string(),
                "json".to_string(),
            ]);
            let arg_refs: Vec<_> = args.iter().map(String::as_str).collect();
            let output = fixture.command(&arg_refs);
            assert!(
                output.status.success(),
                "{} required={required_mask:03b} cap={owner_cap}: {}",
                family.name,
                String::from_utf8_lossy(&output.stderr)
            );
            let report = parse_stdout_json(&output);
            assert_eq!(
                report["status"],
                if expected_found {
                    "found"
                } else {
                    "exhausted_no_peel"
                },
                "{} required={required_mask:03b} cap={owner_cap}: {report}",
                family.name
            );
            assert_eq!(
                read_tree(&fixture.modules),
                baseline,
                "{} proposal must preserve the authored module tree",
                family.name
            );

            if expected_found {
                let plan = &report["plan"];
                let planned = model_from_plan(plan, &units);
                let planned_model = accepted
                    .iter()
                    .find(|model| model.destinations == planned)
                    .unwrap_or_else(|| {
                        panic!(
                            "{} plan is absent from the raw runtime-accepted models: {plan}",
                            family.name
                        )
                    });
                assert_ne!(planned_model.moved_units, 0, "{plan}");
                assert_eq!(
                    planned_model.moved_units & required_mask,
                    required_mask,
                    "{plan}"
                );
                assert!(planned_model.fresh_owners <= owner_cap, "{plan}");

                if !validated_witness {
                    let plan_path = fixture.root().join("constructive-plan.json");
                    fs::write(&plan_path, serde_json::to_vec(plan).unwrap()).unwrap();
                    let plan_arg = plan_path.to_string_lossy().into_owned();
                    let check_args = ["modules", "check", "--plan", plan_arg.as_str()];
                    fixture.assert_success(&check_args);
                    assert_eq!(read_tree(&fixture.modules), baseline);
                    let preview_args =
                        ["modules", "apply", "--plan", plan_arg.as_str(), "--dry-run"];
                    fixture.assert_success(&preview_args);
                    assert_eq!(read_tree(&fixture.modules), baseline);
                    let apply_args = ["modules", "apply", "--plan", plan_arg.as_str()];
                    fixture.assert_success(&apply_args);
                    fixture.assert_runs(family.expected);
                    restore_modules(&fixture, &baseline);
                    validated_witness = true;
                }
            } else {
                assert!(report["plan"].is_null(), "{report}");
            }
        }
    }
    assert!(
        validated_witness,
        "{} should have a found witness",
        family.name
    );
}

fn write_assignment(fixture: &GraphFixture, selectors: &[Selector; 3], destinations: &[u8; 3]) {
    let mut docs = BTreeMap::<&str, ModuleDoc<'_>>::new();
    docs.entry("middle").or_default().members.push("anchor");
    for (selector, destination) in selectors.iter().zip(destinations) {
        if *destination == 0 {
            continue;
        }
        let document = docs.entry(DESTINATIONS[*destination as usize]).or_default();
        match selector {
            Selector::Binding(binding) => document.members.push(binding),
            Selector::Anonymous(statement) => document.anonymous_statements.push(statement),
        }
    }

    for (module, document) in docs {
        let mut yaml = String::new();
        if !document.members.is_empty() {
            yaml.push_str("members:\n");
            for binding in document.members {
                yaml.push_str(&format!("  - selector: {{binding: {{name: {binding}}}}}\n"));
            }
        }
        if !document.anonymous_statements.is_empty() {
            yaml.push_str("anonymous_statements:\n");
            for statement in document.anonymous_statements {
                yaml.push_str(&format!("  - match: '{statement}'\n"));
            }
        }
        fs::write(fixture.modules.join(format!("{module}.yaml")), yaml).unwrap();
    }
}

fn model_from_plan(plan: &Value, units: &[String]) -> [u8; 3] {
    let mut destinations = [0u8; 3];
    for change in plan["changes"].as_array().expect("plan changes") {
        let unit = change["unit_id"].as_str().expect("unit id");
        let index = units
            .iter()
            .position(|candidate| candidate == unit)
            .unwrap_or_else(|| panic!("plan changed a unit outside --only-units: {change}"));
        let module = change["module"].as_str().expect("module destination");
        destinations[index] = match module {
            "middle" => 1,
            "a_before" => 2,
            "z_after" => 3,
            _ => panic!("plan chose a destination outside the requested scope: {change}"),
        };
    }
    destinations
}

fn restore_modules(fixture: &GraphFixture, baseline: &BTreeMap<PathBuf, Vec<u8>>) {
    for path in read_tree(&fixture.modules).keys() {
        if !baseline.contains_key(path) {
            fs::remove_file(fixture.modules.join(path)).unwrap();
        }
    }
    for (path, bytes) in baseline {
        fs::write(fixture.modules.join(path), bytes).unwrap();
    }
}
