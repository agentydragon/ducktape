//! Effect translation validation: the `Array.prototype` iteration rows
//! (`forEach`, `map`, `filter`, `reduce` with an initial value) in a
//! kernel-run body. The method is selected at GetValue, the receiver is
//! rechecked after the arguments and after every callback, and the callback
//! runs once per element.

use debundle_e2e_graph_support::effect_validation::*;
use debundle_e2e_support::*;
use serde_json::Value;

const RELOCATION: &str =
    "const independent = 7;\nvar result = run();\nconsole.log(independent, result.join(','));\n";

fn assert_commuting_relocation(fixture: &Fixture, case: &str) {
    let report = fixture.effect_translation_validation();
    assert!(
        report["swapped_pairs"].as_u64().unwrap() > 0,
        "{case}: {report:#}"
    );
    assert_eq!(
        report["decision"]["status"], "accepted",
        "{case}: {report:#}"
    );
    assert_eq!(
        report["decision"]["scope"], "statement_order_commutation",
        "{case}: {report:#}"
    );
    assert_effect_verdict_accepts(fixture, case);
}

/// The root binding's receipt names the native row as one of its call edges.
fn assert_iteration_receipt(fixture: &Fixture, member: &str, case: &str) {
    let execution = binding_execution_report(fixture, "result");
    let authority = &execution["binding_effect_authority"];
    assert_eq!(
        authority["authority"], "operation_receipt",
        "{case}: {execution:#}"
    );
    let property = format!("prototype.{member}");
    assert!(
        authority["receipt"]["calls"]
            .as_array()
            .unwrap()
            .iter()
            .any(|call| call["target"]["kind"] == "native"
                && call["target"]["intrinsic"] == "array"
                && call["target"]["property"] == property.as_str()),
        "{case}: the receipt must name {property}: {execution:#}"
    );
}

fn array_iteration_refusals(fixture: &Fixture, binding: &str) -> Vec<Value> {
    binding_execution_report(fixture, binding)["precision_losses"]
        .as_array()
        .expect("precision losses")
        .iter()
        .filter(|loss| loss["reason"]["operation"] == "array_iteration")
        .map(|loss| loss["reason"]["ordered_call_admission_refusal"].clone())
        .collect()
}

#[test]
fn iteration_rows_keep_receipts_across_an_independent_inversion() {
    for (declarations, members, member, expected) in [
        (
            "function run() { const items = [1, 2, 3]; return items.map((x) => x * 2); }",
            &["run", "result"][..],
            "map",
            "7 2,4,6\n",
        ),
        (
            "function double(x) { return x * 2; }\nfunction run() { const items = [1, 2, 3]; return items.map(double); }",
            &["double", "run", "result"][..],
            "map",
            "7 2,4,6\n",
        ),
        (
            "function run() { const items = [1, 2, 3]; return items.filter((x) => x > 1); }",
            &["run", "result"][..],
            "filter",
            "7 2,3\n",
        ),
        (
            "function run() { const items = [1, 2, 3]; return [items.reduce((sum, x) => sum + x, 0)]; }",
            &["run", "result"][..],
            "reduce",
            "7 6\n",
        ),
        (
            "const out = [];\nfunction record(x) { out.push(x * 10); }\nfunction run() { const items = [1, 2]; items.forEach(record); return out; }",
            &["out", "record", "run", "result"][..],
            "forEach",
            "7 10,20\n",
        ),
    ] {
        let source = format!("{declarations}\n{RELOCATION}");
        let members: Vec<Member> = members.iter().map(|name| Member::new(name)).collect();
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module("run", &members)],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_iteration_receipt(&fixture, member, declarations);
        assert_commuting_relocation(&fixture, declarations);
    }
}

