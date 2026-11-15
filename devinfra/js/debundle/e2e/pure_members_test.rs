//! End-to-end pinning for the per-member `pure_members:
//! [<prop>, …]` spec annotation.
//!
//! The annotation extends `purity: pure` from "calls of the bound
//! Ident classify pure" to "calls of `<binding>.<prop>(...)` for
//! each listed property classify pure". Targets the
//! vendor-namespace shape — a star-import or renamed binding
//! standing in for a vendor module (React, etc.), where member
//! calls (`React.forwardRef`, `React.memo`, `React.lazy`,
//! `React.createContext`) are author-asserted side-effect-free
//! when their arguments classify pure.
//!
//! Same author-trust contract as `purity: pure`: the validator does
//! not re-verify; soundness shifts to the spec author. See
//! <docs/purity_soundness.md> "Declared purity".

use analysis::EdgeRoleReport;
use analysis_model::DepKind;
use debundle_e2e_graph_support::{FixtureGraphExt, source_unit_for_binding};
use debundle_e2e_support::*;
use spec::ChunkRenames;

const VENDOR_SOURCE: &str = "export function makePure(arg) { return arg; }\n\
     export function forwardRef(render) { return { render }; }\n";
const VENDOR_CHUNKS: &[(&str, &str)] = &[("static/vendor", VENDOR_SOURCE)];
const VENDOR_RUNTIME_FILES: &[(&str, &str)] = &[("static/app/vendor.js", VENDOR_SOURCE)];

fn assert_schedule_refusal_preserves_source_and_candidate(
    opts: FixtureOpts<'_>,
    expected_output: &str,
) {
    let rejected = run_rejection_fixture_with_graphs_and_args(opts, &["--emit-on-rejection"], 3);
    assert!(
        rejected
            .stderr
            .contains("evaluation_schedule_unknown_reordered"),
        "{}",
        rejected.stderr
    );
    assert!(rejected.gate_rejected_marker().is_file());
    assert_node_output(
        &rejected.source_root.join("static/app.js"),
        expected_output,
        "",
    );
    assert_node_output(&rejected.entry_path(), expected_output, "");
}

/// Cycle-forcing fixture body parameterized by the per-case knobs.
///
/// Shared layout across every test:
///   * `import * as ns from "./vendor.js"` — vendor namespace.
///   * `const a = (() => 1)()` — SE, stays in residual.
///   * `const b = ns.makePure(<arg>)` — the call site under test.
///   * `const c = <expr> + a` — reads `b` at init.
///   * `console.log(c)`.
///
/// Tests vary only the call argument, the `c` expression (which
/// determines whether the cycle is "real"), and the `pure_members`
/// list. The fixture must be a `FixtureOpts` borrowed from caller-
/// owned strings to satisfy `FixtureOpts<'a>`'s lifetime.
struct PureMembersCase {
    source: String,
    pure_members: ChunkRenames,
}

impl PureMembersCase {
    fn new(makepure_arg: &str, c_expr: &str, pure_members: &[&str]) -> Self {
        let source = format!(
            r#"import * as ns from "./vendor.js";
const a = (() => 1)();
const b = ns.makePure({makepure_arg});
const c = {c_expr} + a;
console.log(c);
export {{ a, b, c }};
"#,
        );
        Self {
            source,
            pure_members: chunk_rename_with_pure_members(
                "React",
                "ns",
                Some("import_specifier"),
                pure_members,
            ),
        }
    }

    fn opts(&self) -> FixtureOpts<'_> {
        FixtureOpts::new(
            &self.source,
            vec![logical_module("b_module", &[Member::new("b")])],
        )
        .with_chunk_renames(self.pure_members.clone())
        .with_extra_chunks(VENDOR_CHUNKS)
        .with_extra_files(VENDOR_RUNTIME_FILES)
    }
}

