//! End-to-end coverage for the peel factorizer extending a
//! user-declared module with anonymous top-level statements whose
//! only cross-module dependency is into that module.
//!
//! Canonical case: a class `Foo` lives in the existing
//! `features/foo` logical module. The bundle emits three top-level
//! side-effect statements after the class — decorator applications,
//! property installs, registry calls — none of which declare a new
//! binding, all of which read `Foo` eagerly. When the spec author
//! peeled `Foo` into `features/foo`, those anonymous statements
//! stayed behind in residual. The factorizer should propose
//! migrating them into the existing module as
//! `anonymous_statements:` entries; without this routing the spec
//! author has to spot them by hand.
//!
//! Negative coverage: anonymous statements that cross-depend on a
//! second active module (ambiguous extension target) and anonymous
//! statements whose only deps are intra-residual (no extension
//! target at all) must NOT be promoted into a single-module
//! extension.

use analysis::OwnerGraphReport;
use debundle_e2e_support::*;
use peel::factorize::factorize;
use spec::ModulePath;
use std::collections::BTreeMap;

/// Build an active-claims map (binding name → canonical module path)
/// from clean spec paths.
fn claims(pairs: &[(&str, &str)]) -> BTreeMap<String, ModulePath> {
    pairs
        .iter()
        .map(|(binding, path)| (binding.to_string(), ModulePath::parse(path, "").unwrap()))
        .collect()
}

/// All-residual anchor pattern: one named anchor binding belongs to
/// the anchor module so the chunk has at least one logical module,
/// and the class binding the test cares about belongs to the
/// "existing" module the proposal must extend.
fn build_report_from_source(
    source: &str,
    logical_modules: Vec<LogicalModuleEntry>,
) -> OwnerGraphReport {
    let mut opts = FixtureOpts::new(source, logical_modules);
    opts.unassigned_mode = unassigned_mode_inline();
    let fixture = run_fixture(opts);
    read_json(&fixture.report_root.join("static/app/owner_graph.json"))
}

#[test]
fn extend_existing_module_with_orphaned_decorator_calls() {
    // `features/foo` claims `Foo`. The three property installs
    // are anonymous side-effect statements that read `Foo`
    // eagerly. Their only cross-module constraining edge is to
    // `Foo`'s owner — i.e., into `features/foo`. The factorizer
    // must propose extending `features/foo` with those three
    // owners.
    let source = r#"const anchor = "anchor";
class Foo {
  method() { return this.x; }
}
Foo.x = "x";
Foo.y = "y";
Foo.z = "z";
export { anchor, Foo };
"#;

    let report_graph = build_report_from_source(
        source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            logical_module("features/foo", &[Member::new("Foo")]),
        ],
    );
    let report = factorize(&report_graph, &claims(&[("Foo", "features/foo")]), 10_000);

    let extension = report
        .proposals
        .iter()
        .find(|p| p.extends_module_id.as_deref() == Some("features/foo"))
        .unwrap_or_else(|| {
            panic!(
                "factorizer must propose extending `features/foo` with the three orphaned anonymous statements; got: {report:#?}",
            )
        });
    assert_eq!(
        extension.extension_owner_ids.len(),
        3,
        "extension must list all three anonymous statement owners; got: {extension:#?}",
    );
    assert!(
        extension.binding_ids.is_empty(),
        "extension into `features/foo` carries no new bindings — only anonymous statements: {extension:#?}",
    );
    assert!(
        extension.landable_today,
        "anon-only extension into an existing module must be landable: {extension:#?}",
    );

    // Downstream materialization: claiming `Foo` plus those three
    // anonymous statements in `features/foo` must round-trip
    // through the materializer. This pins that the proposal a
    // consumer reads can actually be applied.
    let promoted_opts = FixtureOpts::new(
        source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            logical_module_with_anon(
                "features/foo",
                &[Member::new("Foo")],
                &["Foo.x = \"x\";", "Foo.y = \"y\";", "Foo.z = \"z\";"],
            ),
        ],
    );
    let _ = run_fixture(promoted_opts);
}

#[test]
fn anonymous_statement_with_external_deps_is_not_promoted_to_extension() {
    // `Bar.use(Foo)` reads bindings from BOTH active modules. The
    // extension target is ambiguous — the factorizer must NOT
    // route it into a single-module extension.
    let source = r#"const anchor = "anchor";
class Foo {}
class Bar {
  static use(target) { this.target = target; }
}
Bar.use(Foo);
export { anchor, Foo, Bar };
"#;

    let report_graph = build_report_from_source(
        source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            logical_module("features/foo", &[Member::new("Foo")]),
            logical_module("features/bar", &[Member::new("Bar")]),
        ],
    );
    let report = factorize(
        &report_graph,
        &claims(&[("Foo", "features/foo"), ("Bar", "features/bar")]),
        10_000,
    );

    // No anon-only extension proposal should exist. The mixed-deps
    // anonymous statement must land as a fresh-module proposal
    // (or stay residual), never as an extension of one of the two
    // active modules.
    let anon_extension = report.proposals.iter().find(|p| {
        p.extends_module_id.is_some()
            && p.binding_ids.is_empty()
            && !p.extension_owner_ids.is_empty()
    });
    assert!(
        anon_extension.is_none(),
        "no anon-only extension proposal expected when the anon statement crosses two active modules; got: {anon_extension:#?}; report: {report:#?}",
    );
}

#[test]
fn anonymous_statement_with_only_intra_residual_deps_is_not_promoted() {
    // The anonymous statement `helper.install()` reads a residual
    // binding `helper`. `Foo` exists in `features/foo` but the
    // anon never touches it. The factorizer must NOT promote the
    // anon into a `features/foo` extension.
    let source = r#"const anchor = "anchor";
class Foo {}
const helper = { install() {} };
helper.install();
export { anchor, Foo };
"#;

    let report_graph = build_report_from_source(
        source,
        vec![
            logical_module("anchors/anchor", &[Member::new("anchor")]),
            logical_module("features/foo", &[Member::new("Foo")]),
        ],
    );
    let report = factorize(&report_graph, &claims(&[("Foo", "features/foo")]), 10_000);

    // No extension of `features/foo` should be proposed for the
    // intra-residual anon.
    assert!(
        !report.proposals.iter().any(|p| {
            p.extends_module_id.as_deref() == Some("features/foo")
                && p.binding_ids.is_empty()
                && !p.extension_owner_ids.is_empty()
        }),
        "intra-residual anon must not be misrouted into a `features/foo` extension: {report:#?}",
    );
}
