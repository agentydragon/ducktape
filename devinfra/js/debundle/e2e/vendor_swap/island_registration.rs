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

/// Island init the strip may drop: no residual reader can observe it, so
/// the emitted caller still runs (`keepMe` sees an untouched registry).
#[test]
fn partial_swap_drops_unobserved_island_effects() {
    for (name, chunk, absent, expected) in [
        (
            "registration of an array born in the island",
            island_registration("[\"mouseout\", \"mouseover\"]", "7"),
            &["register("][..],
            "true 7\n",
        ),
        // MobX's `var ObservableMap = (function () { … })()` shape.
        (
            "IIFE whose uncalled methods read retained state",
            "const registry = {};\n\
             const all = (function () { function Island() {} Island.prototype.peek = function () { return registry; }; return new Set([\"mouseout\"]); })();\n\
             export const e6 = () => all.has(\"mouseout\");\n\
             export const keepMe = () => Object.keys(registry).length;\n"
                .to_owned(),
            &[],
            "true 0\n",
        ),
        // React DOM's DevTools injection: host code calling back into the
        // internals is outside the analysed program, so `fill` is not
        // assumed to run.
        (
            "internals handed to a host hook",
            "const registry = {};\n\
             const all = new Set([\"mouseout\"]);\n\
             const internals = { all, fill(name) { registry[name] = true; } };\n\
             if (typeof globalThis.__HOOK__ !== \"undefined\") { var hook = globalThis.__HOOK__; hook.inject(internals); }\n\
             export const e6 = () => internals.all.has(\"mouseout\");\n\
             export const keepMe = () => Object.keys(registry).length;\n"
                .to_owned(),
            &[],
            "true 0\n",
        ),
        // The `t instanceof mh` / `x === K` shape cannot mutate the state.
        (
            "call comparing retained state by identity",
            "const registry = {};\n\
             const all = new Set();\n\
             function isRegistry(value) { return value === registry || typeof registry === value; }\n\
             function register(name) { if (!isRegistry(name)) all.add(name); }\n\
             register(\"mouseout\");\n\
             export const e6 = () => all.has(\"mouseout\");\n\
             export const keepMe = () => Object.keys(registry).length;\n"
                .to_owned(),
            &[],
            "true 0\n",
        ),
        (
            "primitive array iteration without invoking the typeof operand",
            "const registry = {};\nfunction dormant() { registry.hit = true; }\nconst entries = [typeof dormant];\nconst all = entries.map(entry => entry);\nexport const e6 = () => all.length > 0;\nexport const keepMe = () => Object.keys(registry).length;\n"
                .to_owned(),
            &[],
            "true 0\n",
        ),
    ] {
        in_case(name, || {
            let fixture = zod_swap(&chunk, LOGGING_CALLER, ZOD_TRUE);
            fixture.assert_zod_caller_output(ZOD_TRUE, expected);
            assert_source_file(&fixture.megachunk_emitted_path, &[], absent);
        });
    }
}

/// Island init a residual reader observes: dropping it would change what
/// `keepMe` prints, so the strip must refuse and name the retained state.
#[test]
fn partial_swap_rejects_island_effects_a_residual_reader_observes() {
    for (name, chunk, diagnostics) in [
        (
            "registration of a scalar",
            island_registration("\"mouseout\"", "Object.keys(registry).join(\",\")"),
            &["`registry`"][..],
        ),
        (
            "registration of a fresh array",
            island_registration(
                "[\"mouseout\", \"mouseover\"]",
                "Object.keys(registry).join(\",\")",
            ),
            &["`registry`"],
        ),
        // The alias is dropped with the island; the registry it points at is not.
        (
            "registration through an island alias",
            "const registry = {};\n\
             const view = registry;\n\
             const all = new Set();\n\
             function register(name) { view[name] = true; all.add(name); }\n\
             register(\"onMouseEnter\");\n\
             export const e6 = () => all.has(\"onMouseEnter\");\n\
             export const keepMe = () => Object.keys(registry).join(\",\");\n"
                .to_owned(),
            &["`registry`"],
        ),
        // Anchor for the host-hook case: an unresolved chunk call may run `fill`.
        (
            "internals handed to chunk code",
            "const registry = {};\n\
             const all = new Set([\"mouseout\"]);\n\
             const internals = { all, fill(name) { registry[name] = true; } };\n\
             const local = { inject(target) { target.fill(\"onMouseEnter\"); } };\n\
             local.inject(internals);\n\
             export const e6 = () => internals.all.has(\"mouseout\");\n\
             export const keepMe = () => Object.keys(registry).join(\",\");\n"
                .to_owned(),
            &[],
        ),
        // One run reports every refused side effect, not only the first.
        (
            "two refused registrations",
            "const first = {};\n\
             const second = {};\n\
             const all = new Set();\n\
             function register(target, name) { target[name] = true; all.add(name); }\n\
             register(first, \"a\");\n\
             register(second, \"b\");\n\
             export const e6 = () => all.has(\"a\");\n\
             export const keepMe = () => Object.keys(first).concat(Object.keys(second)).join(\",\");\n"
                .to_owned(),
            &["`first`", "`second`"],
        ),
        // Anchor for the uncalled-methods IIFE: this body runs at init.
        (
            "IIFE calling its own registration",
            "const registry = {};\n\
             const all = (function () { function fill(name) { registry[name] = true; } fill(\"onMouseEnter\"); return new Set([\"mouseout\"]); })();\n\
             export const e6 = () => all.has(\"mouseout\");\n\
             export const keepMe = () => Object.keys(registry).join(\",\");\n"
                .to_owned(),
            &["`registry`"],
        ),
    ] {
        in_case(name, || {
            assert_rejected(
                &zod_swap(&chunk, LOGGING_CALLER, ZOD_TRUE).result,
                diagnostics,
            );
        });
    }
}