/// The strict schedule guard currently refuses to move this annotated
/// namespace call across the fixture's independent IIFE. Keep that exact
/// split refusal, then verify the annotation and runtime under a placement
/// that retains their original source phase.
#[test]
fn pure_members_namespace_call_stays_in_its_source_phase() {
    let case = PureMembersCase::new("{ value: 1 }", "b.value", &["makePure"]);
    assert_schedule_refusal_preserves_source_and_candidate(case.opts(), "2\n");

    let mut colocated = case.opts();
    colocated.logical_modules = vec![logical_module(
        "b_module",
        &[Member::new("a"), Member::new("b")],
    )];
    let fixture = run_fixture_with_graphs(colocated);
    assert_preserved_entry_output(&fixture, "2\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const a", "const b = "],
        &["const c"],
    );
}

/// Companion negative: the same fixture shape but the spec lists
/// a different property name. The actual `ns.makePure(...)` call
/// stays Unknown (the spec didn't annotate `makePure`), the
/// side-effect cycle closes, and the validator rejects. Pins that
/// the annotation doesn't bleed onto sibling properties.
#[test]
fn pure_members_does_not_bleed_to_other_props() {
    // Annotated property is not the one called.
    let case = PureMembersCase::new("{ value: 1 }", "b.value", &["somethingElse"]);
    expect_pure_cycle_rejection(case.opts(), "b_module");
}

/// Args are still classified independently of the spec hint — the
/// declared-purity contract covers the function value, not the
/// arguments. An impure arg (an IIFE call here) keeps the call
/// classified impure, the cycle closes, and the validator rejects.
#[test]
fn pure_members_call_with_impure_arg_does_not_admit() {
    let case = PureMembersCase::new(
        "(() => globalThis.touched = 1)()",
        "(b ? 1 : 0)",
        &["makePure"],
    );
    expect_pure_cycle_rejection(case.opts(), "b_module");
}

#[test]
fn pure_members_member_call_does_not_promote_callback_body_reads() {
    let mut pure_members = chunk_rename_with_pure_members(
        "ReactLike",
        "ns",
        Some("import_specifier"),
        &["forwardRef"],
    );
    pure_members
        .annotations
        .get_mut("ReactLike")
        .unwrap()
        .no_sync_callback_members = vec!["forwardRef".to_string()];
    let source = r#"import * as ns from "./vendor.js";
const component = ns.forwardRef(() => later);
const later = "ready";
console.log(typeof component, later);
export { component, later };
"#;
    assert_schedule_refusal_preserves_source_and_candidate(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "component_module",
                &[Member::new("component")],
            )],
        )
        .with_chunk_renames(pure_members.clone())
        .with_extra_chunks(VENDOR_CHUNKS)
        .with_extra_files(VENDOR_RUNTIME_FILES),
        "object ready\n",
    );
    let fixture = run_fixture_with_graphs(
        FixtureOpts::new(
            source,
            vec![logical_module(
                "component_module",
                &[Member::new("component"), Member::new("later")],
            )],
        )
        .with_chunk_renames(pure_members)
        .with_extra_chunks(VENDOR_CHUNKS)
        .with_extra_files(VENDOR_RUNTIME_FILES),
    );

    let graph = fixture.dependency_graph();
    let component_source_unit = source_unit_for_binding(&graph, "component");
    let later_source_unit = source_unit_for_binding(&graph, "later");
    let promoted: Vec<_> = graph
        .edges
        .iter()
        .filter(|edge| {
            edge.source == component_source_unit
                && edge.target == later_source_unit
                && edge.edge_kind == DepKind::EagerUse
                && matches!(edge.role, Some(EdgeRoleReport::PromotedAtInit { .. }))
        })
        .collect();

    assert!(
        promoted.is_empty(),
        "`ns.forwardRef(() => later)` should not promote the callback \
         body's lazy read of `later` into an at-init eager edge; got \
         {promoted:#?}\n\nFull dependency graph: {graph:#?}",
    );
    assert_preserved_entry_output(&fixture, "object ready\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/component_module.js",
        &["const component", "const later"],
        &[],
    );
}
