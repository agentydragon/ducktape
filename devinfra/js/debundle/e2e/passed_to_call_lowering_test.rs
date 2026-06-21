//! End-to-end coverage for `passed_to_call` member selectors through the **full
//! lowering pipeline** — the `resolves_to`-of-argument primitive. It pins a target
//! by the target binding being **passed as an argument** to a call of a known
//! callee (`registry.register(Target)`) — rather than by the target's own body,
//! its own use sites (`reads_member` / `member_of_module`), or its own minified
//! name. This is the inverse direction of `member_of_module`: that primitive pins
//! the owner whose *own* subtree consumes `mod.X` and explicitly cannot reach a
//! target distinguished only by an external registration statement (the call site
//! declares nothing, so its `declares` conjunct excludes it). This primitive closes
//! that gap.
//!
//! Unlike `selector_solve_test` (which exercises the kernel on a synthetic owner
//! graph), these tests drive the real `debundle` binary: the spec carries
//! `passed_to_call` selectors, the call-argument facts are derived from the chunk
//! AST and joined to each argument's declaring owner, and we assert the resolved
//! binding lands in the right module and the emitted tree runs under Node.
//!
//! ## The registry-distinguished empty class (the headline case)
//!
//! The headline shape is a top-level **empty** class with no internal anchor of
//! its own, distinguished *only* by an external `registry.register(C)` statement —
//! exactly the registry abort bar the use-site primitives could not reach
//! (debug/2026_06_19_p4_debt_worklist.md). The argument-pass in the registration
//! call is what the `passed_to_call` EDB rides.

use debundle_e2e_support::*;

/// **The registry-distinguished empty class, end to end.** An empty top-level
/// class is registered via `registry.register(FooService)` in a separate
/// statement; it has no internal anchor. `passed_to_call` pins it by the call that
/// names it — `register` off `@registry` — never by the minified class name. A
/// second empty class registered under a different callee member must not
/// interfere.
#[test]
fn passed_to_call_pins_registry_distinguished_empty_class() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const registry = { register(x) { (this.items ||= []).push(x); }, addPlugin(x) { (this.plugins ||= []).push(x); } };
class FooService {}
class BarPlugin {}
registry.register(FooService);
registry.addPlugin(BarPlugin);
console.log(new FooService().constructor.name, new BarPlugin().constructor.name);
export { registry, FooService, BarPlugin };
"#,
        vec![
            logical_module("registry", &[Member::renamed("appRegistry", "registry")]),
            logical_module(
                "services",
                &[Member::passed_to_call(
                    "FooServiceClass",
                    "register",
                    Some("appRegistry"),
                    None,
                    Some("class_declaration"),
                )],
            ),
        ],
    ));

    // The empty class resolved to the minified binding `FooService` (the `source
    // bindings` provenance comment proves it) and lives in the `services` module
    // under its readable export name — pinned purely by the registration call,
    // never by the class name. `BarPlugin` (registered under a different callee
    // member) was correctly left out.
    assert_module_exports(
        &fixture.out_root,
        "static/app/modules/services.js",
        &["FooServiceClass"],
        &["BarPlugin", "FooService"],
    );
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/services.js",
        &["source bindings: FooService", "class FooServiceClass"],
        &["source bindings: BarPlugin"],
    );
    assert_entry_output(&fixture, "FooServiceClass BarPlugin\n");
}

/// Object-constrained disambiguation: two empty classes are each passed to a
/// `.register` member, but off different registries. The callee-member-only query
/// is ambiguous; the `object: @Anchor` constraint picks out exactly the one passed
/// to `@viewRegistry.register`.
#[test]
fn passed_to_call_constrains_by_object_through_full_pipeline() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const viewRegistry = { register(x) { (this.v ||= []).push(x); } };
const commandRegistry = { register(x) { (this.c ||= []).push(x); } };
class TableView {}
class DeleteCommand {}
viewRegistry.register(TableView);
commandRegistry.register(DeleteCommand);
console.log(new TableView().constructor.name, new DeleteCommand().constructor.name);
export { viewRegistry, commandRegistry, TableView, DeleteCommand };
"#,
        vec![
            logical_module(
                "registries",
                &[Member::new("viewRegistry"), Member::new("commandRegistry")],
            ),
            logical_module(
                "views",
                &[Member::passed_to_call(
                    "TableViewComponent",
                    "register",
                    Some("viewRegistry"),
                    None,
                    None,
                )],
            ),
        ],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/views.js",
        &["source bindings: TableView", "class TableViewComponent"],
        &["source bindings: DeleteCommand"],
    );
    assert_entry_output(&fixture, "TableViewComponent DeleteCommand\n");
}

