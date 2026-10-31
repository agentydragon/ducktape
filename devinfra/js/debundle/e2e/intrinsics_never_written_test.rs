//! `program_premises.intrinsics_never_written` keeps the named families'
//! slots intact against census writes it overrides, which the program graph
//! lists, and is refuted by a write on a named family's exact object.

use debundle_e2e_support::*;

/// zod's shape: a descriptor installed on an instance's prototype under a
/// caller-supplied key, so the receiver may be any native object.
const INSTALL: &str = "function install(instance, name, value) { Object.defineProperty(Object.getPrototypeOf(instance), name, { value }); }";

fn source(extra: &str) -> String {
    format!(
        "globalThis.saved = () => later;\n{INSTALL}\n{extra}const collection = new Map();\nconst later = 42;\nconsole.log(collection.size, later);\n"
    )
}

fn opts<'a>(source: &'a str, members: &[&'static str], families: Option<&str>) -> FixtureOpts<'a> {
    let mut native: Vec<Member> = members.iter().map(|member| Member::new(member)).collect();
    native.push(Member::new("collection"));
    let opts = FixtureOpts::new(
        source,
        vec![
            logical_module_with_anon(
                "values/native",
                &native,
                &["globalThis.saved = () => later;"],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    );
    match families {
        Some(families) => opts.with_program_premises(
            serde_yaml::from_str(&format!(
                "intrinsics_never_written:\n  families: {families}\n  note: audited\n"
            ))
            .expect("program premises"),
        ),
        None => opts,
    }
}

#[test]
fn named_families_stay_intact_and_overrides_are_listed() {
    let source = source("");
    for families in ["all", "[Map]"] {
        let fixture = run_fixture(opts(&source, &["install"], Some(families)));
        assert_preserved_entry_output(&fixture, "0 42\n");
        assert_all_emitted_js_checks(&fixture);
        let graph = fixture.dependency_graph();
        let node_id = source_unit_for_binding(&graph, "collection");
        let node = graph.nodes.iter().find(|node| node.id == node_id).unwrap();
        let report = serde_json::to_value(node).unwrap();
        assert_eq!(
            report["synchronous_execution"]["native_integrity_uses"][0]["program_premises"],
            serde_json::json!(["intrinsics_never_written"]),
            "{report}"
        );
        let program = serde_json::to_value(fixture.program_graph()).unwrap();
        let overrides = program["intrinsics_never_written_overrides"]
            .as_array()
            .unwrap();
        assert!(
            overrides
                .iter()
                .any(|entry| entry["reach"] == "unknown_receiver"
                    && entry["families"]
                        .as_array()
                        .unwrap()
                        .contains(&serde_json::json!("map"))),
            "{program}"
        );
    }
}

#[test]
fn without_the_premise_or_naming_another_family_map_stays_unknown() {
    let source = source("");
    for families in [None, Some("[Array]")] {
        expect_atomic_conflict_rejection(
            opts(&source, &["install"], families),
            &["values/native", "values/later"],
            &[],
        );
    }
}

#[test]
fn a_write_on_a_named_familys_exact_object_refutes_the_premise() {
    for (patch, families) in [
        (
            "function patch(value) { Map.prototype.extra = value; }\n",
            "[Map]",
        ),
        ("function patch(value) { Map.groupBy = value; }\n", "all"),
        (
            "function patch(value) { globalThis.Map = value; }\n",
            "[Map]",
        ),
    ] {
        let source = source(patch);
        expect_rejection_containing_all(
            opts(&source, &["install", "patch"], Some(families)),
            &["intrinsics_never_written", "exact objects"],
        );
    }
}
