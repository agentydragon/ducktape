//! A released `debundle` is one file. Copied alone into an empty directory and
//! run with an empty environment it still resolves selectors that need CP-SAT,
//! and that solver is an optimized build: an unoptimized one logs
//! `CP-SAT is running in debug mode` to stderr at its first solve.
//!
//! The fixture pins `mod_a` by a selector and `mod_b` by a broad one, which only
//! resolves as a group of two entities: a group that interacts goes to the
//! solver (`docs/selector_resolution.md` § Inside the resolve).

use debundle_e2e_support::{
    CommandResult, FixtureOpts, Member, StandaloneDebundle, assert_module_source, logical_module,
    write_validate_fixture_spec,
};

fn eliminated_selector_fixture() -> FixtureOpts<'static> {
    let source = r#"function sharedRead(input, key) {
  return input[key] ?? "";
}

function route0(input) {
  return sharedRead(input, "slot-0");
}

function route1(input) {
  return sharedRead(input, "slot-1");
}

console.log(route1({ "slot-1": "ok" }));
export { route0, route1 };
"#;
    let selector = |key: &str| {
        format!(
            r#"function target(input) {{
  return sharedRead(input, {key});
}}"#
        )
    };
    FixtureOpts::new(
        source,
        vec![
            logical_module(
                "routes/mod_a",
                &[Member::source_alpha_target(
                    "RouteA",
                    "target",
                    selector(r#""slot-0""#),
                )],
            ),
            logical_module(
                "routes/mod_b",
                &[Member::source_alpha_target(
                    "RouteB",
                    "target",
                    selector("ANYTHING"),
                )],
            ),
        ],
    )
}

fn assert_quiet_success(command: &str, result: &CommandResult) {
    assert!(
        result.status.success(),
        "{command} exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    assert!(
        !result.stderr.contains("debug mode"),
        "{command} ran an unoptimized CP-SAT:\n{}",
        result.stderr,
    );
}

#[test]
fn a_lone_binary_resolves_selectors_for_validate_and_run() {
    let debundle = StandaloneDebundle::install();
    let fixture = write_validate_fixture_spec(eliminated_selector_fixture());
    let spec = fixture.spec_path.to_str().unwrap();

    assert_quiet_success(
        "spec validate",
        &debundle.run(&["spec", "validate", "--spec", spec]),
    );
    assert_quiet_success("run", &debundle.run(&["run", "--spec", spec]));

    assert_module_source(
        &fixture.spec_path.parent().unwrap().join("out/app"),
        "static/app/modules/routes/mod_b.js",
        &["slot-1"],
        &["slot-0"],
    );
}
