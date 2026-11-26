//! Partial swaps rewrite swapped names to the upstream namespace and keep residual behavior.

use super::*;

#[test]
fn partial_swap_basic_rewrites_to_namespace_member() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        caller_source: "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean() && kept(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert_success(&fixture.result);

    assert_source_file(
        &fixture.caller_emitted_path,
        &[
            "import * as z from \"zod\"",
            "z.boolean()",
            "keepMe as kept",
            "kept()",
        ],
        &["zodBoolean"],
    );
}

// ─── strip swapped exports ───────────────────────────────────────────────
//
// After the consumer side is rewritten (lowering construction + the
// pass-through emission rewriter), the strip pass drops the swapped names from the
// vendor chunk's own export surface and sweeps any top-level bindings
// that are no longer reachable. These tests cover both halves of that
// pass against the synthetic megachunk fixture.

#[test]
fn partial_swap_strips_swapped_names_from_export_block() {
    // `export { e6, keepMe }` form: stripping `e6` should rewrite the
    // export block to expose only `keepMe`. Bare `export const e6 = …`
    // is exercised by the implementation-DCE tests below.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "const e6Impl = () => true;\nconst keepMeImpl = () => 7;\nexport { e6Impl as e6, keepMeImpl as keepMe };\n",
        caller_source: "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean() && kept(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert_success(&fixture.result);
    assert_source_file(
        &fixture.megachunk_emitted_path,
        &["keepMeImpl as keepMe"],
        &["e6Impl as e6"],
    );
}

#[test]
fn partial_swap_strips_implementation_when_unreferenced() {
    // `export const e6 = …` with no cross-refs: after stripping the
    // export prefix, `const e6 = …` becomes a chunk-local pure binding
    // that nothing reads, so the DCE pass deletes it entirely.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        caller_source: "import { e6 as zodBoolean, keepMe as kept } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean() && kept(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert_success(&fixture.result);
    assert_source_file(&fixture.megachunk_emitted_path, &["keepMe"], &[" e6 "]);
}

#[test]
fn partial_swap_rejects_split_brain_residual_reachability() {
    // `object` (swapped) references `ZodObject`; `ZodObject` is still
    // exported. Keeping it would leave a residual copy of the old
    // package implementation reachable next to the replacement import.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "class ZodObject {}\nconst object = () => new ZodObject();\nexport { object, ZodObject };\n",
        caller_source: "import { object as zodObject, ZodObject as Z } from \"../megachunk/entry.js\";\nexport function go() { return zodObject() instanceof Z; }\n",
        upstream_source: "export const object = () => ({});\n",
        symbols: vec![("object", "zod", "object")],
        upstream_version: "3.23.8",
    });

    assert_rejected(
        &fixture.result,
        &[
            "split-brain vendor swap",
            "swapped path: swapped export `object`",
            "dependency of item",
        ],
    );
}

#[test]
fn partial_swap_separates_calls_to_same_default_unwrapper() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "const state = {};\n\
             (function (cell) { cell.value = 7; })(state);\n\
             function unwrap(value) { return value && value.__esModule ? value.default : value; }\n\
             function selected() { return 1; }\n\
             function other() { return state.value; }\n\
             const selectedValue = unwrap(selected);\n\
             const otherValue = unwrap(other);\n\
             export const e6 = () => selectedValue();\n\
             export const keepMe = () => otherValue();\n",
        caller_source: "import { e6, keepMe } from '../megachunk/entry.js';\n\
             console.log(e6(), keepMe());\n",
        upstream_source: "export const boolean = () => 1;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });
    assert_success(&fixture.result);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => 1;\n",
    );
    assert_node_output(&fixture.caller_emitted_path, "1 7\n", "");
}

#[test]
fn partial_swap_rejects_function_default_returning_shared_callback() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "const state = {};\n\
             (function (cell) { cell.value = 7; })(state);\n\
             function unwrap(value) { return value && value.__esModule ? value.default : value; }\n\
             function selected() { return 1; }\n\
             function other() { return state.value; }\n\
             selected.__esModule = true;\nconst alias = selected;\nalias.default = other;\n\
             const selectedValue = unwrap(selected);\n\
             export const e6 = () => selectedValue();\n\
             export const keepMe = () => other();\n",
        caller_source: "import { e6, keepMe } from '../megachunk/entry.js';\n\
             console.log(e6(), keepMe());\n",
        upstream_source: "export const boolean = () => 1;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });
    assert_rejected(&fixture.result, &["split-brain vendor swap"]);
}

#[test]
fn partial_swap_separates_unwrapper_inputs_after_local_property_write() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "const state = {};\n\
             (function (cell) { cell.value = 7; })(state);\n\
             function unwrap(value) { return value && value.__esModule ? value.default : value; }\n\
             const api = {};\n\
             (function (target) { target.get = function selected() { return 1; }; })(api);\n\
             function other() { return state.value; }\n\
             const selectedValue = unwrap(api.get);\nconst otherValue = unwrap(other);\n\
             export const e6 = () => selectedValue();\n\
             export const keepMe = () => otherValue();\n",
        caller_source: "import { e6, keepMe } from '../megachunk/entry.js';\n\
             console.log(e6(), keepMe());\n",
        upstream_source: "export const boolean = () => 1;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });
    assert_success(&fixture.result);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => 1;\n",
    );
    assert_node_output(&fixture.caller_emitted_path, "1 7\n", "");
}

