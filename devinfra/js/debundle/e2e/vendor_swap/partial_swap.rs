//! Partial swaps rewrite swapped names to the upstream namespace and keep residual behavior.

use super::*;

#[test]
fn partial_swap_basic_rewrites_to_namespace_member() {
    let fixture = zod_swap(
        "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
        ALIASED_CALLER,
        ZOD_TRUE,
    );
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

#[test]
fn partial_swap_strip_rewrites_vendor_chunk_source() {
    for (name, chunk, caller, present, absent) in [
        (
            "swapped name leaves the export block",
            "const e6Impl = () => true;\nconst keepMeImpl = () => 7;\nexport { e6Impl as e6, keepMeImpl as keepMe };\n",
            ALIASED_CALLER,
            &["keepMeImpl as keepMe"][..],
            &["e6Impl as e6"][..],
        ),
        // Once `export` is stripped, `const e6 = …` is an unread pure local.
        (
            "unreferenced implementation is deleted",
            "export const e6 = () => true;\nexport const keepMe = () => 7;\n",
            ALIASED_CALLER,
            &["keepMe"],
            &[" e6 "],
        ),
        // A top-level hard side effect stays even when its only
        // "reference" is a swapped binding.
        (
            "side-effect init is kept",
            "console.log(\"keep side effect\");\nexport const e6 = () => true;\n",
            SWAPPED_ONLY_CALLER,
            &["console.log"],
            &[" e6 "],
        ),
        (
            "original package island with local mutations is dropped",
            "class ZodBoolean {}\nZodBoolean.displayName = \"ZodBoolean\";\nconst e6 = () => new ZodBoolean();\nexport { e6 };\n",
            SWAPPED_ONLY_CALLER,
            &[],
            &["ZodBoolean", "displayName"],
        ),
    ] {
        in_case(name, || {
            let fixture = zod_swap(chunk, caller, ZOD_TRUE);
            assert_success(&fixture.result);
            assert_source_file(&fixture.megachunk_emitted_path, present, absent);
        });
    }
}

#[test]
fn partial_swap_rejects_split_brain_residual_reachability() {
    // `object` (swapped) references `ZodObject`; `ZodObject` is still
    // exported. Keeping it would leave a residual copy of the old
    // package implementation reachable next to the replacement import.
    let fixture = run_partial_swap_fixture(
        "class ZodObject {}\nconst object = () => new ZodObject();\nexport { object, ZodObject };\n",
        "import { object as zodObject, ZodObject as Z } from \"../megachunk/entry.js\";\nexport function go() { return zodObject() instanceof Z; }\n",
        "export const object = () => ({});\n",
        ("object", "object"),
        "3.23.8",
    );
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
fn partial_swap_rejects_version_mismatch() {
    // The spec requests 9.9.9; the installed package.json pins 3.23.8.
    let fixture = run_partial_swap_fixture(
        "export const e6 = () => true;\n",
        SWAPPED_ONLY_CALLER,
        ZOD_TRUE,
        ("e6", "boolean"),
        "9.9.9",
    );
    assert_rejected(&fixture.result, &["version mismatch"]);
}

/// Shared prefix: `other` reads state filled by an init-time IIFE, and
/// `unwrap` is a default-unwrapper both exports' callbacks pass through.
const UNWRAPPER_PRELUDE: &str = "const state = {};\n\
     (function (cell) { cell.value = 7; })(state);\n\
     function unwrap(value) { return value && value.__esModule ? value.default : value; }\n";

/// Owners the strip can separate: the swapped and retained exports reach
/// distinct callbacks, so the emitted caller still prints both values.
#[test]
fn partial_swap_separates_callback_owners_at_runtime() {
    for (name, body, upstream, expected) in [
        (
            "two calls to the same default unwrapper",
            "function selected() { return 1; }\n\
             function other() { return state.value; }\n\
             const selectedValue = unwrap(selected);\n\
             const otherValue = unwrap(other);\n\
             export const e6 = () => selectedValue();\n\
             export const keepMe = () => otherValue();\n",
            ZOD_ONE,
            "1 7\n",
        ),
        (
            "unwrapper input installed by a local property write",
            "const api = {};\n\
             (function (target) { target.get = function selected() { return 1; }; })(api);\n\
             function other() { return state.value; }\n\
             const selectedValue = unwrap(api.get);\nconst otherValue = unwrap(other);\n\
             export const e6 = () => selectedValue();\n\
             export const keepMe = () => otherValue();\n",
            ZOD_ONE,
            "1 7\n",
        ),
    ] {
        in_case(name, || {
            let fixture = zod_swap(
                &format!("{UNWRAPPER_PRELUDE}{body}"),
                LOGGING_CALLER,
                upstream,
            );
            fixture.assert_zod_caller_output(upstream, expected);
        });
    }
    in_case("escaped synchronous callback effect", || {
        let fixture = zod_swap(
            "export const e6 = () => true;\n\
             const callbacks = [];\n\
             function register(callback) { callbacks.push(callback); }\n\
             register(() => { globalThis.__vendor_callback_probe = 'called'; });\n\
             callbacks[0]();\n\
             export const keepMe = () => globalThis.__vendor_callback_probe;\n",
            LOGGING_CALLER,
            ZOD_TRUE,
        );
        fixture.assert_zod_caller_output(ZOD_TRUE, "true called\n");
    });
}

/// Anchors for the runtime cases above: a default the unwrapper returns is
/// the retained callback itself, or a hard effect reads the swapped binding.
#[test]
fn partial_swap_rejects_effects_the_strip_would_lose() {
    for (name, chunk, caller, upstream, diagnostics) in [
        (
            "function default returning the shared callback",
            format!(
                "{UNWRAPPER_PRELUDE}\
                 function selected() {{ return 1; }}\n\
                 function other() {{ return state.value; }}\n\
                 selected.__esModule = true;\nconst alias = selected;\nalias.default = other;\n\
                 const selectedValue = unwrap(selected);\n\
                 export const e6 = () => selectedValue();\n\
                 export const keepMe = () => other();\n"
            ),
            LOGGING_CALLER,
            ZOD_ONE,
            &["split-brain vendor swap"][..],
        ),
        (
            "accessor default returning the shared callback",
            format!(
                "{UNWRAPPER_PRELUDE}\
                 function selected() {{ return 1; }}\n\
                 function other() {{ return state.value; }}\n\
                 const api = {{}};\napi.get = selected;\n\
                 selected.__esModule = true;\n\
                 Object.defineProperty(selected, 'default', {{ get() {{ return other; }} }});\n\
                 const selectedValue = unwrap(api.get);\n\
                 export const e6 = () => selectedValue();\n\
                 export const keepMe = () => other();\n"
            ),
            LOGGING_CALLER,
            ZOD_ONE,
            &["split-brain vendor swap"],
        ),
        // A globally observable write that reads the swapped binding is
        // swap-reachable, so the keep-pass cannot honor it and dropping it
        // would lose the effect.
        (
            "observable side effect reading the swapped binding",
            "export const e6 = () => true;\nglobalThis.__zodBoolean = e6;\n".to_owned(),
            SWAPPED_ONLY_CALLER,
            ZOD_TRUE,
            &["observable side-effect item", "swap-reachable"],
        ),
    ] {
        in_case(name, || {
            assert_rejected(&zod_swap(&chunk, caller, upstream).result, diagnostics);
        });
    }
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
        let fixture = zod_swap(&chunk_source, LOGGING_CALLER, ZOD_ONE);
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
