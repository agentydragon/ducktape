//! A known impure callee should explain each call site once. Copying the
//! callee's full reason list makes a branching call graph grow exponentially.

use debundle_e2e_support::*;

const FUNCTIONS: &str = r#"function a() { globalThis.value = 1; return 0; }
function b() { a(); a(); return 0; }
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

    let graph = fixture.owner_graph();
    let owner = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("result owner");
    let purity = serde_json::to_value(&owner.purity).expect("serialize purity");
    let reasons = purity["reasons"].as_array().expect("impure call reasons");
    assert_eq!(
        reasons.len(),
        1,
        "one reason at the local call site: {purity}"
    );
    assert_eq!(reasons[0]["rule"], "impure_function_call");
    assert_eq!(reasons[0]["source_location"]["start_line"], 7);
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

    let graph = fixture.owner_graph();
    let owner = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "result")
        })
        .expect("result owner");
    let purity = serde_json::to_value(&owner.purity).expect("serialize purity");
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
}
