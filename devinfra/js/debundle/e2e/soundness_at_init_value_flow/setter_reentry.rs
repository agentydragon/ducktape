//! Setters installed before or across an `await` keep blocking splits that would read a future value.

use super::in_case;
use debundle_e2e_support::*;

#[test]
fn future_uncreated_command_is_not_an_available_setter_for_fresh_property_write() {
    let source = r#"function dormant() { return command; }
const reference = dormant;
globalThis.hostCall = () => {};
globalThis.hostCall();
const box = {};
box.value = 1;
const command = { run: () => later };
const later = 7;
console.log(box.value, later);
export { box, later };
"#;
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &binding_members(["box", "command"]),
                    &[
                        "globalThis.hostCall = () => {};",
                        "globalThis.hostCall();",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "1 7\n");
}

/// `box.value = 1` may run a setter reading `later`, so `value/box` (with the
/// `setup` statements) cannot precede `value/later`.
#[test]
fn property_write_rejects_reachable_setters() {
    for (name, source, setup) in [
        (
            "hoisted setter before its declaration",
            r#"const box = {};
Object.defineProperty(box, "value", { set: setter, configurable: true });
box.value = 1;
function setter() { return later; }
const later = 7;
export { box, later };
"#,
            &["Object.defineProperty(box, \"value\", { set: setter, configurable: true });"][..],
        ),
        (
            "Proxy set trap",
            r#"const box = new Proxy({}, { set() { return later; } });
box.value = 1;
const later = 7;
export { box, later };
"#,
            &[],
        ),
        (
            "assignment before a future var declaration",
            r#"const box = {};
setter = () => later;
Object.defineProperty(box, "value", { set: setter, configurable: true });
box.value = 1;
var setter;
const later = 7;
export { box, later };
"#,
            &[
                "setter = () => later;",
                "Object.defineProperty(box, \"value\", { set: setter, configurable: true });",
            ],
        ),
        (
            "async setter reading before its first await",
            r#"const box = {};
Object.defineProperty(box, "value", { set: async function setter() { const observed = later; await 0; return observed; } });
box.value = 1;
const later = 7;
export { box, later };
"#,
            &[
                "Object.defineProperty(box, \"value\", { set: async function setter() { const observed = later; await 0; return observed; } });",
            ],
        ),
    ] {
        in_case(name, || {
            let mut anonymous = setup.to_vec();
            anonymous.push("box.value = 1;");
            expect_atomic_conflict_rejection(
                FixtureOpts::new(
                    source,
                    vec![
                        logical_module_with_anon("value/box", &[Member::new("box")], &anonymous),
                        logical_module("value/later", &[Member::new("later")]),
                    ],
                )
                .with_unassigned_mode(unassigned_mode_inline()),
                &["value/box", "value/later"],
                &[],
            );
        });
    }
}

/// Anchor for the pre-await async setter: a read after the first await is deferred.
#[test]
fn async_setter_read_after_first_await_can_split() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const box = {};
Object.defineProperty(box, "value", { set: async function setter() { await 0; return later; } });
box.value = 1;
const later = 7;
console.log(later);
export { box, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &[Member::new("box")],
                    &[
                        "Object.defineProperty(box, \"value\", { set: async function setter() { await 0; return later; } });",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

#[test]
fn direct_eval_installed_setter_keeps_whole_chunk_fallback() {
    let source = r#"function dormant() { return command; }
const box = {};
eval('Object.defineProperty(box, "value", { set() { return command.run(); }, configurable: true })');
box.value = 1;
const command = { run: () => later };
const later = 7;
export { box, command, later };
"#;
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &binding_members(["box", "command"]),
                    &[
                        "eval('Object.defineProperty(box, \"value\", { set() { return command.run(); }, configurable: true })');",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline())
        .with_admission_overrides(&["a1_eval"]),
        &["value/box", "value/later"],
        &[],
    );
}

#[test]
fn executed_callback_reading_future_const_still_blocks_split() {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"globalThis.invoke = (callback) => callback();
const box = {};
globalThis.invoke(() => command);
box.value = 1;
const command = { run: () => 7 };
export { box, command };
"#,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &[Member::new("box")],
                    &[
                        "globalThis.invoke = (callback) => callback();",
                        "globalThis.invoke(() => command);",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/command", &[Member::new("command")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["value/box", "value/command"],
        &[],
    );
}

/// `install()` defines the setter synchronously before its first `await`,
/// whether the setter is created there or hoisted from after it.
#[test]
fn nested_setter_installed_before_await_still_blocks_split() {
    for (name, source) in [
        (
            "setter created before the await",
            r#"const box = {};
async function install() {
  const setter = () => later;
  Object.defineProperty(box, "value", { set: setter });
  await 0;
}
install();
box.value = 1;
const later = 7;
export { box, later };
"#,
        ),
        (
            "hoisted setter declared after the await",
            r#"const box = {};
async function install() {
  Object.defineProperty(box, "value", { set: setter });
  await 0;
  function setter() { return later; }
}
install();
box.value = 1;
const later = 7;
export { box, later };
"#,
        ),
    ] {
        in_case(name, || {
            expect_atomic_conflict_rejection(
                FixtureOpts::new(
                    source,
                    vec![
                        logical_module_with_anon(
                            "value/box",
                            &binding_members(["box", "install"]),
                            &["install();", "box.value = 1;"],
                        ),
                        logical_module("value/later", &[Member::new("later")]),
                    ],
                )
                .with_unassigned_mode(unassigned_mode_inline()),
                &["value/box", "value/later"],
                &[],
            );
        });
    }
}

#[test]
fn nested_setter_created_after_parent_await_can_split() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const box = {};
async function install() {
  await 0;
  const setter = () => later;
  Object.defineProperty(box, "value", { set: setter });
}
install();
globalThis.hostCall = () => {};
globalThis.hostCall();
box.value = 1;
const later = 7;
console.log(later);
export { box, later };
"#,
            vec![
                logical_module_with_anon(
                    "value/box",
                    &binding_members(["box", "install"]),
                    &[
                        "install();",
                        "globalThis.hostCall = () => {};",
                        "globalThis.hostCall();",
                        "box.value = 1;",
                    ],
                ),
                logical_module("value/later", &[Member::new("later")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_preserved_entry_output(&fixture, "7\n");
}

// ─── class members handed to an opaque peer ─────────────────────────────
