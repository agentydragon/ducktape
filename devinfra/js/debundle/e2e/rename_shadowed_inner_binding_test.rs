//! Regression: lowering rename passes must be scope-aware.
//!
//! A top-level binding rename (here driven by `chunk_renames`, the same
//! string-keyed map the `IdentifierRenamer` walks the whole entry body with)
//! must NOT leak into a nested scope that re-binds the same name. The old
//! sym-only renamer rewrote every matching identifier — including an inner
//! `var`/`let` declaration and its references that shadow the renamed
//! top-level name — which both rewrote a binding it had no business touching
//! and, when the rename target collided with another in-scope binding,
//! silently merged two distinct bindings and changed runtime behavior.
//!
//! This is the emit-side instance of the shadowing root cause fixed in the
//! purity classifier (#1714).

use debundle_e2e_support::*;
use serde_json::{Value, json};

fn chunk_rename(rename_to: &str, from_binding: &str) -> Value {
    json!({
        "members": [
            {
                "name": rename_to,
                "selector": { "binding": { "name": from_binding } },
            },
        ],
    })
}

/// Renaming top-level `a` -> `b` must leave the inner function's own `var a`
/// (which shadows the top-level `a`) untouched. The rename target `b` already
/// names the function's parameter, so a leaked rename of the inner `var a`
/// would collide with that parameter and merge two distinct bindings —
/// observably corrupting the return value.
#[test]
fn top_level_rename_does_not_leak_into_shadowing_inner_var() {
    let opts = FixtureOpts::new(
        r#"var a = "A";
function f(b) {
  var a = "innerA";
  return a + b;
}
console.log(f("B") + a);
export { a, f };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_rename("b", "a"))
    .with_unassigned_mode(unassigned_mode_inline());
    let fixture = run_fixture(opts);

    // Runtime must be unchanged by the rename: f("B") = "innerA" + "B", then
    // + the top-level value "A". A leaked rename would turn the inner body
    // into `var b = "innerA"; return b + b;` (the param `b` is clobbered),
    // yielding "innerAinnerAA".
    assert_entry_output(&fixture, "innerABA\n");

    // The top-level binding is renamed to `b`; the inner shadowing binding
    // and its references stay `a`.
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &[
            "var b = \"A\"",
            "function f(b)",
            "var a = \"innerA\"",
            "return a + b",
            "console.log(f(\"B\") + b)",
        ],
        &["var b = \"innerA\"", "return b + b"],
    );
}

/// Labels live in a separate namespace from bindings; renaming binding
/// `a` must not rewrite a label `a:` or its `break`/`continue`
/// references (a shadow-suppressed `continue a` paired with a renamed
/// `b:` label would desync into a SyntaxError).
#[test]
fn top_level_rename_does_not_rename_matching_labels() {
    let opts = FixtureOpts::new(
        r#"var a = "A";
function f() {
  a: for (;;) {
    break a;
  }
  return a;
}
console.log(f());
export { a, f };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_rename("b", "a"))
    .with_unassigned_mode(unassigned_mode_inline());
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "A\n");
    // The label and its break stay `a`; the binding reference inside
    // `f` (not shadowed) is renamed to `b`.
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &["a: for", "break a", "return b"],
        &["b: for", "break b"],
    );
}

/// The same guard for a function parameter that shadows the renamed
/// top-level name: `f(a)`'s param `a` and its uses refer to the parameter,
/// not the renamed top-level binding, so they must be left alone.
#[test]
fn top_level_rename_does_not_leak_into_shadowing_param() {
    let opts = FixtureOpts::new(
        r#"var a = "top";
function f(a) {
  return a;
}
console.log(f("arg") + a);
export { a, f };
"#,
        vec![],
    )
    .with_chunk_renames(chunk_rename("readable", "a"))
    .with_unassigned_mode(unassigned_mode_inline());
    let fixture = run_fixture(opts);

    assert_entry_output(&fixture, "argtop\n");
    assert_module_source(
        &fixture.out_root,
        "static/app/entry.js",
        &[
            "var readable = \"top\"",
            "function f(a)",
            "return a",
            "console.log(f(\"arg\") + readable)",
        ],
        &["function f(readable)", "return readable"],
    );
}
