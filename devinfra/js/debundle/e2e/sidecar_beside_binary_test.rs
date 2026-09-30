//! A released `debundle` runs outside Bazel: no runfiles tree and no
//! `DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER`. It finds the CP-SAT sidecar beside
//! itself, and says where it looked when it is not there.
//!
//! The fixture pins `mod_a` by a selector and `mod_b` by a broad one, which
//! only resolves as a group of two entities: a group that interacts goes to the
//! sidecar (`docs/selector_resolution.md` § Inside the resolve).

use debundle_e2e_support::{
    DownloadedDebundle, FixtureOpts, Member, assert_module_source, logical_module,
    write_validate_fixture_spec,
};

const SIDECAR_ENV: &str = "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER";

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

#[test]
fn sidecar_beside_the_binary_resolves_selectors_for_validate_and_run() {
    let debundle = DownloadedDebundle::with_sidecar();
    let fixture = write_validate_fixture_spec(eliminated_selector_fixture());
    let spec = fixture.spec_path.to_str().unwrap();

    let validated = debundle.run(&["spec", "validate", "--spec", spec]);
    assert!(
        validated.status.success(),
        "spec validate exited {:?}\nstdout:\n{}\nstderr:\n{}",
        validated.status.code(),
        validated.stdout,
        validated.stderr,
    );
    let ran = debundle.run(&["run", "--spec", spec]);
    assert!(
        ran.status.success(),
        "run exited {:?}\nstdout:\n{}\nstderr:\n{}",
        ran.status.code(),
        ran.stdout,
        ran.stderr,
    );

    let out_root = fixture.spec_path.parent().unwrap().join("out/app");
    assert_module_source(
        &out_root,
        "static/app/modules/routes/mod_b.js",
        &["slot-1"],
        &["slot-0"],
    );
}

#[test]
fn missing_sidecar_error_says_where_it_looked_and_where_to_get_it() {
    let debundle = DownloadedDebundle::binary_only();
    let fixture = write_validate_fixture_spec(eliminated_selector_fixture());

    let out = debundle.run(&[
        "spec",
        "validate",
        "--spec",
        fixture.spec_path.to_str().unwrap(),
    ]);

    assert!(!out.status.success(), "stdout:\n{}", out.stdout);
    let expected_sibling = debundle
        .dir()
        .canonicalize()
        .unwrap()
        .join(DownloadedDebundle::SIDECAR_FILE_NAME);
    for expected in [
        SIDECAR_ENV,
        expected_sibling.to_str().unwrap(),
        "`debundle-*` release",
    ] {
        assert!(out.stderr.contains(expected), "{expected}\n{}", out.stderr);
    }
}

#[test]
fn env_var_overrides_the_sidecar_beside_the_binary() {
    let debundle = DownloadedDebundle::with_sidecar();
    let fixture = write_validate_fixture_spec(eliminated_selector_fixture());

    let out = debundle.run_with_env(
        &[
            "spec",
            "validate",
            "--spec",
            fixture.spec_path.to_str().unwrap(),
        ],
        &[(SIDECAR_ENV, "/nonexistent/selector_cpsat_solver")],
    );

    assert!(!out.status.success(), "stdout:\n{}", out.stdout);
    assert!(
        out.stderr.contains("/nonexistent/selector_cpsat_solver"),
        "{}",
        out.stderr
    );
}
