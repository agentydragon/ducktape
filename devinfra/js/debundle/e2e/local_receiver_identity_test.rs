//! Native integrity uses a local receiver's value at the mutation site.

use debundle_e2e_support::*;

fn source(update: &str, observe_update: bool) -> String {
    let observation = if observe_update {
        "const first = update(null, true);\nconsole.log(registry.item.executor(), first.flag, update(first, true).flag, update(null, false) === globalThis.slot);"
    } else {
        "console.log(registry.item.executor());"
    };
    format!(
        r#"{update}
function deferred() {{ return later; }}
const entries = [{{ name: 'item', executor: deferred }}];
const registry = Object.fromEntries(entries.map(entry => [entry.name, entry]));
const later = 1;
export {{ registry, later, update }};
{observation}
"#
    )
}

fn split_opts(source: &str) -> FixtureOpts<'_> {
    let mut opts = FixtureOpts::new(
        source,
        vec![
            logical_module(
                "registry",
                &[
                    Member::new("deferred"),
                    Member::new("entries"),
                    Member::new("registry"),
                ],
            ),
            logical_module("values/later", &[Member::new("later")]),
        ],
    )
    .with_unassigned_mode(unassigned_mode_inline());
    opts.execution_runtime = ExecutionRuntime::NativeBrowserEsm;
    opts.dataflow_aware_s_chain = true;
    opts.trusted_dataflow_summaries = true;
    opts.local_property_effects = true;
    opts
}

fn assert_safe_split(update: &str) {
    let source = source(update, true);
    let fixture = run_fixture(split_opts(&source));
    assert_preserved_entry_output(&fixture, "1 true true true\n");
    assert_generated_module_after_entry_script(
        &fixture,
        "const { update } = await import('./static/app/entry.js');\nconst value = update(null, true);\nconsole.log(value.flag, update(value, true).flag);",
        "true true\n",
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/registry.js",
        &["registry"],
        &["later", "update"],
    );
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/values/later.js",
        &["later"],
        &["registry", "update"],
    );
}

fn assert_unsafe_receiver_rejected(update: &str) {
    let source = source(update, false);
    expect_atomic_conflict_rejection(split_opts(&source), &["registry", "values/later"], &[]);
}

#[test]
fn later_branch_alias_does_not_taint_an_earlier_fresh_literal_write() {
    assert_safe_split(
        r#"function update(value, branch) {
  var receiver = {};
  if (branch) receiver.flag = true;
  else receiver = globalThis.slot;
  return receiver;
}"#,
    );
}

#[test]
fn local_constructor_and_return_new_wrapper_preserve_receiver_identity() {
    assert_safe_split(
        r#"function Holder(value) { this.value = value; }
function clone(value) { return new Holder(value); }
function update(value, branch) {
  var receiver = value instanceof Holder ? clone(value) : new Holder(value);
  if (branch) receiver.flag = true;
  else receiver = globalThis.slot;
  return receiver;
}"#,
    );
}

#[test]
fn alias_reaching_the_write_keeps_native_integrity_fallback() {
    assert_unsafe_receiver_rejected(
        r#"function update(value, branch) {
  var receiver = {};
  if (branch) receiver = globalThis.slot;
  receiver.flag = true;
  return receiver;
}"#,
    );
}

#[test]
fn constructor_returning_a_native_object_is_not_a_fresh_receiver() {
    assert_unsafe_receiver_rejected(
        r#"function Holder() { return Array.prototype; }
function update(value, branch) {
  var receiver = new Holder();
  if (branch) receiver.flag = true;
  else receiver = globalThis.slot;
  return receiver;
}"#,
    );
}

#[test]
fn constructor_returning_an_unknown_object_keeps_fallback() {
    assert_unsafe_receiver_rejected(
        r#"function Holder(value) { return value; }
function update(value, branch) {
  var receiver = new Holder(value);
  if (branch) receiver.flag = true;
  else receiver = globalThis.slot;
  return receiver;
}"#,
    );
}

#[test]
fn wrapper_returning_its_argument_is_not_an_allocation_certificate() {
    assert_unsafe_receiver_rejected(
        r#"function Holder(value) { this.value = value; }
function clone(value) { return value; }
function update(value, branch) {
  var receiver = value ? clone(value) : new Holder(value);
  if (branch) receiver.flag = true;
  else receiver = globalThis.slot;
  return receiver;
}"#,
    );
}

#[test]
fn an_unknown_initializer_alternative_keeps_fallback() {
    assert_unsafe_receiver_rejected(
        r#"function update(value, branch) {
  var receiver = value || {};
  if (branch) receiver.flag = true;
  else receiver = globalThis.slot;
  return receiver;
}"#,
    );
}

#[test]
fn captured_rebinding_during_an_opaque_call_keeps_fallback() {
    assert_unsafe_receiver_rejected(
        r#"function update(value, branch) {
  var receiver = {};
  value(() => { receiver = globalThis.slot; });
  if (branch) receiver.flag = true;
  else receiver = globalThis.slot;
  return receiver;
}"#,
    );
}

#[test]
fn a_loop_backedge_can_bring_a_native_alias_to_the_write() {
    assert_unsafe_receiver_rejected(
        r#"function update(value, branch) {
  var receiver = {};
  for (var index = 0; index < 2; index++) {
    if (branch) receiver.flag = true;
    receiver = globalThis.slot;
  }
  return receiver;
}"#,
    );
}

#[test]
fn a_parameter_with_the_same_name_as_a_local_constructor_is_unknown() {
    assert_unsafe_receiver_rejected(
        r#"function Holder() {}
function update(Holder, branch) {
  var receiver = new Holder();
  if (branch) receiver.flag = true;
  else receiver = globalThis.slot;
  return receiver;
}"#,
    );
}

#[test]
fn fresh_receiver_identity_does_not_certify_a_native_descendant() {
    assert_unsafe_receiver_rejected(
        r#"function update(value, branch) {
  var receiver = { nested: Array.prototype };
  if (branch) receiver.nested.flag = true;
  else receiver = globalThis.slot;
  return receiver;
}"#,
    );
}
