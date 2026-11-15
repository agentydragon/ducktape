//! End-to-end coverage for `passed_to_call` member selectors through the full
//! lowering pipeline. The selector pins a top-level declaration by its binding
//! being passed to a method call, with optional receiver, argument-position and
//! declaration-kind constraints. It does not claim the registration statement.
//!
//! Use-site `source_matches[]` covers simple registrations too; see
//! `use_site_pinning_test.rs`. The final test covers the distinct nonlocal join:
//! neither the declaration shape nor the call shape alone identifies the class,
//! while the call relation combined with its declaration kind does.

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
fn passed_to_call_constrains_by_object() {
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

/// Argument-position disambiguation: `Widget` and `Mixin` are the second and third
/// arguments of `host.define("widget", Widget, Mixin)` (the string literal occupies
/// index 0, which names no binding). The callee member alone is ambiguous between
/// the two; the `arg_index: 1` constraint pins exactly the target at that position.
#[test]
fn passed_to_call_constrains_by_arg_index() {
    let fixture = run_fixture(FixtureOpts::new(
        r#"const host = { define(name, ctor, mixin) { (this.m ||= {})[name] = [ctor, mixin]; } };
class Widget {}
class Mixin {}
host.define("widget", Widget, Mixin);
console.log(new Widget().constructor.name, new Mixin().constructor.name);
export { host, Widget, Mixin };
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
        &["source bindings: Mixin"],
    );
    assert_entry_output(&fixture, "WidgetClass Mixin\n");
}

/// Fail-closed: a `passed_to_call` whose callee member is the argument of **two**
/// declaring source_units is ambiguous, so it errors rather than guessing one.
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
/// source_unit to errors rather than silently dropping the claim.
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
/// a bare class binding), no call-argument row names a declaring source_unit, so the
/// selector finds nothing and errors — never mis-resolving to an unrelated source_unit.
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

/// A use-site shape alone sees both the class and function argument; a class
/// shape alone sees both empty classes. The relational kind constraint joins
/// the distant registration call to the declaration without owning the call.
#[test]
fn passed_to_call_filters_declaration_kind_without_adjacency() {
    const SOURCE: &str = r#"const registry = { register(x) { (this.items ||= []).push(x); } };
class A {}
class B {}
function helper() {}
console.log("before registration");
registry.register(A);
registry.register(helper);
console.log(registry.items[0].name, registry.items.length, new B().constructor.name);
export { registry, A, B, helper };
"#;
    let fixture = run_fixture(FixtureOpts::new(
        SOURCE,
        vec![logical_module(
            "services",
            &[Member::passed_to_call(
                "ServiceClass",
                "register",
                None,
                None,
                Some("class_declaration"),
            )],
        )],
    ));
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/services.js",
        &["class ServiceClass"],
        &["class B", "function helper", ".register("],
    );
    assert_entry_output(&fixture, "before registration\nServiceClass 2 B\n");

    for template in ["registry.register(Target);", "class Target {}"] {
        let rejected = run_dry_run_rejection_fixture(FixtureOpts::new(
            SOURCE,
            vec![logical_module_with_binding_groups(
                "services",
                &[],
                &[BindingGroup::source_alpha(
                    template,
                    &[("Target", "ServiceClass")],
                )],
            )],
        ));
        let outcomes = read_selector_outcomes(&rejected.report_root);
        find_outcome(&outcomes, "ambiguous", "ServiceClass");
    }
}