/// Argument-position disambiguation: the target `Widget` is the *second* argument
/// of `host.define("widget", Widget)` (the string literal occupies index 0, which
/// names no binding). The `arg_index: 1` constraint pins exactly the target at that
/// position — proving the index rides through the full pipeline.
#[test]
fn passed_to_call_constrains_by_arg_index_through_full_pipeline() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const host = { define(name, ctor) { (this.m ||= {})[name] = ctor; } };
class Widget {}
host.define("widget", Widget);
console.log(new Widget().constructor.name);
export { host, Widget };
"#,
        vec![logical_module(
            "widgets",
            &[Member::passed_to_call(
                "WidgetClass",
                "define",
                None,
                Some(1),
                None,
            )],
        )],
    ));

    assert_module_source(
        &fixture.out_root,
        "static/app/modules/widgets.js",
        &["source bindings: Widget", "class WidgetClass"],
        &[],
    );
    assert_entry_output(&fixture, "WidgetClass\n");
}

/// Fail-closed: a `passed_to_call` whose callee member is the argument of **two**
/// declaring owners is ambiguous, so it errors rather than guessing one.
#[test]
fn passed_to_call_ambiguous_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const r = { register(x) { (this.i ||= []).push(x); } };
class A {}
class B {}
r.register(A);
r.register(B);
console.log(new A().constructor.name, new B().constructor.name);
export { r, A, B };
"#,
            vec![logical_module(
                "things",
                &[Member::passed_to_call(
                    "Thing", "register", None, None, None,
                )],
            )],
        ),
        &["passed_to_call", "register"],
    );
}

/// Fail-closed: a `passed_to_call` whose callee member no call passes a declaring
/// owner to errors rather than silently dropping the claim.
#[test]
fn passed_to_call_zero_match_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const r = { register(x) { (this.i ||= []).push(x); } };
class A {}
r.register(A);
console.log(new A().constructor.name);
export { r, A };
"#,
            vec![logical_module(
                "things",
                &[Member::passed_to_call(
                    "Thing",
                    "absentCallee",
                    None,
                    None,
                    None,
                )],
            )],
        ),
        &["passed_to_call", "absentCallee"],
    );
}

/// Fail-closed: a `passed_to_call` whose `object: @Anchor` does not name a resolved
/// member in the chunk errors rather than ignoring the object constraint.
#[test]
fn passed_to_call_unknown_object_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const r = { register(x) { (this.i ||= []).push(x); } };
class A {}
r.register(A);
console.log(new A().constructor.name);
export { r, A };
"#,
            vec![logical_module(
                "things",
                &[Member::passed_to_call(
                    "Thing",
                    "register",
                    Some("notAMember"),
                    None,
                    None,
                )],
            )],
        ),
        &["passed_to_call", "notAMember"],
    );
}

/// Fail-closed: the argument naming the target must be a chunk-top declaration.
/// When the registration passes a non-declared value (a `new X()` expression, not
/// a bare class binding), no call-argument row names a declaring owner, so the
/// selector finds nothing and errors — never mis-resolving to an unrelated owner.
#[test]
fn passed_to_call_non_identifier_argument_fails_closed() {
    expect_rejection_containing_all(
        FixtureOpts::new(
            r#"const r = { register(x) { (this.i ||= []).push(x); } };
class A {}
r.register(new A());
console.log("ok");
export { r, A };
"#,
            vec![logical_module(
                "things",
                &[Member::passed_to_call(
                    "Thing", "register", None, None, None,
                )],
            )],
        ),
        &["passed_to_call", "register"],
    );
}
