//! Pinning by use site: a `source_matches[].bindings[]` local that the
//! template uses without declaring claims the top-level declaration that
//! identifier binds to in the match. An entity with no distinctive shape of
//! its own, such as one of several byte-identical helper copies, is pinned
//! through a statement that uses it.

use debundle_e2e_support::{
    BindingGroup, FixtureOpts, find_outcome, logical_module_with_binding_groups,
    read_selector_outcomes, run_rejection_fixture, run_spec_validate, write_validate_fixture_spec,
};

fn no_match_for(chunk: &'static str, template: &'static str, local: &'static str) {
    let rejected = run_rejection_fixture(
        FixtureOpts::new(
            chunk,
            vec![logical_module_with_binding_groups(
                "use/site",
                &[],
                &[BindingGroup::source_alpha(template, &[(local, "Pinned")])],
            )],
        )
        .with_extra_args(&["--dry-run"]),
    );
    let outcomes = read_selector_outcomes(&rejected.report_root);
    find_outcome(&outcomes, "no_match", "Pinned");
}

/// `console` has no top-level declaration in the chunk: there is nothing to
/// claim.
#[test]
fn free_local_without_a_top_level_declaration_is_no_match() {
    no_match_for(
        "const x = 1;\nconsole.log(x);\n",
        "logger.log(x);",
        "logger",
    );
}

/// `helper` binds `a` in one arrow and `b` in the other: no one declaration.
#[test]
fn free_local_bound_to_two_identifiers_is_no_match() {
    no_match_for(
        "const a = 1;\nconst b = 2;\nconst p = [() => a, () => b];\nconsole.log(p);\n",
        "const pair = [() => helper, () => helper];",
        "helper",
    );
}

/// A local the template neither declares nor uses names nothing: the spec is
/// rejected.
#[test]
fn local_absent_from_the_template_is_rejected() {
    let fixture = write_validate_fixture_spec(FixtureOpts::new(
        "const x = 1;\nconsole.log(x);\n",
        vec![logical_module_with_binding_groups(
            "use/site",
            &[],
            &[BindingGroup::source_alpha(
                "console.log(x);",
                &[("absent", "Absent")],
            )],
        )],
    ));
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(!out.status.success(), "stdout={}", out.stdout);
    assert!(
        out.stderr
            .contains("`absent` is neither declared nor used by source_matches[].match"),
        "{}",
        out.stderr
    );
}
