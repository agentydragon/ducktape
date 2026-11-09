//! Source/emitted controls for suspended calls and branch-local unwinding.

use analysis::execution_evidence::SkippedCallReason;
use analysis::reports::schema::BindingEffectAuthorityReport;
use debundle_e2e_graph_support::FixtureGraphExt;
use debundle_e2e_support::*;

#[test]
fn thrown_call_world_skips_suffix_and_enters_the_handler() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
let writes = 0;
let state = 0;
function choose(flag) {
  writes++;
  if (flag) { state = 1; throw 7; }
  state = 2;
  return 3;
}
function run(flag) {
  try {
    choose(flag);
    state = 4;
  } catch (error) {
    return state + error;
  }
  return state;
}
const thrown = run(true);
const normal = run(false);
console.log(thrown, normal, writes);
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "8 4 2\n");
}

#[test]
fn argument_binary_and_initializer_continuations_keep_completed_prefixes() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
let calls = 0;
function first() { calls++; return 2; }
function choose(flag) { calls++; if (flag) throw 7; return 3; }
function combine(left, right) { return left + right; }
function run(flag) {
  try {
    const result = combine(first(), choose(flag)) + first();
    return result;
  } catch (error) { return error; }
}
const thrown = run(true);
const afterThrow = calls;
const normal = run(false);
console.log(thrown, afterThrow, normal, calls);
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "7 2 7 5\n");
}

#[test]
fn selected_if_arms_account_only_the_call_in_the_unselected_arm() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
function consequent() { return "then"; }
function alternate() { return "else"; }
function choose(value) {
  if (value) { return consequent(); }
  else { return alternate(); }
}
const truthy = choose({});
const falsy = choose(null);
console.log(truthy, falsy);
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "then else\n");

    let graph = fixture.dependency_graph();
    for (binding, expected_reason) in [
        ("truthy", SkippedCallReason::IfAlternateNotSelected),
        ("falsy", SkippedCallReason::IfConsequentNotSelected),
    ] {
        let execution = graph
            .nodes
            .iter()
            .find(|node| {
                node.declared_bindings
                    .iter()
                    .any(|declared| declared.binding == binding)
            })
            .and_then(|node| node.synchronous_execution.as_ref())
            .unwrap_or_else(|| panic!("{binding} has no synchronous execution report"));
        assert!(execution.complete, "{binding}: {execution:#?}");
        let Some(BindingEffectAuthorityReport::OperationReceipt { receipt }) =
            &execution.binding_effect_authority
        else {
            panic!("{binding} must retain a complete branch call inventory: {execution:#?}");
        };
        assert!(
            receipt
                .skipped_calls
                .iter()
                .any(|call| call.reason == expected_reason),
            "{binding} must identify the excluded If arm: {receipt:#?}"
        );
    }
}

#[test]
fn callee_selection_survives_a_branching_argument_and_lexical_unwind() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
let calls = 0;
function original(value) { return value + 1; }
function replacement(value) { return value + 10; }
let selected = original;
function argument(flag) {
  calls++;
  selected = replacement;
  if (flag) { const payload = () => 9; throw payload; }
  return 4;
}
function run(flag) {
  try { return selected(argument(flag)); }
  catch (error) { return error(); }
}
const normal = run(false);
selected = original;
const thrown = run(true);
console.log(normal, thrown, calls);
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "5 9 2\n");
}

#[test]
fn parameter_member_and_field_continuations_preserve_references() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"
let calls = 0;
const first = { value: 0 };
const second = { value: 99 };
let target = first;
function choose(flag) {
  calls++;
  target = second;
  if (flag) throw 7;
  return 3;
}
const object = {
  base: 10,
  method(flag, input = choose(flag)) {
    const receiver = this;
    return receiver.base + input;
  }
};
function run(flag) {
  target = first;
  try {
    target.value = object.method(flag);
    return `${first.value}:${calls}`;
  } catch (error) { return `${first.value}:${error}`; }
}
const normal = run(false);
const thrown = run(true);
console.log(normal, thrown, second.value, calls);
"#,
        vec![],
    ));
    assert_preserved_entry_output(&fixture, "13:1 13:7 99 2\n");
}
