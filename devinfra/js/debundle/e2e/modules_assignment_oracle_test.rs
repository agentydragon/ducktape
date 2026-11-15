//! Enumerate raw statement placements and compare the public checker with the
//! normal source/spec pipeline. This oracle uses neither atoms nor planner
//! contractions to choose the candidate space.

use debundle_e2e_graph_support::GraphFixture;
use debundle_e2e_support::{parse_stdout_json, read_tree};
use serde_json::json;
use std::collections::BTreeMap;
use std::fs;

#[test]
fn all_small_raw_assignments_agree_with_materialization_and_node() {
    for (body, second_binding, expected) in [
        (
            "const first = 2;\nconst second = first + 1;",
            Some("second"),
            "1 2 3\n",
        ),
        (
            "const first = 2;\nfunction second() { return first + 1; }",
            Some("second"),
            "1 2 3\n",
        ),
        (
            "let first = 2;\nfunction second() { first += 1; return first; }",
            Some("second"),
            "1 2 3\n",
        ),
        ("let first = 2;\nfirst += 1;", None, "1 3\n"),
    ] {
        let log = match second_binding {
            Some(_) if body.contains("function") => "console.log(anchor, first, second());",
            Some(_) => "console.log(anchor, first, second);",
            None => "console.log(anchor, first);",
        };
        let source = format!("const anchor = 1;\n{body}\n{log}\n");
        let fixture = GraphFixture::new(
            &source,
            &[(
                "middle.yaml",
                "members: [{selector: {binding: {name: anchor}}}]\n",
            )],
        );
        let baseline = read_tree(&fixture.modules);
        let units = [
            fixture.source_unit_for_line(2),
            fixture.source_unit_for_line(3),
        ];
        // Include extending an existing owner and both path orders of fresh
        // owners. Each statement can also remain residual independently.
        let destinations = [None, Some("a_before"), Some("middle"), Some("z_after")];
        for first in destinations {
            for second in destinations {
                let assignment = [first, second];
                let batch = assignment
                    .iter()
                    .enumerate()
                    .filter_map(|(index, module)| {
                        module.map(|module| json!({"unit_id": units[index], "module": module}))
                    })
                    .collect::<Vec<_>>();
                let batch_path = fixture.root().join("batch.json");
                fs::write(&batch_path, serde_json::to_vec(&batch).unwrap()).unwrap();
                let checked = fixture.command(&[
                    "modules",
                    "check",
                    "--batch",
                    batch_path.to_str().unwrap(),
                    "--format",
                    "json",
                ]);
                let report = parse_stdout_json(&checked);
                let accepted = report["accepted"].as_bool().unwrap();
                assert_eq!(checked.status.code(), Some(if accepted { 0 } else { 1 }));
                assert_eq!(read_tree(&fixture.modules), baseline);

                let mut docs = BTreeMap::<&str, (Vec<&str>, Vec<&str>)>::new();
                docs.entry("middle").or_default().0.push("anchor");
                for (index, module) in assignment.iter().enumerate() {
                    if let Some(module) = module {
                        let doc = docs.entry(module).or_default();
                        if index == 0 {
                            doc.0.push("first");
                        } else if let Some(binding) = second_binding {
                            doc.0.push(binding);
                        } else {
                            doc.1.push("first += 1;");
                        }
                    }
                }
                for (module, (bindings, statements)) in &docs {
                    let mut text = String::new();
                    if !bindings.is_empty() {
                        text.push_str("members:\n");
                        for binding in bindings {
                            text.push_str(&format!(
                                "  - selector: {{binding: {{name: {binding}}}}}\n"
                            ));
                        }
                    }
                    if !statements.is_empty() {
                        text.push_str("anonymous_statements:\n");
                        for statement in statements {
                            text.push_str(&format!("  - match: '{statement}'\n"));
                        }
                    }
                    fs::write(fixture.modules.join(format!("{module}.yaml")), text).unwrap();
                }
                let reference = fixture.current_spec_runs(expected);
                assert_eq!(accepted, reference, "source={source}, batch={batch:?}");
                for path in read_tree(&fixture.modules).keys() {
                    fs::remove_file(fixture.modules.join(path)).unwrap();
                }
                for (path, bytes) in &baseline {
                    fs::write(fixture.modules.join(path), bytes).unwrap();
                }
            }
        }
    }
}
