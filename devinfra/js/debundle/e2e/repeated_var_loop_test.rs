//! Desired behavior for repeated top-level `var` declarations.

use debundle_e2e_support::*;

#[test]
fn repeated_var_loop_counter_preserves_execution() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"for (var i = 0; i < 2; i++) console.log(i);
for (var i = 3; i < 5; i++) console.log(i);
console.log(i);
"#,
        vec![],
    ));
    // Both initializers run in order; `var` leaves one shared binding after the loops.
    assert_entry_output(&fixture, "0\n1\n3\n4\n5\n");
}

#[test]
fn bare_redeclaration_does_not_reset_an_existing_value() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var value = "initial";
value = "assigned";
var value;
console.log(value);
export { value };
"#,
        vec![],
    ));

    assert_entry_output(&fixture, "assigned\n");
}

#[test]
fn repeated_initializers_stay_around_intervening_reads_in_source_order() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var value = (console.log("first initializer"), 1);
console.log("between", value);
var value = (console.log("second initializer"), value + 1);
console.log("after", value);
"#,
        vec![],
    ));

    assert_entry_output(
        &fixture,
        "first initializer\nbetween 1\nsecond initializer\nafter 2\n",
    );
}

#[test]
fn conditional_var_initializers_only_update_the_shared_binding_when_run() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var value = "initial";
if (false) {
  var value = "skipped";
}
console.log(value);
if (true) {
  var value = "selected";
}
console.log(value);
"#,
        vec![],
    ));

    assert_entry_output(&fixture, "initial\nselected\n");
}

#[test]
fn closure_reads_the_binding_updated_by_a_later_redeclaration() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var count = 1;
var readCount = () => count;
console.log(readCount());
var count = count + 1;
console.log(readCount());
"#,
        vec![],
    ));

    assert_entry_output(&fixture, "1\n2\n");
}

#[test]
fn repeated_declarations_share_a_named_module_and_member_rename() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var counter = 1;
var counter = counter + 1;
console.log(counter);
export { counter };
"#,
        vec![logical_module(
            "state",
            // Member::renamed takes (public/export name, source binding).
            &[Member::renamed("sharedCount", "counter")],
        )],
    ));

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/state.js",
        &["sharedCount"],
        &["counter"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/state.js",
        &["var sharedCount = 1", "var sharedCount = sharedCount + 1"],
        &["var counter"],
    );
    assert_entry_output(&fixture, "2\n");
}

#[test]
fn read_between_redeclarations_cannot_be_moved_away_from_them() {
    // Pulling both `a` declarations into one ESM module would make the
    // residual initializer for `seen` observe the final value of `a`, not
    // the value between the declarations. The ordering constraints make
    // this split unrealizable, so claiming only `a` must be rejected.
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"var a = 1;
const seen = a;
var a = 2;
console.log(a, seen);
export { a, seen };
"#,
            vec![logical_module("state", &[Member::new("a")])],
        ),
        &["state"],
        &[],
    );
}

#[test]
fn predeclaration_reader_cannot_be_split_from_shared_var_declarations() {
    // In the input, `seen` observes the hoisted `value` before either
    // initializer runs, so it must be `undefined`. An ESM import from the
    // later `state` module would evaluate both initializers first.
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            r#"const seen = value;
var value = 1;
var value = 2;
console.log(seen, value);
export { seen, value };
"#,
            vec![
                logical_module("reader", &[Member::new("seen")]),
                logical_module("state", &[Member::new("value")]),
            ],
        ),
        &["reader", "state"],
        &[],
    );
}

#[test]
fn read_before_first_initializer_observes_undefined() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"console.log(value);
var value = "first";
var value = "second";
console.log(value);
export { value };
"#,
        vec![],
    ));

    assert_entry_output(&fixture, "undefined\nsecond\n");
}

#[test]
fn consecutive_for_in_and_for_of_var_heads_share_the_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"for (var value in { first: 1, second: 2 }) console.log("in", value);
for (var value of ["third", "fourth"]) console.log("of", value);
console.log("final", value);
"#,
        vec![],
    ));

    assert_entry_output(
        &fixture,
        "in first\nin second\nof third\nof fourth\nfinal fourth\n",
    );
}

#[test]
fn source_selector_on_second_var_declaration_names_the_shared_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"var value = 1;
var value = 2;
console.log(value);
export { value };
"#,
        vec![logical_module(
            "state",
            &[Member::source_alpha(
                "readableValue",
                r#"var sourceValue = 2;"#,
            )],
        )],
    ));

    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/state.js",
        &["readableValue"],
        &["value"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/state.js",
        &["var readableValue = 1", "var readableValue = 2"],
        &["var value"],
    );
    assert_entry_output(&fixture, "2\n");
}

