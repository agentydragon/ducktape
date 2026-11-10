//! Composed class proofs reject source-visible violations of native premises.

use debundle_e2e_support::*;

#[test]
fn replaced_class_field_constructor_keeps_scalar_effect_order() {
    let source = r#"globalThis.marker = 0;
const setup = (() => {
  globalThis.Set = function () { globalThis.marker = 42; };
  return 1;
})();
class Registry { field = new Set(); }
const instance = new Registry();
const observed = globalThis.marker + setup;
console.log(observed);
"#;
    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_preserved_entry_output(&fixture, "43\n");
    expect_sequenced_cycle_rejection(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "instance",
                &[Member::new("Registry"), Member::new("instance")],
            )],
        ),
        &["instance", "residual"],
    );
}

#[test]
fn replaced_field_builtin_cannot_hide_initialization_callback() {
    let replacement = r#"globalThis.Set = function () {
  try { globalThis.saved(); return { marker: "ready" }; }
  catch (error) { return { marker: error.name }; }
};"#;
    let source = format!(
        r#"globalThis.saved = () => later;
{replacement}
class Registry {{ value = new Set(); }}
const instance = new Registry();
const later = 42;
console.log(instance.value.marker);
"#
    );
    let fixture = run_fixture(FixtureOpts::new(&source, vec![]));
    assert_preserved_entry_output(&fixture, "ReferenceError\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module_with_anon(
                    "registry",
                    &[Member::new("Registry"), Member::new("instance")],
                    &["globalThis.saved = () => later;", replacement],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        ),
        &["registry", "later"],
        &[],
    );
}

#[test]
fn class_field_native_premises_include_other_analyzed_chunks() {
    let source = "import './replacement.js';\nconst a = (() => 1)();\nclass Registry { field = new Set(); }\nconst b = new Registry();\nconst c = b ? a : 0;\nconsole.log(c);\n";
    for replacement in [
        "globalThis.Set = function () {};\n",
        "export function mutate() { const root = globalThis; root.Set = function () {}; }\n",
    ] {
        expect_sequenced_cycle_rejection(
            FixtureOpts::new(
                source,
                vec![logical_module(
                    "registry",
                    &[Member::new("Registry"), Member::new("b")],
                )],
            )
            .with_extra_chunks(&[("static/replacement", replacement)]),
            &["registry", "residual"],
        );
    }
}

/// A global slot's value may be any native, so a write through its read-back
/// reaches its key on every intrinsic: the `%Set%` global slot for `Set`, but
/// not for `add`, which the empty `new Set()` never reads. The control writes
/// the same property on a fresh local object.
#[test]
fn global_slot_read_back_write_reaches_its_key_on_every_native() {
    let source = "import './holder.js';\nconst a = (() => 1)();\nclass Registry { field = new Set(); }\nconst b = new Registry();\nconst c = b ? a : 0;\nconsole.log(c);\n";
    fn split<'a>(source: &'a str, holder: &'a [(&'a str, &'a str)]) -> FixtureOpts<'a> {
        FixtureOpts::new(
            source,
            vec![logical_module(
                "registry",
                &[Member::new("Registry"), Member::new("b")],
            )],
        )
        .with_extra_chunks(holder)
    }
    for holder in [
        "const box = {};\nexport function mutate() { box.add = function () { return this; }; }\n",
        "const box = globalThis.holder;\nexport function mutate() { box.add = function () { return this; }; }\n",
    ] {
        let fixture = run_fixture(split(source, &[("static/holder", holder)]));
        assert_preserved_entry_output(&fixture, "1\n");
    }
    expect_sequenced_cycle_rejection(
        split(
            source,
            &[(
                "static/holder",
                "const box = globalThis.holder;\nexport function mutate() { box.Set = function () {}; }\n",
            )],
        ),
        &["registry", "residual"],
    );
}
