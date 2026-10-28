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
        "export function mutate() { Set.prototype.add = function () { return this; }; }\n",
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