#[test]
fn dataflow_aware_s_chain_preserves_intermediate_repeated_var_read() {
    let source = r#"var x = 1;
const seen = x;
var x = 2;
console.log(seen, x);
export { seen, x };
"#;

    let fixture = run_fixture(FixtureOpts::new(source, vec![]).with_dataflow_aware_s_chain());
    assert_entry_output(&fixture, "1 2\n");

    // The optimized S-chain must retain the sequencing constraints too:
    // extracting both writes to `x` while leaving `seen` in residual would
    // make `seen` observe 2 instead of 1, so this split is rejected.
    expect_atomic_conflict_rejection(
        FixtureOpts::new(source, vec![logical_module("state", &[Member::new("x")])])
            .with_dataflow_aware_s_chain(),
        &["state"],
        &[],
    );
}

#[test]
fn repeated_binding_cannot_be_placed_in_two_named_modules() {
    let outcomes = run_rejection_fixture(FixtureOpts::new(
        r#"var counter = 1;
var counter = counter + 1;
console.log(counter);
export { counter };
"#,
        vec![
            logical_module("state/primary", &[Member::new("counter")]),
            logical_module("state/duplicate", &[Member::new("counter")]),
        ],
    ))
    .selector_outcomes();
    let claim = find_outcome(&outcomes, "duplicate_claim", "counter");
    assert_eq!(claim["placement"]["logical_module"], "state/primary");
    assert_eq!(
        claim["outcome"]["claimed_by"]["logical_module"],
        "state/duplicate"
    );
}

#[test]
fn shared_loop_var_declarations_cannot_split_their_companions() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"for (var i = 0, a = 1; i < 1; i++) {}
for (var i = 0, b = 2; i < 1; i++) {}
console.log(a, b);
export { a, b };
"#,
            vec![
                logical_module("left", &[Member::new("a")]),
                logical_module("right", &[Member::new("b")]),
            ],
        ),
        &["declares bindings assigned to different modules"],
    );
}

#[test]
fn at_init_call_uses_closure_from_later_var_initializer() {
    let source = r#"var read = () => 0;
var read = () => late;
const late = 2;
const answer = read();
console.log(answer);
export { read, late, answer };
"#;

    let fixture = run_fixture(FixtureOpts::new(source, vec![]));
    assert_entry_output(&fixture, "2\n");

    // With the default catchall mode, `late` is in the residual module.
    // The reader module imports it, so ESM evaluates the residual module
    // before the at-init call and preserves the input's value.
    let extracted = run_fixture(FixtureOpts::new(
        source,
        vec![logical_module(
            "reader",
            &[Member::new("read"), Member::new("answer")],
        )],
    ));
    assert_entry_output(&extracted, "2\n");

    // Inline residual statements stay in entry. Entry imports `reader`
    // before initializing `late`, while the reader calls the later closure
    // at module init, so this split creates a real at-init cycle.
    expect_cycle_rejection(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "reader",
                &[Member::new("read"), Member::new("answer")],
            )],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["reader"],
    );
}

#[test]
fn identical_declaration_matches_select_one_shared_binding() {
    let fixture = run_fixture(FixtureOpts::new(
        "var value; var value; console.log(value);",
        vec![logical_module(
            "state",
            &[Member::source_alpha("readableValue", "var candidate;")],
        )],
    ));
    assert_entry_output(&fixture, "undefined\n");
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/state.js",
        &["readableValue"],
        &["value"],
    );
}

#[test]
fn named_loop_binding_moves_and_renames_every_loop() {
    let fixture = run_fixture(FixtureOpts::new(
        "for (var i = 0; i < 2; i++) console.log(i); for (var i = 3; i < 5; i++) console.log(i); console.log(i);",
        vec![logical_module("loops", &[Member::renamed("counter", "i")])],
    ));
    assert_entry_output(&fixture, "0\n1\n3\n4\n5\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/loops.js",
        &["var counter = 0", "var counter = 3"],
        &["var i ="],
    );
}

#[test]
fn name_pin_and_later_site_selector_cannot_claim_same_binding_twice() {
    let outcomes = run_rejection_fixture(FixtureOpts::new(
        "var value = 1; var value = 2; console.log(value);",
        vec![
            logical_module("first", &[Member::new("value")]),
            logical_module(
                "second",
                &[Member::source_alpha("readableValue", "var candidate = 2;")],
            ),
        ],
    ))
    .selector_outcomes();
    let claim = find_outcome(&outcomes, "duplicate_claim", "readableValue");
    assert_eq!(claim["placement"]["logical_module"], "second");
    assert_eq!(claim["outcome"]["claimed_by"]["logical_module"], "first");
}