#[test]
fn partial_swap_rejects_accessor_default_returning_shared_callback() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "const state = {};\n\
             (function (cell) { cell.value = 7; })(state);\n\
             function unwrap(value) { return value && value.__esModule ? value.default : value; }\n\
             function selected() { return 1; }\n\
             function other() { return state.value; }\n\
             const api = {};\napi.get = selected;\n\
             selected.__esModule = true;\n\
             Object.defineProperty(selected, 'default', { get() { return other; } });\n\
             const selectedValue = unwrap(api.get);\n\
             export const e6 = () => selectedValue();\n\
             export const keepMe = () => other();\n",
        caller_source: "import { e6, keepMe } from '../megachunk/entry.js';\n\
             console.log(e6(), keepMe());\n",
        upstream_source: "export const boolean = () => 1;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });
    assert_rejected(&fixture.result, &["split-brain vendor swap"]);
}

#[test]
fn partial_swap_keeps_callbacks_installed_through_local_writes() {
    let cases = [
        (
            "binding reassign",
            "let selected = function initial() { return 1; };\nselected = other;",
            "selected()",
        ),
        (
            "alias property write",
            "const box = { callback: function initial() { return 1; } };\n\
             const alias = box;\nalias.callback = other;",
            "box.callback()",
        ),
        (
            "returned closure",
            "function make(callback) { return () => callback(); }\n\
             const selected = make(other);",
            "selected()",
        ),
    ];
    for (name, setup, call) in cases {
        let chunk_source = format!(
            "const state = {{}};\n\
             (function (cell) {{ cell.value = 7; }})(state);\n\
             function other() {{ return state.value; }}\n\
             {setup}\n\
             export const e6 = () => {call};\n\
             export const keepMe = () => other();\n"
        );
        let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
            chunk_source: &chunk_source,
            caller_source: "import { e6, keepMe } from '../megachunk/entry.js';\n\
                 console.log(e6(), keepMe());\n",
            upstream_source: "export const boolean = () => 1;\n",
            symbols: vec![("e6", "zod", "boolean")],
            upstream_version: "3.23.8",
        });
        assert!(
            !fixture.result.status.success(),
            "{name}: shared callback must prevent partial swap",
        );
        assert!(
            fixture.result.stderr.contains("split-brain vendor swap")
                || fixture
                    .result
                    .stderr
                    .contains("observable side-effect item"),
            "{name}: expected ownership rejection in stderr:\n{}",
            fixture.result.stderr,
        );
    }
}

#[test]
fn partial_swap_keeps_side_effect_init() {
    // Top-level hard side effects must stay
    // even when its only "reference" is a swapped binding.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "console.log(\"keep side effect\");\nexport const e6 = () => true;\n",
        caller_source: "import { e6 as zodBoolean } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert_success(&fixture.result);
    assert_source_file(&fixture.megachunk_emitted_path, &["console.log"], &[" e6 "]);
}

#[test]
fn partial_swap_preserves_escaped_synchronous_callback_effect() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\n\
             const callbacks = [];\n\
             function register(callback) { callbacks.push(callback); }\n\
             register(() => { globalThis.__vendor_callback_probe = 'called'; });\n\
             callbacks[0]();\n\
             export const keepMe = () => globalThis.__vendor_callback_probe;\n",
        caller_source: "import { e6, keepMe } from '../megachunk/entry.js';\n\
             console.log(e6(), keepMe());\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });
    assert_success(&fixture.result);
    let app_root = emitted_app_root(&fixture);
    install_node_module(
        &app_root,
        "zod",
        "3.23.8",
        "export const boolean = () => true;\n",
    );
    assert_node_output(&fixture.caller_emitted_path, "true called\n", "");
}

#[test]
fn partial_swap_rejects_observable_side_effect_reading_swapped_binding() {
    // A `Hard`, globally-observable side effect (`globalThis.__zodBoolean =
    // e6`) that *reads* the swapped binding `e6`. The read makes the
    // statement swap-reachable, so the keep-pass declines to honor it for its
    // side effect and it would otherwise be silently dropped — even though
    // residual / external code can witness the global write via
    // `globalThis.__zodBoolean`. The strip pass must bail rather than ship a
    // bundle missing the effect.
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\nglobalThis.__zodBoolean = e6;\n",
        caller_source: "import { e6 as zodBoolean } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        upstream_version: "3.23.8",
    });

    assert_rejected(
        &fixture.result,
        &["observable side-effect item", "swap-reachable"],
    );
}

#[test]
fn partial_swap_rejects_version_mismatch() {
    let fixture = run_partial_swap_fixture(PartialSwapFixtureArgs {
        chunk_source: "export const e6 = () => true;\n",
        caller_source: "import { e6 as zodBoolean } from \"../megachunk/entry.js\";\nexport function go() { return zodBoolean(); }\n",
        upstream_source: "export const boolean = () => true;\n",
        symbols: vec![("e6", "zod", "boolean")],
        // Mismatch: spec wants 9.9.9 but the on-disk package.json below
        // pins to 3.23.8.
        upstream_version: "9.9.9",
    });

    assert_rejected(&fixture.result, &["version mismatch"]);
}