/// Run a rejected split's original and emitted programs: the original prints
/// `original`, the emitted layout `emitted`, so the refusal is required.
fn assert_rejected_split_diverges(
    source: &str,
    members: &[&'static str],
    original: &str,
    emitted: &str,
) {
    let members: Vec<Member> = members.iter().map(|name| Member::new(name)).collect();
    let rejected = run_emit_on_rejection_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("reader", &[Member::new("observed")]),
                logical_module("run", &members),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    let report = rejected.translation_validation();
    assert_eq!(report["gate_accepts"], false, "{source}: {report:#}");
    assert_eq!(report["accepts"], false, "{source}: {report:#}");
    assert_node_output(
        &rejected
            .source_root
            .join(format!("{}.js", rejected.chunk_id)),
        original,
        "",
    );
    assert_node_output(&rejected.entry_path(), emitted, "");
}

/// Opposing control: each callback invocation's writes are the row's
/// effects, so a reader of the written array stays before the iteration.
#[test]
fn a_callback_write_keeps_its_reader_before_the_iteration() {
    assert_rejected_split_diverges(
        "const seen = [];\nfunction record(x) { seen.push(x); return x; }\nfunction run() { const items = [1, 2]; return items.map(record); }\nconst observed = seen.length;\nvar result = run();\nconsole.log(observed, result.join(','), seen.length);\n",
        &["seen", "record", "run", "result"],
        "0 1,2 2\n",
        "2 1,2 2\n",
    );
}

/// Opposing control: a replaced `Array.prototype` method is not the row.
/// Moving the call before the replacement runs the original method instead.
#[test]
fn a_replaced_prototype_method_is_not_selected() {
    assert_rejected_split_diverges(
        "const log = [];\nArray.prototype.forEach = function () { log.push('patched'); };\nfunction run() { const items = [1, 2]; items.forEach((x) => x); return items; }\nconst observed = log.length;\nvar result = run();\nconsole.log(observed, log.length);\n",
        &["log", "run", "result"],
        "0 1\n",
        "0 0\n",
    );
}

/// Opposing control: an element getter makes the receiver unsettled, so the
/// getter's write is not hidden behind the element read.
#[test]
fn an_element_getter_keeps_the_receiver_unproved() {
    assert_rejected_split_diverges(
        "const log = [];\nfunction run() { const items = [1, 2]; Object.defineProperty(items, 0, { get() { log.push('get'); return 1; } }); items.forEach((x) => x); return items; }\nconst observed = log.length;\nvar result = run();\nconsole.log(observed, log.length);\n",
        &["log", "run", "result"],
        "0 1\n",
        "1 1\n",
    );
}

/// Opposing control: a hole reaches the prototype chain at HasProperty, so a
/// holey receiver has no dense snapshot. Moving the call before an
/// `Object.prototype` getter for that index skips the getter.
#[test]
fn a_hole_keeps_its_prototype_read() {
    assert_rejected_split_diverges(
        "const log = [];\nObject.defineProperty(Object.prototype, 1, { get() { log.push('hole'); return 2; }, configurable: true });\nfunction run() { const items = [1, , 3]; items.forEach((x) => x); return items; }\nconst observed = log.length;\nvar result = run();\nconsole.log(observed, log.length);\n",
        &["log", "run", "result"],
        "0 1\n",
        "0 0\n",
    );
}

/// Valid splits the rows refuse by typed reason today. Call-effect coverage
/// for a callback with live captures is Wave 3 row 12; the early-exit rows
/// need a fork per callback result.
#[test]
fn capturing_and_early_exit_callbacks_are_pending_with_typed_refusals() {
    for (body, expected, refusal, capability) in [
        (
            "const items = [1, 2, 3]; let total = 0; items.forEach((x) => { total += x; }); return [total];",
            "7 6\n",
            "array_iteration_capturing_callback",
            "call-effect coverage for a callback with live captures",
        ),
        (
            "const items = [1, 2, 3]; return [items.some((x) => x > 1)];",
            "7 true\n",
            "array_iteration_row_unsupported",
            "an early-exit row forking on each callback result",
        ),
    ] {
        let source = format!("function run() {{ {body} }}\n{RELOCATION}");
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "run",
                &[Member::new("run"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_effect_coverage_pending(&fixture, "result", "array_iteration", capability);
        assert_eq!(
            array_iteration_refusals(&fixture, "result"),
            vec![Value::from(refusal)],
            "{body}"
        );
    }
}

/// A row nested in the callback of a legacy row stays legacy: the legacy row
/// invokes its callback with `array_iteration_depth > 0`, and only a
/// depth-zero invocation takes the kernel prologue. The split is valid;
/// it becomes provable when the outer row runs in the kernel (L4e).
#[test]
fn a_row_inside_a_legacy_row_callback_stays_legacy() {
    let source = "function wrap(x) { const inner = [x]; return inner.map((y) => y * 2); }\nconst outer = [1, 2];\nconst independent = 7;\nvar result = outer.map(wrap);\nconsole.log(independent, result.join(','));\n";
    let fixture = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "run",
            &[
                Member::new("wrap"),
                Member::new("outer"),
                Member::new("result"),
            ],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 2,4\n");
    assert_effect_verdict_pending(&fixture, "incomplete", "nested legacy row");
}

/// Each refusal the row makes after selecting its method is typed. The
/// split stays valid (Node parity) and the root's coverage stays open.
/// `array_iteration_intrinsics_changed` needs a modeled `Array` intrinsic
/// write between GetValue and the call or inside a callback; every lowered
/// form of such a write refuses earlier, so no fixture reaches it today.
#[test]
fn row_refusals_carry_their_typed_reasons() {
    for (body, expected, refusal) in [
        (
            "const items = [1, 2]; return items.flatMap((x) => [x, x]);",
            "7 1,1,2,2\n",
            "array_iteration_row_unsupported",
        ),
        (
            "const items = [1, 2]; return [items.reduce((sum, x) => sum + x)];",
            "7 3\n",
            "array_iteration_row_unsupported",
        ),
        (
            "const items = [1, 2]; items.forEach((x) => x, null); return items;",
            "7 1,2\n",
            "array_iteration_row_unsupported",
        ),
        (
            "const items = [1, 2]; const pending = items.map(async (x) => x); return [pending.length];",
            "7 2\n",
            "array_iteration_callback_unsupported",
        ),
        (
            "const items = [1, 2]; return items.map((x) => { console.log(x); return x; });",
            "1\n2\n7 1,2\n",
            "array_iteration_callback_incomplete",
        ),
        (
            "const items = [1, 2]; return items.map((x, index, array) => { array.push(0); return x; });",
            "7 1,2\n",
            "array_iteration_receiver_unproved",
        ),
    ] {
        let source = format!("function run() {{ {body} }}\n{RELOCATION}");
        let fixture = run_fixture(FixtureOpts::new(
            &source,
            vec![logical_module(
                "run",
                &[Member::new("run"), Member::new("result")],
            )],
        ));
        assert_preserved_entry_output(&fixture, expected);
        assert_eq!(
            array_iteration_refusals(&fixture, "result"),
            vec![Value::from(refusal)],
            "{body}"
        );
    }
}

/// An empty receiver never invokes its callback; the row still commits its
/// fresh result.
#[test]
fn an_empty_receiver_keeps_its_receipt() {
    let source = format!(
        "function run() {{ const items = []; return items.map((x) => x * 2); }}\n{RELOCATION}"
    );
    let fixture = run_fixture(FixtureOpts::new(
        &source,
        vec![logical_module(
            "run",
            &[Member::new("run"), Member::new("result")],
        )],
    ));
    assert_preserved_entry_output(&fixture, "7 \n");
    assert_iteration_receipt(&fixture, "map", "empty receiver");
    assert_commuting_relocation(&fixture, "empty receiver");
}
