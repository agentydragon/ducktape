//! `global_resolution.json`: every identifier the prepared parse left unbound
//! must still resolve to no binding in the final emitted files. Report-only;
//! the legacy gate keeps acceptance authority.

use debundle_e2e_support::*;
use serde_json::Value;

fn chunk<'a>(report: &'a Value, chunk_id: &str) -> &'a Value {
    &report["chunks"][chunk_id]
}

fn finding_reasons(chunk: &Value) -> Vec<&str> {
    chunk["findings"]
        .as_array()
        .expect("findings array")
        .iter()
        .map(|finding| finding["reason"].as_str().expect("typed reason"))
        .collect()
}

#[test]
fn parameter_default_global_stays_unbound_after_module_move() {
    // The parameter default reads the global `undefined`; the body's `var`
    // does not scope over it. Moving `read` must keep that distinction.
    let source = "function read(value = typeof undefined) {\n\
  var undefined = 1;\n\
  return value + ':' + typeof undefined;\n\
}\n\
console.log(read());\n\
export { read };\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("a_reader", &[Member::new("read")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "undefined:number\n");
    assert_global_resolution_preserved(&fixture);
    // `undefined` in the default and `console`.
    let report = fixture.global_resolution();
    assert_eq!(
        chunk(&report, &fixture.chunk_id)["required_use_count"],
        2,
        "{report:#}"
    );
}

#[test]
fn transformed_dynamic_eval_scope_is_incomplete() {
    // Direct `eval` resolves through the lexical scope at run time; the
    // resolver cannot prove that scope once the body is transformed. A1
    // refuses it at admission; the override keeps the resolver running.
    let source =
        "function read() { return eval('1 + 1'); }\nconsole.log(read());\nexport { read };\n";
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![logical_module("a_reader", &[Member::new("read")])],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_admission_overrides(&["a1_eval"]),
    );
    assert_preserved_entry_output(&fixture, "2\n");
    let report = fixture.global_resolution();
    let main = chunk(&report, &fixture.chunk_id);
    assert_eq!(report["status"], "incomplete", "{report:#}");
    assert_eq!(main["status"], "incomplete", "{report:#}");
    assert!(
        finding_reasons(main).contains(&"dynamic_scope"),
        "{report:#}"
    );
}
