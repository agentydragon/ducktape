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

use analysis::{DepKind, EdgeRoleReport};
use debundle_e2e_support::*;
use spec::ChunkRenames;

const VENDOR_FILE: &[(&str, &str)] = &[(
    "static/app/vendor.js",
    "export function makePure(arg) { return arg; }\n\
     export function forwardRef(render) { return { render }; }\n",
)];

fn namespace_rename(name: &str, pure_members: &[&str]) -> ChunkRenames {
    let mut renames =
        chunk_renames(&[ChunkRenameEntry::new(name, "ns").with_kind("import_specifier")]);
    renames
        .annotations
        .entry(name.to_string())
        .or_default()
        .pure_members = pure_members
        .iter()
        .map(|name| (*name).to_string())
        .collect();
    renames
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
            pure_members: namespace_rename("React", pure_members),
        }
    }

    fn opts(&self) -> FixtureOpts<'_> {
        FixtureOpts::new(
            &self.source,
            vec![logical_module("b_module", &[Member::new("b")])],
        )
        .with_chunk_renames(self.pure_members.clone())
        .with_extra_files(VENDOR_FILE)
    }
}

/// Vendor-namespace fixture: a star-import binding `ns` whose
/// `ns.makePure(...)` call site would force a side-effect-order
/// edge under default classification (`ns.makePure` is a member call
/// on an unknown receiver). With `pure_members: [makePure]` on the
/// `ns` member, the analyzer admits the call as pure, drops the
/// `S` edge, and the cycle-forcing fixture accepts.
///
/// Without the rule: `ns.makePure(...)` is Unknown → `b` is SE →
/// `b → a` s-edge → after peeling `b` to b_module, that crosses
/// `b_module → residual`. Combined with `residual → b_module`
/// (`c` reads `b` at init), the spec is unrealizable.
///
/// With the rule: `ns.makePure(...)` is Pure → `b` is not SE →
/// no `b → a` s-edge. Only `residual → b_module` remains. DAG.
#[test]
fn pure_members_admits_namespace_member_call() {
    let case = PureMembersCase::new("{ value: 1 }", "b.value", &["makePure"]);

    // Peel succeeded: b is in b_module without dragging a.
    // The fact that the build didn't error on a cycle proves
    // that `ns.makePure(...)` was classified Pure.
    assert_pure_cycle_break_with_opts(
        case.opts(),
        "b_module",
        &["const b = "],
        &["const a", "(()=>1)"],
        "2\n",
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
    let mut pure_members = namespace_rename("ReactLike", &["forwardRef"]);
    pure_members
        .annotations
        .get_mut("ReactLike")
        .unwrap()
        .no_sync_callback_members = vec!["forwardRef".to_string()];
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"import * as ns from "./vendor.js";
const component = ns.forwardRef(() => later);
const later = "ready";
console.log(typeof component, later);
export { component, later };
"#,
            vec![logical_module(
                "component_module",
                &[Member::new("component")],
            )],
        )
        .with_chunk_renames(pure_members)
        .with_extra_files(VENDOR_FILE),
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
    assert_entry_output(&fixture, "object ready\n");
}
