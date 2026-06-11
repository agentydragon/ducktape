//! Pins docs/design.md "Lemma 5 (Side-effect ordering)": for every
//! source-order pair of side-effecting statements split across
//! modules, the earlier statement fires before the later one,
//! because the pair's `S`-edge forces the later statement's home
//! module to import the earlier one's.
//!
//! The fixture makes the `S`-edge load-bearing: the two impure
//! statements share no binding reads, so without the
//! `mod_a_second → mod_b_first` S-edge nothing in `I` would order
//! the modules and the import tie-break (`ModuleId` ascending —
//! `mod_a_second` sorts first alphabetically, hence the deliberately
//! inverted module names) would evaluate `mod_a_second` first,
//! visibly printing "second" before "first". The S-edge both flips
//! the entry's import order and materializes as a phantom
//! side-effect import in `mod_a_second`, so a wrong topological
//! choice cannot reorder the prints.

use debundle_e2e_support::*;

#[test]
fn cross_module_impure_statements_fire_in_source_order() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const loud_first = (console.log("first"), "f");
const loud_second = (console.log("second"), "s");
console.log(loud_first, loud_second);
"#,
        vec![
            logical_module("mod_a_second", &[Member::new("loud_second")]),
            logical_module("mod_b_first", &[Member::new("loud_first")]),
        ],
    ));
    // The S-edge materializes as a phantom side-effect import: the
    // later impure statement's module imports the earlier one's, so
    // the linker cannot evaluate mod_a_second first.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/mod_a_second.js",
        &["./mod_b_first.js"],
        &[],
    );
    // Observation trace identical to the input chunk: "first" prints
    // before "second" even though the statements now live in
    // different modules.
    assert_entry_output(&fixture, "first\nsecond\nf s\n");
}
