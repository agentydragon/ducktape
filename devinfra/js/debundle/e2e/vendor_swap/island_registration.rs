//! Partial swaps drop island registrations only when no residual reader can observe them.

use super::*;

/// React DOM's event-registry setup (`p3("onMouseEnter", ["mouseout",
/// "mouseover"])`): an island function fills island state from an argument.
fn island_registration(argument: &str, residual_reader: &str) -> String {
    format!(
        "const registry = {{}};\n\
         const all = new Set();\n\
         function register(name, deps) {{ registry[name] = deps; for (let i = 0; i < deps.length; i++) all.add(deps[i]); }}\n\
         register(\"onMouseEnter\", {argument});\n\
         export const e6 = () => all.has(\"mouseout\");\n\
         export const keepMe = () => {residual_reader};\n"
    )
}

#[test]
fn partial_swap_drops_island_registration_of_an_array_born_in_the_island() {
    let fixture =
        run_island_registration_fixture(&island_registration("[\"mouseout\", \"mouseover\"]", "7"));
    assert_success(&fixture.result);
    assert_source_file(&fixture.megachunk_emitted_path, &[], &["register("]);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => true;\n",
    );
    assert_node_output(&fixture.caller_emitted_path, "true 7\n", "");
}

/// Dropping the island call would empty the registry residual code prints
/// (`true onMouseEnter` would become `true `), so the strip must refuse,
/// whether the call hands over a scalar or a fresh array.
#[test]
fn partial_swap_rejects_island_registration_that_a_residual_reader_observes() {
    for argument in ["\"mouseout\"", "[\"mouseout\", \"mouseover\"]"] {
        let fixture = run_island_registration_fixture(&island_registration(
            argument,
            "Object.keys(registry).join(\",\")",
        ));
        assert_rejected_case(argument, &fixture.result, &["`registry`"]);
    }
}

/// MobX's `var ObservableMap = (function () { … })()` shape: an island IIFE
/// whose methods read retained state but never run at init.
#[test]
fn partial_swap_drops_island_iife_whose_uncalled_methods_read_retained_state() {
    let fixture = run_island_registration_fixture(
        "const registry = {};\n\
         const all = (function () { function Island() {} Island.prototype.peek = function () { return registry; }; return new Set([\"mouseout\"]); })();\n\
         export const e6 = () => all.has(\"mouseout\");\n\
         export const keepMe = () => Object.keys(registry).length;\n",
    );
    assert_success(&fixture.result);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => true;\n",
    );
    assert_node_output(&fixture.caller_emitted_path, "true 0\n", "");
}

/// The island fills the registry through an alias it declares; the alias is
/// dropped with the island, the registry it points at is not.
#[test]
fn partial_swap_rejects_island_registration_through_an_alias() {
    let fixture = run_island_registration_fixture(
        "const registry = {};\n\
         const view = registry;\n\
         const all = new Set();\n\
         function register(name) { view[name] = true; all.add(name); }\n\
         register(\"onMouseEnter\");\n\
         export const e6 = () => all.has(\"onMouseEnter\");\n\
         export const keepMe = () => Object.keys(registry).join(\",\");\n",
    );
    assert_rejected(&fixture.result, &["`registry`"]);
}

/// React DOM's DevTools injection: island internals handed to a host global.
/// Host code calling back into them is outside the analysed program, so the
/// method that would fill the retained registry is not assumed to run.
#[test]
fn partial_swap_drops_island_internals_handed_to_a_host_hook() {
    let fixture = run_island_registration_fixture(
        "const registry = {};\n\
         const all = new Set([\"mouseout\"]);\n\
         const internals = { all, fill(name) { registry[name] = true; } };\n\
         if (typeof globalThis.__HOOK__ !== \"undefined\") { var hook = globalThis.__HOOK__; hook.inject(internals); }\n\
         export const e6 = () => internals.all.has(\"mouseout\");\n\
         export const keepMe = () => Object.keys(registry).length;\n",
    );
    assert_success(&fixture.result);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => true;\n",
    );
    assert_node_output(&fixture.caller_emitted_path, "true 0\n", "");
}

/// Anchor: the same internals handed to a chunk function, whose call is
/// unresolved, may run `fill`, so the strip refuses.
#[test]
fn partial_swap_rejects_island_internals_handed_to_chunk_code() {
    let fixture = run_island_registration_fixture(
        "const registry = {};\n\
         const all = new Set([\"mouseout\"]);\n\
         const internals = { all, fill(name) { registry[name] = true; } };\n\
         const local = { inject(target) { target.fill(\"onMouseEnter\"); } };\n\
         local.inject(internals);\n\
         export const e6 = () => internals.all.has(\"mouseout\");\n\
         export const keepMe = () => Object.keys(registry).join(\",\");\n",
    );
    assert_rejected(&fixture.result, &[]);
}

/// An island call that only compares retained state by identity (the
/// `t instanceof mh` / `x === K` shape) cannot mutate it.
#[test]
fn partial_swap_drops_island_call_comparing_retained_state_by_identity() {
    let fixture = run_island_registration_fixture(
        "const registry = {};\n\
         const all = new Set();\n\
         function isRegistry(value) { return value === registry || typeof registry === value; }\n\
         function register(name) { if (!isRegistry(name)) all.add(name); }\n\
         register(\"mouseout\");\n\
         export const e6 = () => all.has(\"mouseout\");\n\
         export const keepMe = () => Object.keys(registry).length;\n",
    );
    assert_success(&fixture.result);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => true;\n",
    );
    assert_node_output(&fixture.caller_emitted_path, "true 0\n", "");
}

/// One run reports every refused side effect, not only the first.
#[test]
fn partial_swap_reports_every_refused_registration() {
    let fixture = run_island_registration_fixture(
        "const first = {};\n\
         const second = {};\n\
         const all = new Set();\n\
         function register(target, name) { target[name] = true; all.add(name); }\n\
         register(first, \"a\");\n\
         register(second, \"b\");\n\
         export const e6 = () => all.has(\"a\");\n\
         export const keepMe = () => Object.keys(first).concat(Object.keys(second)).join(\",\");\n",
    );
    assert_rejected(&fixture.result, &["`first`", "`second`"]);
}

/// The same IIFE calling a function it declares, which fills the registry a
/// residual reader prints: that body runs at init, so the strip must refuse.
#[test]
fn partial_swap_rejects_island_iife_calling_its_own_registration() {
    let fixture = run_island_registration_fixture(
        "const registry = {};\n\
         const all = (function () { function fill(name) { registry[name] = true; } fill(\"onMouseEnter\"); return new Set([\"mouseout\"]); })();\n\
         export const e6 = () => all.has(\"mouseout\");\n\
         export const keepMe = () => Object.keys(registry).join(\",\");\n",
    );
    assert_rejected(&fixture.result, &["`registry`"]);
}

#[test]
fn partial_swap_drops_original_package_island_with_local_mutations() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "class ZodBoolean {}\nZodBoolean.displayName = \"ZodBoolean\";\nconst e6 = () => new ZodBoolean();\nexport { e6 };\n",
        caller_source: "import { e6 as zodBoolean } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert_success(&fixture.result);
    assert_source_file(
        &fixture.megachunk_emitted_path,
        &[],
        &["ZodBoolean", "displayName"],
    );
}
