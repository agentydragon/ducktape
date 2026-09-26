//! Pinning by use site: a `source_matches[].bindings[]` local that the
//! template uses without declaring claims the top-level declaration that
//! identifier binds to in the match. An entity with no distinctive shape of
//! its own, such as one of several byte-identical helper copies, is pinned
//! through a statement that uses it.

use debundle_e2e_support::{
    BindingGroup, FixtureOpts, Member, assert_entry_output, assert_module_source, find_outcome,
    logical_module_with_source_matches, read_selector_outcomes, run_dry_run_rejection_fixture,
    run_fixture, run_spec_validate, write_validate_fixture_spec,
};

const TWO_HELPER_COPIES: &str = r#"var ga = Object.getOwnPropertyDescriptor;
var pa = Object.defineProperty;
var da = (decorators, target, key) => {
  const desc = ga(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  pa(target, key, desc);
};
var gb = Object.getOwnPropertyDescriptor;
var pb = Object.defineProperty;
var db = (decorators, target, key) => {
  const desc = gb(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  pb(target, key, desc);
};
const mark = (suffix) => (target, key, desc) => {
  const original = desc.value;
  desc.value = function () { return original.call(this) + suffix; };
};
class Alpha {
  alphaLabel() { return "a"; }
}
class Beta {
  betaLabel() { return "b"; }
}
da([mark("A")], Alpha.prototype, "alphaLabel", 1);
db([mark("B")], Beta.prototype, "betaLabel", 1);
console.log(new Alpha().alphaLabel(), new Beta().betaLabel());
export { Alpha, Beta };
"#;

/// The two helper copies are byte-identical, so no template of a helper's
/// body tells them apart; each is pinned through the decorator call that uses
/// it.
#[test]
fn identical_helper_copies_are_pinned_through_their_call_sites() {
    let fixture = run_fixture(FixtureOpts::new(
        TWO_HELPER_COPIES,
        vec![
            logical_module_with_source_matches(
                "alpha",
                &[Member::source_alpha(
                    "Alpha",
                    "class Alpha {\n  alphaLabel() {\n    STMT_LIST;\n  }\n}",
                )],
                &[BindingGroup::source_alpha(
                    r#"decorate([mark("A")], Alpha.prototype, "alphaLabel", 1);"#,
                    &[("decorate", "alphaDecorator")],
                )],
            ),
            logical_module_with_source_matches(
                "beta",
                &[Member::source_alpha(
                    "Beta",
                    "class Beta {\n  betaLabel() {\n    STMT_LIST;\n  }\n}",
                )],
                &[BindingGroup::source_alpha(
                    r#"decorate([mark("B")], Beta.prototype, "betaLabel", 1);"#,
                    &[("decorate", "betaDecorator")],
                )],
            ),
        ],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/alpha.js",
        &["var alphaDecorator = "],
        &["betaDecorator"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/beta.js",
        &["var betaDecorator = "],
        &["alphaDecorator"],
    );
    assert_entry_output(&fixture, "aA bB\n");
}

/// One template claims both a declaration it makes and the helper it calls.
#[test]
fn template_claims_its_declaration_and_a_helper_it_uses() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"function h(n) {
  return n + 1;
}
function k(n) {
  return n + 1;
}
const x = h(1);
console.log(x, k(2));
"#,
        vec![logical_module_with_source_matches(
            "counter",
            &[],
            &[BindingGroup::source_alpha(
                "const value = increment(1);",
                &[("value", "value"), ("increment", "increment")],
            )],
        )],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/counter.js",
        &["function increment(", "value = increment(1)"],
        &["function k("],
    );
    assert_entry_output(&fixture, "2 3\n");
}

fn no_match_for(chunk: &'static str, template: &'static str, local: &'static str) {
    let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(
        chunk,
        vec![logical_module_with_source_matches(
            "use/site",
            &[],
            &[BindingGroup::source_alpha(template, &[(local, "Pinned")])],
        )],
    ));
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
        vec![logical_module_with_source_matches(
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
