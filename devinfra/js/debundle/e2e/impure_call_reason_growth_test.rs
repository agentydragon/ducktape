//! A known impure callee should explain each call site once. Copying the
//! callee's full reason list makes a branching call graph grow exponentially.

use debundle_e2e_support::*;
use std::collections::BTreeSet;

fn reachable_assignments<'a>(
    graph: &'a analysis::DependencyGraphReport,
    root: &'a analysis::Purity,
) -> Vec<&'a analysis::PurityReason> {
    let mut pending = vec![root];
    let mut seen = BTreeSet::new();
    let mut assignments = Vec::new();
    while let Some(purity) = pending.pop() {
        let analysis::Purity::NotPure { reasons } = purity else {
            continue;
        };
        for reason in reasons {
            if reason.rule == analysis::PurityRule::AssignOrUpdate {
                assignments.push(reason);
            }
            if let Some(id) = &reason.cause_ref {
                if !seen.insert(id.as_str()) {
                    continue;
                }
                let cause = graph
                    .purity_causes
                    .iter()
                    .find(|cause| &cause.id == id)
                    .expect("cause reference resolves in the report");
                pending.push(&cause.purity);
            }
        }
    }
    assignments.sort_by_key(|reason| reason.source_location.as_ref().map(|loc| loc.start_line));
    assignments
}

const FUNCTIONS: &str = r#"function a() { globalThis.value = 1; return 0; }
function z() { globalThis.other = 2; return 0; }
function b() { a(); z(); a(); return 0; }
function c() { b(); b(); return 0; }
function d() { c(); c(); return 0; }
function e() { d(); d(); return 0; }
function foo() { e(); e(); return 0; }
"#;

#[test]
fn local_impure_call_reports_its_own_site_once() {
    let source =
        format!("{FUNCTIONS}const result = foo();\nconsole.log(result);\nexport {{ result }};\n");
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module("result_module", &[Member::new("result")])],
    ));

    let graph = fixture.dependency_graph();
    let source_unit = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("result source_unit");
    let purity = serde_json::to_value(&source_unit.purity).expect("serialize purity");
    let reasons = purity["reasons"].as_array().expect("impure call reasons");
    assert_eq!(
        reasons.len(),
        1,
        "one reason at the local call site: {purity}"
    );
    assert_eq!(reasons[0]["rule"], "impure_function_call");
    assert_eq!(reasons[0]["source_location"]["start_line"], 8);
    assert!(
        graph.purity_causes.len() <= 7,
        "shared causes should stay linear"
    );
    let assignments = reachable_assignments(&graph, &source_unit.purity);
    assert_eq!(
        assignments
            .iter()
            .map(|reason| reason.source_location.as_ref().unwrap().start_line)
            .collect::<Vec<_>>(),
        vec![1, 2],
    );
}

#[test]
fn imported_impure_call_reports_its_own_site_once() {
    let lib = format!("{FUNCTIONS}export {{ foo }};\n");
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import { foo } from "./lib.js";
const result = foo();
console.log(result);
export { result };
"#,
            vec![logical_module("result_module", &[Member::new("result")])],
        )
        .with_extra_chunks(&[("static/lib", &lib)]),
    );

    let graph = fixture.dependency_graph();
    let source_unit = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("result source_unit");
    let purity = serde_json::to_value(&source_unit.purity).expect("serialize purity");
    let reasons = purity["reasons"].as_array().expect("impure call reasons");
    assert_eq!(
        reasons.len(),
        1,
        "one reason at the imported call site: {purity}"
    );
    assert_eq!(reasons[0]["rule"], "impure_function_call");
    assert_eq!(
        reasons[0]["source_location"]["source_path"],
        "static/app.js"
    );
    assert_eq!(reasons[0]["source_location"]["start_line"], 2);
    assert!(
        graph.purity_causes.len() <= 7,
        "shared causes should stay linear"
    );
    let assignments = reachable_assignments(&graph, &source_unit.purity);
    assert_eq!(
        assignments
            .iter()
            .map(|reason| reason.source_location.as_ref().unwrap().start_line)
            .collect::<Vec<_>>(),
        vec![1, 2],
    );
    assert!(
        assignments
            .iter()
            .all(|reason| reason.source_location.as_ref().unwrap().source_path == "static/lib.js")
    );
}

fn assert_recursive_import_effects(effect: &str) {
    let second = format!(
        "import {{ first }} from './first.js';\n\
         export function second(stop) {{\n{effect}\n\
         return stop === true ? 8 : first(true);\n}}\n"
    );
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { first } from './first.js';\n\
             const result = first(false);\nconsole.log(result);\nexport { result };\n",
            vec![logical_module("result_module", &[Member::new("result")])],
        )
        .with_extra_chunks(&[
            (
                "static/first",
                "import { second } from './second.js';\n\
                 export function first(stop) { return stop === true ? 7 : second(true); }\n",
            ),
            ("static/second", &second),
        ]),
    );
    assert_preserved_entry_output(&fixture, "8\n");
    let graph = fixture.dependency_graph();
    let result = source_unit_for_binding(&graph, "result");
    let purity = &graph
        .nodes
        .iter()
        .find(|node| node.id == result)
        .unwrap()
        .purity;
    assert_eq!(purity.is_pure(), effect.is_empty());
    if !effect.is_empty() {
        let assignments = reachable_assignments(&graph, purity);
        assert!(
            assignments.iter().any(|reason| reason
                .source_location
                .as_ref()
                .is_some_and(|location| location.source_path == "static/second.js"
                    && location.start_line == 3)),
            "the recursive call's explanation must reach its provider's direct effect"
        );
        assert!(
            graph.purity_causes.len() <= 4,
            "recursive causes stay finite"
        );
    }
}

#[test]
fn cross_module_recursive_functions_without_effects_are_pure() {
    assert_recursive_import_effects("");
}

#[test]
fn cross_module_recursive_effects_retain_the_provider_witness() {
    assert_recursive_import_effects("globalThis.observed = true;");
}
